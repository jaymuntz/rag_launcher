# RAG Launcher

Deploys a streaming AI chatbot on AWS using Bedrock Knowledge Base, CloudFront, and Lambda. Answer questions grounded in your own documents.

## Architecture

```
Browser → CloudFront → Lambda@Edge (signer) → rag-node Lambda → Bedrock KB + Claude
```

- **CloudFront** serves the chat frontend from S3 and proxies API requests
- **Lambda@Edge signer** signs requests so rag-node can be invoked securely
- **rag-node** retrieves relevant chunks from the Bedrock Knowledge Base and streams a response token-by-token using Claude
- All infrastructure is defined in `rag_template.yaml` (CloudFormation)

## Prerequisites

### AWS account
- Bedrock model access enabled for **Claude Sonnet** (`us.anthropic.claude-sonnet-4-6`) in `us-east-1`  
  → AWS Console → Amazon Bedrock → Model access → Request access
- IAM permissions to create Lambda, CloudFront, S3, Bedrock, WAF, ACM, IAM, CloudFormation, EventBridge resources
- AWS CLI installed and configured

### Local tools
- Python 3.9+ with `boto3`: `pip install boto3`
- Node.js (only needed if you modify `rag-node/` — the zip is built from checked-in `node_modules`)

## Setup

### 1. Configure AWS credentials

The scripts use the `default` AWS profile by default. To use a named profile, pass `--profile <name>` to both scripts.

```bash
# Verify access
aws sts get-caller-identity
```

### 2. Customize the system prompt

Edit `system_prompt.txt` in the project root. This is loaded by the chatbot at cold-start and sets the assistant's persona and behavior.

```
You are a helpful assistant for Acme Corp. Answer questions about our products clearly and concisely.
```

### 3. Add a favicon (optional)

Place a `favicon.ico` file in the project root. It will be uploaded automatically during deploy.

## Deploy

```bash
python3 deploy.py
```

The script prompts for:

| Prompt | Description |
|---|---|
| **Stack name** | Lowercase letters, numbers, hyphens, max 20 chars. Used as a prefix for all resource names (e.g. `my-chatbot`). |
| **Use custom Route53 domain?** | `Y` to use your own domain, `N` to use the auto-generated CloudFront URL. |
| **Domain** | *(if Y)* Your Route53-hosted domain (e.g. `example.com`). The chatbot will be at `{stack-name}.{domain}`. |
| **Knowledge base files directory** | Local path to a folder of PDFs/documents to ingest. Leave blank to skip — you can upload files manually to S3 later. |

Deploy runs these steps automatically:

1. **ACM certificate** — requests and DNS-validates a TLS cert (skipped if no custom domain)
2. **WAF Web ACL** — creates a rate-limiting firewall for the CloudFront distribution
3. **Lambda packages** — builds and uploads `rag-node.zip` and `rag-signer.zip` to S3
4. **Pre-flight check** — aborts if orphaned resources from a previous deploy would conflict
5. **CloudFormation deploy** — creates all AWS resources
6. **Frontend upload** — uploads `index.html`, `favicon.ico`, and `system_prompt.txt` to S3
7. **Knowledge base sync** — *(if files provided)* uploads documents and triggers Bedrock ingestion
8. **DNS** — creates a Route53 CNAME pointing your domain to CloudFront (skipped if no custom domain)

At the end, the chatbot URL is printed.

> **First deploy takes ~10–15 minutes** — CloudFront distribution propagation and Bedrock KB creation are the slow steps. Subsequent deploys of the same stack are faster.

### Custom domain notes

- Your domain must be hosted in Route53 in the same AWS account
- The ACM certificate is created in `us-east-1` (required for CloudFront) and validated via DNS automatically
- DNS propagation can take a few minutes after deploy completes

### Uploading knowledge base files after deploy

```bash
python3 deploy.py
# Enter the same stack name, then provide the files directory when prompted
```

Or upload directly to S3 and sync from the AWS Console:

```
s3://{stack-name}-rag-chatbot-data/knowledgebase-files/
```

Then go to **Amazon Bedrock → Knowledge bases → {stack-name}-products-kb → Sync**.

## Updating the chatbot

### Change the system prompt
Edit `system_prompt.txt` and re-run `python3 deploy.py` (the frontend upload step re-uploads it).

### Update the chat UI
Edit `index.html` and re-run `python3 deploy.py`.

### Update rag-node Lambda code
```bash
cd rag-node
zip -r ../rag-node.zip index.mjs node_modules/
aws lambda update-function-code \
  --function-name {stack-name}-rag-node \
  --zip-file fileb://../rag-node.zip
```

## Tear down

```bash
python3 teardown.py --stack-name <stack-name>
```

Deletes all resources created by deploy, including the CloudFront distribution, Bedrock knowledge base, Lambda functions, S3 data bucket, WAF ACL, and ACM certificate. Prompts for confirmation before proceeding.

> **Note on Lambda@Edge cleanup:** The `rag-signer` Lambda is replicated globally by CloudFront. If teardown reports it cannot delete the signer function, wait 1–2 hours for AWS to remove the replicas, then run the printed `aws lambda delete-function` command manually.

## S3 bucket layout

| Path | Contents |
|---|---|
| `front/` | Chat frontend (`index.html`, `favicon.ico`) |
| `knowledgebase-files/` | Source documents for Bedrock ingestion |
| `system_prompt.txt` | System prompt loaded by rag-node at cold-start |

## Troubleshooting

**503 from CloudFront on API requests**  
Usually a timeout. The Lambda@Edge signer has a 30-second limit — if Bedrock is slow, requests near that limit may fail. Check CloudWatch logs for `/aws/lambda/us-east-1.{stack-name}-rag-signer-*`.

**"No identity-based policy allows the lambda:InvokeFunction action"**  
The signer role is missing its IAM policy. This can happen if the stack was partially deleted. Re-run `python3 deploy.py` with the same stack name to restore the policy via CloudFormation.

**Chatbot answers "I don't know" for everything**  
The knowledge base may be empty or not yet synced. Check the Bedrock console and trigger a manual sync if needed. Also verify `system_prompt.txt` was uploaded to S3.

**Custom domain not resolving**  
DNS propagation can take a few minutes. If it's been more than 15 minutes, check that the Route53 CNAME record was created and that the ACM certificate status is `ISSUED`.
