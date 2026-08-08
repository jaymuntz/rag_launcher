# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A fully-automated deploy/teardown pipeline for an AI chatbot hosted on AWS. Infrastructure is defined in `rag_template.yaml` (CloudFormation), with two wrapper scripts (`deploy.py` / `teardown.py`) that handle prerequisites and cleanup that CloudFormation alone can't manage.

## Deploying and tearing down

```bash
# Full deploy — prompts for stack name and optional KB files directory
python3 deploy.py [--profile <name>] [--region us-east-1]

# Tear down everything (--stack-name required, prompts for confirmation)
python3 teardown.py --stack-name <name> [--profile <name>]
```

Both scripts default to `--profile default` and `--region us-east-1`.

Deploy runs 5 ordered steps:
1. Build Lambda zips and upload to S3
2. Pre-flight check for orphaned retained resources (aborts with delete commands if found)
3. `aws cloudformation deploy`
4. Upload `index.html`, `favicon.ico`, `system_prompt.txt` to S3
5. Upload KB files and trigger Bedrock sync — skipped if no directory provided

## CloudFormation parameters

`parameters.json` stores all parameter values. Keep this file committed so re-deploys don't recreate those resources.

Key parameters:

| Key | How it's set |
|---|---|
| `AppName` | Prompted interactively; prefix for all resource names |
| `AppSuffix` | Generated once on first deploy, preserved forever; appended to both S3 bucket names |
| `LambdaCodeS3Bucket` | Auto-derived as `{AppName}-rag-lambdas-{AppSuffix}` |
| `RagSignerSuffix` | Regenerated each deploy; appended to the signer function name only |

## Lambda functions

Two Lambdas, each in its own subdirectory:

| Directory | Stack resource | Runtime | Role |
|---|---|---|---|
| `rag-node/` | `{AppName}-rag-node` | Node.js 22.x | Retrieves KB chunks, streams LLM response via ConverseStream |
| `rag-signer/` | `{AppName}-rag-signer-{suffix}` | Node.js 22.x | Lambda@Edge origin-request function; signs and proxies requests to rag-node |

`rag-node/` also contains an unused `lambda_function.py` — the deployed function is `index.mjs`.

### Quick Lambda update (without full redeploy)

```bash
# rag-node
cd rag-node && zip -r ../rag-node.zip index.mjs node_modules/
aws lambda update-function-code --function-name {AppName}-rag-node --zip-file fileb://../rag-node.zip

# rag-signer (deploy.py does the __APP_NAME__ substitution; do this manually for quick updates)
# Edit rag-signer/index.mjs, then:
zip rag-signer.zip rag-signer/index.mjs
aws lambda update-function-code --function-name {AppName}-rag-signer-{suffix} --zip-file fileb://rag-signer.zip
```

## Critical Lambda@Edge architecture

**Why the signer calls Lambda directly (not via Function URL):**
Lambda@Edge runs under a session policy that blocks `lambda:InvokeFunctionUrl`. The signer uses SigV4-signed `POST /2015-03-31/functions/{name}/invocations` (direct Lambda API) instead. The inline IAM policy on the signer role explicitly grants `lambda:InvokeFunction`.

**Build-time `__APP_NAME__` substitution:**
Lambda@Edge functions cannot use environment variables. `rag-signer/index.mjs` contains the literal string `__APP_NAME__` which `deploy.py`'s `_build_signer()` replaces with the actual app name at zip-build time.

**Signer suffix prevents name collisions:**
After teardown, AWS takes 1–2 hours to drain Lambda@Edge replicas before the function can be deleted. A fresh 5-char hex suffix (`RagSignerSuffix`) is generated each deploy so a new deployment can proceed while the old signer function is still waiting to be deleted.

**Streaming response extraction:**
`rag-node` uses `awslambda.streamifyResponse`. When invoked via buffered `InvokeFunction`, the response buffer contains prelude JSON + null bytes + SSE body. `extractSseBody()` in the signer skips the prelude to extract just the SSE data.

**Blacklisted header:**
CloudFront rejects `x-accel-buffering` in Lambda@Edge responses. The signer must not include it; only `rag-node`'s Function URL path sends it (where it's valid).

## S3 bucket layout

Single data bucket: `{AppName}-rag-chatbot-data-{AppSuffix}`

| Prefix | Contents |
|---|---|
| `front/` | `index.html`, `favicon.ico` served by CloudFront |
| `knowledgebase-files/` | Source documents for Bedrock ingestion |
| `system_prompt.txt` | Loaded at rag-node cold-start |

## Retained resources

These have `DeletionPolicy: Retain` in the CloudFormation template, so stack deletion leaves them in place:
`DataBucket`, `ChatbotDistribution`, `RagNodeFunction`, `RagSignerFunction`, `KnowledgeBase`, `KnowledgeBaseDataSource`, `RagNodeFunctionUrl`.

`teardown.py` deletes them after the stack is gone. `deploy.py` step 3 (pre-flight) aborts with manual delete commands if any of them already exist under the expected names.

## Model

`rag-node/index.mjs` uses `us.anthropic.claude-sonnet-4-6` via Bedrock's `ConverseStreamCommand`. Requires Bedrock model access to be enabled in `us-east-1`.
