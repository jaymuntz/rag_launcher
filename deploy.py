#!/usr/bin/env python3
"""
Full deployment script for the jay-chat CloudFormation stack.

Steps:
  1. ACM certificates (us-east-1) with Route53 DNS validation
  2. WAF Web ACLs (us-east-1, CLOUDFRONT scope)
  3. Build Lambda zips from source and upload to S3
  4. Pre-flight check for orphaned retained resources
  5. Deploy CloudFormation stack
  6. Post-deploy Route53 CNAMEs pointing aliases to CloudFront domains
"""

import argparse
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

TEMPLATE_FILE = "rag_template.yaml"
PARAMETERS_FILE = "parameters.json"

BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# parameters.json helpers
# ---------------------------------------------------------------------------

def load_params():
    with open(BASE_DIR / PARAMETERS_FILE) as f:
        return json.load(f)  # list of {"ParameterKey": ..., "ParameterValue": ...}


def save_params(params):
    with open(BASE_DIR / PARAMETERS_FILE, "w") as f:
        json.dump(params, f, indent=2)
        f.write("\n")


def get_param(params, key):
    return next((p["ParameterValue"] for p in params if p["ParameterKey"] == key), None)


def set_param(params, key, value):
    for p in params:
        if p["ParameterKey"] == key:
            p["ParameterValue"] = value
            return
    raise KeyError(f"Parameter {key} not found in {PARAMETERS_FILE}")


# ---------------------------------------------------------------------------
# Step 1: ACM certificates
# ---------------------------------------------------------------------------

def ensure_certificate(acm, route53, params, alias_key, arn_key):
    domain = get_param(params, alias_key)
    existing_arn = get_param(params, arn_key)

    if existing_arn:
        try:
            resp = acm.describe_certificate(CertificateArn=existing_arn)
            status = resp["Certificate"]["Status"]
            if status == "ISSUED":
                print(f"  [{domain}] certificate already ISSUED")
                return
            print(f"  [{domain}] certificate status: {status}")
        except ClientError:
            print(f"  [{domain}] stored ARN not found, requesting new certificate")
            existing_arn = None

    if not existing_arn:
        # Check if a cert already exists for this domain
        paginator = acm.get_paginator("list_certificates")
        found_arn = None
        for page in paginator.paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
            for cert in page["CertificateSummaryList"]:
                if cert["DomainName"] == domain:
                    found_arn = cert["CertificateArn"]
                    break
            if found_arn:
                break

        if found_arn:
            print(f"  [{domain}] found existing certificate {found_arn}")
            existing_arn = found_arn
        else:
            print(f"  [{domain}] requesting new ACM certificate")
            resp = acm.request_certificate(DomainName=domain, ValidationMethod="DNS")
            existing_arn = resp["CertificateArn"]
            print(f"  [{domain}] certificate ARN: {existing_arn}")
            time.sleep(5)  # let AWS populate validation options

        set_param(params, arn_key, existing_arn)
        save_params(params)

    # Create Route53 DNS validation record if not yet validated
    for _ in range(12):
        resp = acm.describe_certificate(CertificateArn=existing_arn)
        cert = resp["Certificate"]
        if cert["Status"] == "ISSUED":
            print(f"  [{domain}] certificate is ISSUED")
            return
        options = cert.get("DomainValidationOptions", [])
        if options and options[0].get("ResourceRecord"):
            record = options[0]["ResourceRecord"]
            break
        print(f"  [{domain}] waiting for validation options...")
        time.sleep(10)
    else:
        raise RuntimeError(f"Timed out waiting for validation options for {domain}")

    zone_id = _find_hosted_zone(route53, domain)
    print(f"  [{domain}] upserting DNS validation record")
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Changes": [{
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": record["Name"],
                "Type": record["Type"],
                "TTL": 300,
                "ResourceRecords": [{"Value": record["Value"]}],
            },
        }]},
    )

    print(f"  [{domain}] waiting for certificate to be issued (may take a few minutes)...")
    waiter = acm.get_waiter("certificate_validated")
    waiter.wait(CertificateArn=existing_arn, WaiterConfig={"Delay": 30, "MaxAttempts": 40})
    print(f"  [{domain}] certificate is now ISSUED")


def _find_hosted_zone(route53, domain):
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:]) + "."
        resp = route53.list_hosted_zones_by_name(DNSName=candidate, MaxItems="1")
        zones = resp["HostedZones"]
        if zones and zones[0]["Name"] == candidate:
            return zones[0]["Id"].split("/")[-1]
    raise ValueError(f"No Route53 hosted zone found for {domain}")


