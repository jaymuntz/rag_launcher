#!/usr/bin/env python3
"""
Teardown script for the jay-chat CloudFormation stack.

Deletes everything created by deploy.py:
  1.  Disable CloudFront distribution (async — takes a few minutes)
  2.  Delete CloudFormation stack (handles IAM, OACs, bucket policy, etc.)
  3.  Wait for distribution to finish disabling, then delete it
  4.  Delete retained resources:
        Lambda functions, Bedrock data source + knowledge base, S3 data bucket,
        Lambda code S3 bucket
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

PARAMETERS_FILE = "parameters.json"

BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_params():
    with open(BASE_DIR / PARAMETERS_FILE) as f:
        return json.load(f)

def get_param(params, key):
    return next((p["ParameterValue"] for p in params if p["ParameterKey"] == key), None)

def confirm(msg):
    ans = input(f"\n{msg} [y/N] ").strip().lower()
    if ans != "y":
        print("Aborted.")
        sys.exit(0)


def get_stack_resources(cfn, stack_name):
    """Return {LogicalId: PhysicalId} for the current stack."""
    out = {}
    try:
        paginator = cfn.get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=stack_name):
            for r in page["StackResourceSummaries"]:
                out[r["LogicalResourceId"]] = r.get("PhysicalResourceId", "")
    except ClientError:
        pass
    return out


# ---------------------------------------------------------------------------
# Step 1: Disable CloudFront distribution (non-blocking)
# ---------------------------------------------------------------------------

def disable_distribution(cf, dist_id):
    """Set Enabled=False on a distribution. Returns the new ETag."""
    try:
        resp = cf.get_distribution_config(Id=dist_id)
    except ClientError as e:
        if "NoSuchDistribution" in str(e):
            return None
        raise

    config = resp["DistributionConfig"]
    etag   = resp["ETag"]

    if not config["Enabled"]:
        print(f"  Distribution {dist_id} already disabled")
        return etag

    config["Enabled"] = False
    resp2 = cf.update_distribution(Id=dist_id, DistributionConfig=config, IfMatch=etag)
    print(f"  Disabling distribution {dist_id} (takes a few minutes)")
    return resp2["ETag"]


def wait_and_delete_distribution(cf, dist_id):
    """Wait for a distribution to reach Deployed state, then delete it."""
    if not dist_id:
        return

    print(f"  Waiting for distribution {dist_id} to finish disabling...")
    for _ in range(40):
        try:
            resp = cf.get_distribution(Id=dist_id)
        except ClientError as e:
            if "NoSuchDistribution" in str(e):
                print(f"  Distribution {dist_id} already gone")
                return
            raise
        status = resp["Distribution"]["Status"]
        if status == "Deployed":
            break
        time.sleep(30)
    else:
        print(f"  WARNING: timed out waiting for {dist_id} — delete it manually")
        return

    etag = cf.get_distribution_config(Id=dist_id)["ETag"]
    cf.delete_distribution(Id=dist_id, IfMatch=etag)
    print(f"  Deleted distribution {dist_id}")


# ---------------------------------------------------------------------------
# Step 2: Delete CloudFormation stack
# ---------------------------------------------------------------------------

def delete_stack(cfn, stack_name):
    try:
        cfn.describe_stacks(StackName=stack_name)
    except ClientError:
        print(f"  Stack {stack_name} not found, skipping")
        return

    cfn.delete_stack(StackName=stack_name)
    print(f"  Waiting for stack deletion...")
    waiter = cfn.get_waiter("stack_delete_complete")
    try:
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
        print(f"  Stack deleted")
    except WaiterError:
        # Fetch the actual stack status and failed resource reasons
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
        except ClientError:
            status = "UNKNOWN"

        if status == "DELETE_FAILED":
            failed = []
            try:
                paginator = cfn.get_paginator("list_stack_resources")
                for page in paginator.paginate(StackName=stack_name):
                    for r in page["StackResourceSummaries"]:
                        if r["ResourceStatus"] == "DELETE_FAILED":
                            failed.append(
                                f"    {r['LogicalResourceId']} ({r['ResourceType']}): "
                                f"{r.get('ResourceStatusReason', 'no reason given')}"
                            )
            except ClientError:
                pass

            print(f"\n  WARNING: Stack is in DELETE_FAILED state. Some resources could not be deleted:")
            for line in failed:
                print(line)

            lambda_edge_stuck = any("replicated function" in f for f in failed)
            if lambda_edge_stuck:
                print(
                    "\n  NOTE: Lambda@Edge functions cannot be deleted while CloudFront replicas still exist."
                    "\n  AWS automatically removes replicas over the next 1-2 hours."
                    "\n  After waiting, delete the stuck function(s) manually, then re-run teardown"
                    "\n  or delete the stack again via the AWS Console."
                )
            print(f"\n  Continuing teardown of remaining resources...\n")
        else:
            raise


# ---------------------------------------------------------------------------
# Step 4: Delete retained resources
# ---------------------------------------------------------------------------


def delete_lambda_function(lam, name):
    try:
        lam.delete_function(FunctionName=name)
        print(f"  Deleted Lambda function {name}")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            print(f"  Lambda {name} not found, skipping")
        elif "replicated function" in str(e):
            print(
                f"  WARNING: Cannot delete Lambda {name} — it is a Lambda@Edge function with"
                f" replicas still being removed by AWS. Wait 1-2 hours, then run:\n"
                f"    aws lambda delete-function --function-name {name} --region us-east-1"
            )
        else:
            raise



def delete_bedrock_resources(bedrock, kb_id, ds_id):
    if not kb_id:
        print("  No Knowledge Base ID, skipping Bedrock cleanup")
        return

    if ds_id:
        try:
            bedrock.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
            print(f"  Deleted data source {ds_id}")
        except ClientError as e:
            if "ResourceNotFoundException" in str(e):
                print(f"  Data source {ds_id} not found, skipping")
            else:
                raise

    try:
        bedrock.delete_knowledge_base(knowledgeBaseId=kb_id)
        print(f"  Deleted knowledge base {kb_id}")
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            print(f"  Knowledge base {kb_id} not found, skipping")
        else:
            raise


def empty_and_delete_bucket(s3, bucket_name):
    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError:
        print(f"  Bucket {bucket_name} not found, skipping")
        return

    print(f"  Emptying bucket {bucket_name}...")
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        objects = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        if objects:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})

    # Also delete any unversioned objects
    paginator2 = s3.get_paginator("list_objects_v2")
    for page in paginator2.paginate(Bucket=bucket_name):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})

    s3.delete_bucket(Bucket=bucket_name)
    print(f"  Deleted bucket {bucket_name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tear down the CloudFormation stack and all related resources")
    parser.add_argument("--profile",    default="default", help="AWS profile (default: default)")
    parser.add_argument("--region",     default="us-east-1",      help="AWS region (default: us-east-1)")
    parser.add_argument("--stack-name", required=True, help="CloudFormation stack name to tear down")
    args = parser.parse_args()

    profile    = args.profile
    region     = args.region
    stack_name = args.stack_name

    params = load_params()
    app    = get_param(params, "AppName")

    sess = boto3.Session(profile_name=profile, region_name=region)

    cfn     = sess.client("cloudformation")
    cf      = sess.client("cloudfront")
    s3      = sess.client("s3")
    lam     = sess.client("lambda")
    bedrock = sess.client("bedrock-agent")

    confirm(f"This will permanently destroy the {stack_name} stack and all its resources. Continue?")

    print("\nCollecting stack resource IDs...")
    res = get_stack_resources(cfn, stack_name)

    chatbot_dist_id = res.get("ChatbotDistribution")
    kb_id           = res.get("KnowledgeBase")
    ds_physical     = res.get("KnowledgeBaseDataSource", "")
    ds_id = ds_physical.split("|")[-1] if "|" in ds_physical else None

    print("\n=== Step 1: Disable CloudFront distribution ===")
    if chatbot_dist_id:
        disable_distribution(cf, chatbot_dist_id)

    print("\n=== Step 2: Delete CloudFormation stack ===")
    delete_stack(cfn, stack_name)

    print("\n=== Step 3: Delete CloudFront distribution ===")
    wait_and_delete_distribution(cf, chatbot_dist_id)

    print("\n=== Step 4: Delete retained resources ===")
    delete_lambda_function(lam, f"{app}-rag-node")
    signer_suffix = get_param(params, "RagSignerSuffix")
    if signer_suffix:
        delete_lambda_function(lam, f"{app}-rag-signer-{signer_suffix}")
    else:
        print("  No RagSignerSuffix in parameters.json, skipping signer deletion")

    delete_bedrock_resources(bedrock, kb_id, ds_id)

    bucket_name = res.get("DataBucket", f"{app}-rag-chatbot-data")
    empty_and_delete_bucket(s3, bucket_name)

    lambda_bucket = get_param(params, "LambdaCodeS3Bucket")
    if lambda_bucket:
        empty_and_delete_bucket(s3, lambda_bucket)
    else:
        print("  No LambdaCodeS3Bucket in parameters.json, skipping")

    print("\nDone.")


if __name__ == "__main__":
    main()
