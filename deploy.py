#!/usr/bin/env python3
"""
Full deployment script for the jay-chat CloudFormation stack.

Steps:
  1. Build Lambda zips from source and upload to S3
  2. Pre-flight check for orphaned retained resources
  3. Deploy CloudFormation stack
  4. Upload frontend (index.html, favicon.ico, system_prompt.txt)
  5. Upload knowledge base files (optional)
"""

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
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
# Step 1: Build Lambda zips and upload to S3
# ---------------------------------------------------------------------------

def build_and_upload_lambdas(s3, params):
    bucket = get_param(params, "LambdaCodeS3Bucket")

    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  S3 bucket {bucket} exists")
    except ClientError:
        print(f"  Creating S3 bucket {bucket}")
        s3.create_bucket(Bucket=bucket)

    app = get_param(params, "AppName")
    _upload_zip(s3, bucket, "rag-node.zip",   _build_node)
    _upload_zip(s3, bucket, "rag-signer.zip", lambda p: _build_signer(p, app))


def _upload_zip(s3, bucket, key, build_fn):
    zip_path = BASE_DIR / key
    build_fn(zip_path)
    print(f"  Uploading {key}")
    s3.upload_file(str(zip_path), bucket, key)


def _build_node(zip_path):
    src = BASE_DIR / "rag-node"
    print(f"  Building rag-node.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src / "index.mjs", "index.mjs")
        for f in sorted((src / "node_modules").rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src))



def _build_signer(zip_path, app_name):
    src = BASE_DIR / "rag-signer"
    print(f"  Building rag-signer.zip (app: {app_name})")
    code = (src / "index.mjs").read_text().replace("__APP_NAME__", app_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.mjs", code)


# ---------------------------------------------------------------------------
# Step 2: Pre-flight check for orphaned retained resources
# ---------------------------------------------------------------------------

def preflight_check(session, params, profile):
    # Pre-flight only matters for fresh deploys — retained resources from a prior
    # stack would block CloudFormation. If a stack already exists, it owns those
    # resources and we're just doing an update; skip the check entirely.
    stack_name = get_param(params, "AppName")
    cfn = session.client("cloudformation")
    try:
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        if not status.endswith("_FAILED"):
            print(f"  Stack '{stack_name}' exists ({status}) — skipping orphan check")
            return
    except ClientError:
        pass  # stack doesn't exist → run full pre-flight

    app = get_param(params, "AppName")
    issues = []

    s3      = session.client("s3")
    lam     = session.client("lambda")
    bedrock = session.client("bedrock-agent")

    def warn(resource_type, name, delete_cmd):
        issues.append(f"{resource_type} '{name}' already exists.\n    Delete: {delete_cmd}")

    # S3 data bucket (name includes AppSuffix)
    app_suffix = get_param(params, "AppSuffix") or ""
    bucket = f"{app}-rag-chatbot-data-{app_suffix}" if app_suffix else f"{app}-rag-chatbot-data"
    try:
        s3.head_bucket(Bucket=bucket)
        warn("S3 bucket", bucket, f"aws s3 rb --force s3://{bucket} --profile {profile}")
    except ClientError:
        pass

    # Lambda function (signer excluded — suffix makes each deploy's name unique)
    try:
        lam.get_function(FunctionName=f"{app}-rag-node")
        warn("Lambda function", f"{app}-rag-node",
             f"aws lambda delete-function --function-name {app}-rag-node --profile {profile}")
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
# Step 3: Deploy CloudFormation stack
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
# Step 5: Upload knowledge base files (optional)
# ---------------------------------------------------------------------------

def upload_kb_files(s3, kb_files_path, stack_name, sess):
    path = Path(kb_files_path)
    if not path.is_dir():
        print(f"  {kb_files_path} is not a directory, skipping")
        return

    files = [f for f in sorted(path.rglob("*")) if f.is_file()]
    if not files:
        print(f"  No files found in {kb_files_path}")
        return

    cfn = sess.client("cloudformation")
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    bucket = outputs.get("DataBucketName")
    kb_id  = outputs.get("KnowledgeBaseId")
    if not bucket:
        print("  Could not find DataBucketName in stack outputs, skipping upload")
        return

    print(f"  Uploading {len(files)} file(s) to s3://{bucket}/knowledgebase-files/")
    for f in files:
        key = f"knowledgebase-files/{f.relative_to(path)}"
        s3.upload_file(str(f), bucket, key)
        print(f"    {f.name} → {key}")

    if not kb_id:
        print("  KnowledgeBaseId not found in stack outputs, skipping sync")
        return

    bedrock = sess.client("bedrock-agent")
    ds_resp = bedrock.list_data_sources(knowledgeBaseId=kb_id, maxResults=1)
    data_sources = ds_resp.get("dataSourceSummaries", [])
    if not data_sources:
        print("  No data sources found for knowledge base, skipping sync")
        return
    ds_id = data_sources[0]["dataSourceId"]

    print(f"  Starting knowledge base sync (KB: {kb_id}, DS: {ds_id})")
    job_resp = bedrock.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    job_id   = job_resp["ingestionJob"]["ingestionJobId"]

    print(f"  Waiting for sync to complete...")
    while True:
        job = bedrock.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = job["status"]
        if status == "COMPLETE":
            stats = job.get("statistics", {})
            print(f"  Sync complete — "
                  f"scanned: {stats.get('numberOfDocumentsScanned', '?')}, "
                  f"indexed: {stats.get('numberOfNewDocumentsIndexed', '?')}, "
                  f"failed: {stats.get('numberOfDocumentsFailed', '?')}")
            break
        if status == "FAILED":
            print(f"  Sync FAILED: {job.get('failureReasons', [])}")
            break
        time.sleep(10)


# ---------------------------------------------------------------------------
# Step 4: Upload frontend (index.html, favicon.ico, system_prompt.txt)
# ---------------------------------------------------------------------------

def upload_frontend(s3, stack_name, sess):
    html_path = BASE_DIR / "frontend" / "index.html"
    if not html_path.exists():
        print(f"  index.html not found at {html_path}, skipping")
        return

    cfn = sess.client("cloudformation")
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}

    bucket = outputs.get("DataBucketName")
    if not bucket:
        print("  DataBucketName not in stack outputs, skipping frontend upload")
        return

    api_url = f"https://{outputs.get('ChatbotDistributionDomain', stack_name)}/api"

    html = html_path.read_text(encoding="utf-8")
    html = re.sub(r"<title>[^<]*</title>",   f"<title>{stack_name}</title>",  html)
    html = re.sub(r"<h1>[^<]*</h1>",         f"<h1>{stack_name}</h1>",        html)
    html = re.sub(r'const API_URL = "[^"]*";', f'const API_URL = "{api_url}";', html)

    s3.put_object(
        Bucket=bucket,
        Key="front/index.html",
        Body=html.encode("utf-8"),
        ContentType="text/html",
    )
    print(f"  Uploaded index.html → s3://{bucket}/front/index.html")

    favicon_path = BASE_DIR / "frontend" / "favicon.ico"
    if favicon_path.exists():
        s3.put_object(
            Bucket=bucket,
            Key="front/favicon.ico",
            Body=favicon_path.read_bytes(),
            ContentType="image/x-icon",
        )
        print(f"  Uploaded favicon.ico → s3://{bucket}/front/favicon.ico")
    else:
        print(f"  favicon.ico not found at {favicon_path}, skipping")

    prompt_path = BASE_DIR / "frontend" / "system_prompt.txt"
    if prompt_path.exists():
        s3.put_object(
            Bucket=bucket,
            Key="system_prompt.txt",
            Body=prompt_path.read_bytes(),
            ContentType="text/plain",
        )
        print(f"  Uploaded system_prompt.txt → s3://{bucket}/system_prompt.txt")
    else:
        print(f"  system_prompt.txt not found at {prompt_path}, skipping")