# ---------------------------------------------------------------------------
# Step 2: WAF Web ACLs
# ---------------------------------------------------------------------------

def ensure_waf_acl(wafv2, params, name_suffix, arn_key):
    app_name = get_param(params, "AppName")
    acl_name = f"{app_name}-{name_suffix}"
    existing_arn = get_param(params, arn_key)

    if existing_arn:
        try:
            parts = existing_arn.split("/")
            wafv2.get_web_acl(Name=parts[-2], Scope="CLOUDFRONT", Id=parts[-1])
            print(f"  [{acl_name}] WAF ACL already exists")
            return
        except ClientError:
            print(f"  [{acl_name}] stored ARN not found, creating new ACL")

    # Check by name
    for acl in wafv2.list_web_acls(Scope="CLOUDFRONT").get("WebACLs", []):
        if acl["Name"] == acl_name:
            print(f"  [{acl_name}] found existing WAF ACL")
            set_param(params, arn_key, acl["ARN"])
            save_params(params)
            return

    print(f"  [{acl_name}] creating WAF ACL")
    resp = wafv2.create_web_acl(
        Name=acl_name,
        Scope="CLOUDFRONT",
        DefaultAction={"Allow": {}},
        VisibilityConfig={
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": acl_name,
        },
        Rules=[],
    )
    arn = resp["Summary"]["ARN"]
    print(f"  [{acl_name}] created: {arn}")
    set_param(params, arn_key, arn)
    save_params(params)


# ---------------------------------------------------------------------------
# Step 3: Build Lambda zips and upload to S3
# ---------------------------------------------------------------------------

def build_and_upload_lambdas(s3, params):
    bucket = get_param(params, "LambdaCodeS3Bucket")

    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  S3 bucket {bucket} exists")
    except ClientError:
        print(f"  Creating S3 bucket {bucket}")
        s3.create_bucket(Bucket=bucket)

    _upload_zip(s3, bucket, "rag-ingest.zip",     _build_ingest)
    _upload_zip(s3, bucket, "rag-node.zip",        _build_node)
    _upload_zip(s3, bucket, "rag-log-indexer.zip", _build_log_indexer)


def _upload_zip(s3, bucket, key, build_fn):
    zip_path = BASE_DIR / key
    build_fn(zip_path)
    print(f"  Uploading {key}")
    s3.upload_file(str(zip_path), bucket, key)