# ---------------------------------------------------------------------------
# Interactive configuration prompt
# ---------------------------------------------------------------------------

def prompt_deployment_config(params):
    """Prompt for deployment settings, update params in-place, and return (stack_name, kb_files)."""
    print("\n=== Deployment Configuration ===")

    current_stack = get_param(params, "AppName") or "parts-assistant"
    while True:
        stack_name = input(f"Stack name [{current_stack}]: ").strip() or current_stack
        if re.fullmatch(r'[a-z][a-z0-9-]{0,19}', stack_name):
            break
        print("  Stack name must be lowercase letters, numbers, and hyphens only, start with a letter, max 20 chars.")

    kb_files = input("Knowledge base files directory (Enter to skip): ").strip() or None

    set_param(params, "AppName", stack_name)

    # Generate AppSuffix once; preserve it on every subsequent re-deploy.
    app_suffix = get_param(params, "AppSuffix") or ""
    if not app_suffix:
        app_suffix = uuid.uuid4().hex[:5]
        set_param(params, "AppSuffix", app_suffix)
    set_param(params, "LambdaCodeS3Bucket", f"{stack_name}-rag-lambdas-{app_suffix}")
    save_params(params)

    return stack_name, kb_files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Deploy the CloudFormation stack and prerequisites")
    parser.add_argument("--profile", default="default", help="AWS profile (default: default)")
    parser.add_argument("--region",  default="us-east-1", help="AWS region (default: us-east-1)")
    args = parser.parse_args()

    profile = args.profile
    region  = args.region

    params = load_params()
    stack_name, kb_files = prompt_deployment_config(params)

    sess = boto3.Session(profile_name=profile, region_name=region)
    s3   = sess.client("s3", region_name="us-east-1")

    print("\n=== Step 1: Lambda packages ===")
    build_and_upload_lambdas(s3, params)

    print("\n=== Step 2: Pre-flight check ===")
    preflight_check(sess, params, profile)

    signer_suffix = uuid.uuid4().hex[:5]
    set_param(params, "RagSignerSuffix", signer_suffix)
    save_params(params)
    print(f"  Signer suffix: {signer_suffix}")

    print("\n=== Step 3: Deploy CloudFormation stack ===")
    deploy_stack(profile, region, stack_name)

    print("\n=== Step 4: Upload frontend ===")
    upload_frontend(s3, stack_name, sess)

    if kb_files:
        print("\n=== Step 5: Upload knowledge base files ===")
        upload_kb_files(s3, kb_files, stack_name, sess)

    cfn = sess.client("cloudformation")
    outputs = {o["OutputKey"]: o["OutputValue"]
               for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])}
    print(f"\nDone.")
    print(f"  Chatbot: https://{outputs.get('ChatbotDistributionDomain', '')}")


if __name__ == "__main__":
    main()