def _build_ingest(zip_path):
    src = BASE_DIR / "rag-ingest"
    print(f"  Building rag-ingest.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        pkg = src / "package"
        for f in sorted(pkg.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(pkg))
        zf.write(src / "lambda_function.py", "lambda_function.py")


def _build_node(zip_path):
    src = BASE_DIR / "rag-node"
    print(f"  Building rag-node.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src / "index.mjs", "index.mjs")
        for f in sorted((src / "node_modules").rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src))


def _build_log_indexer(zip_path):
    src = BASE_DIR / "rag-log-indexer"
    print(f"  Building rag-log-indexer.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src / "lambda_function.py", "lambda_function.py")


# ---------------------------------------------------------------------------
# Step 4: Pre-flight check for orphaned retained resources
# ---------------------------------------------------------------------------

def preflight_check(session, params, profile):
    app = get_param(params, "AppName")
    issues = []

    s3        = session.client("s3")
    cf        = session.client("cloudfront")
    lam       = session.client("lambda")
    scheduler = session.client("scheduler")
    bedrock   = session.client("bedrock-agent")

    def warn(resource_type, name, delete_cmd):
        issues.append(f"{resource_type} '{name}' already exists.\n    Delete: {delete_cmd}")

    # S3 data bucket
    bucket = f"{app}-rag-chatbot-data"
    try:
        s3.head_bucket(Bucket=bucket)
        warn("S3 bucket", bucket, f"aws s3 rb --force s3://{bucket} --profile {profile}")
    except ClientError:
        pass

    # CloudFront function
    fn = f"{app}-password-protect-logs"
    try:
        resp = cf.describe_function(Name=fn)
        etag = resp["ETag"]
        warn("CloudFront function", fn,
             f"aws cloudfront delete-function --name {fn} --if-match {etag} --profile {profile}")
    except ClientError:
        pass

    # Lambda functions
    for fn_name in [f"{app}-rag-ingest", f"{app}-rag-node", f"{app}-rag-log-indexer"]:
        try:
            lam.get_function(FunctionName=fn_name)
            warn("Lambda function", fn_name,
                 f"aws lambda delete-function --function-name {fn_name} --profile {profile}")
        except ClientError:
            pass

    # EventBridge schedules
    for sched in [f"{app}-hourly", f"{app}-every-15-minutes", f"{app}-nightly"]:
        try:
            scheduler.get_schedule(Name=sched)
            warn("EventBridge schedule", sched,
                 f"aws scheduler delete-schedule --name {sched} --profile {profile}")
        except ClientError:
            pass

    # Bedrock knowledge base (check by name)
    kb_name = f"{app}-products-kb"
    try:
        resp = bedrock.list_knowledge_bases(maxResults=50)
        for kb in resp.get("knowledgeBaseSummaries", []):
            if kb["name"] == kb_name:
                warn("Bedrock knowledge base", kb_name,
                     f"aws bedrock-agent delete-knowledge-base --knowledge-base-id {kb['knowledgeBaseId']} --profile {profile}")
                break
    except ClientError:
        pass

    if issues:
        print("\n  Orphaned resources detected — delete them before deploying:\n")
        for issue in issues:
            print(f"  • {issue}\n")
        sys.exit(1)

    print("  No orphaned resources detected")


# ---------------------------------------------------------------------------
# Step 5: Deploy CloudFormation stack
# ---------------------------------------------------------------------------

def deploy_stack(profile, region, stack_name):
    cmd = [
        "aws", "cloudformation", "deploy",
        "--template-file", str(BASE_DIR / TEMPLATE_FILE),
        "--stack-name", stack_name,
        "--parameter-overrides", f"file://{BASE_DIR / PARAMETERS_FILE}",
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--region", region,
        "--profile", profile,
    ]
    print(f"  Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Step 6: Post-deploy Route53 CNAMEs
# ---------------------------------------------------------------------------

def post_deploy_dns(session, route53, params, stack_name):
    cfn = session.client("cloudformation")
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}

    for alias_key, output_key in [
        ("ChatbotAlias", "ChatbotDistributionDomain"),
        ("LogsAlias", "LogsDistributionDomain"),
    ]:
        alias = get_param(params, alias_key)
        cf_domain = outputs.get(output_key)
        if not cf_domain:
            print(f"  No stack output for {output_key}, skipping")
            continue

        zone_id = _find_hosted_zone(route53, alias)
        print(f"  Upserting CNAME {alias} → {cf_domain}")
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={"Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": alias + ".",
                    "Type": "CNAME",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": cf_domain}],
                },
            }]},
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Deploy the CloudFormation stack and prerequisites")
    parser.add_argument("--profile",    default="bedrock-course", help="AWS profile (default: bedrock-course)")
    parser.add_argument("--region",     default="us-east-1",      help="AWS region (default: us-east-1)")
    parser.add_argument("--stack-name", default="jay-chat",        help="CloudFormation stack name (default: jay-chat)")
    args = parser.parse_args()

    profile    = args.profile
    region     = args.region
    stack_name = args.stack_name

    params  = load_params()
    sess    = boto3.Session(profile_name=profile, region_name=region)

    acm     = sess.client("acm",    region_name="us-east-1")
    wafv2   = sess.client("wafv2",  region_name="us-east-1")
    route53 = sess.client("route53")
    s3      = sess.client("s3",     region_name="us-east-1")

    print("\n=== Step 1: ACM Certificates ===")
    ensure_certificate(acm, route53, params, "ChatbotAlias", "ChatbotAcmCertificateArn")
    ensure_certificate(acm, route53, params, "LogsAlias",    "LogsAcmCertificateArn")

    print("\n=== Step 2: WAF Web ACLs ===")
    ensure_waf_acl(wafv2, params, "chatbot-waf", "ChatbotWafAclArn")
    ensure_waf_acl(wafv2, params, "logs-waf",    "LogsWafAclArn")

    print("\n=== Step 3: Lambda packages ===")
    build_and_upload_lambdas(s3, params)

    print("\n=== Step 4: Pre-flight check ===")
    preflight_check(sess, params, profile)

    print("\n=== Step 5: Deploy CloudFormation stack ===")
    deploy_stack(profile, region, stack_name)

    print("\n=== Step 6: Post-deploy DNS ===")
    post_deploy_dns(sess, route53, params, stack_name)

    print("\nDone.")


if __name__ == "__main__":
    main()
