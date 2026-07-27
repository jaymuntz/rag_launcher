# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AWS Lambda function (`{AppName}-rag-log-indexer`) that indexes chatbot conversation log files stored in S3. It scans `logs/*.json` in the data bucket (from the `DATA_BUCKET` environment variable), extracts metadata from each file, and writes year-level and top-level manifests back to the same bucket.

The function name follows the CloudFormation pattern `{AppName}-rag-log-indexer`.

## Deployment

There is no build step. Deploy by zipping `lambda_function.py` and uploading to AWS Lambda, or use the AWS CLI:

```bash
zip function.zip lambda_function.py
aws lambda update-function-code --function-name {AppName}-rag-log-indexer --zip-file fileb://function.zip
```

To invoke the function directly:

```bash
aws lambda invoke --function-name {AppName}-rag-log-indexer output.json && cat output.json
```

## Architecture

- **Input**: S3 objects at `s3://{DATA_BUCKET}/logs/yyyy/mm/dd/*.json`
- **Expected log file shape**: `{ "request_id": "...", "question": "..." }` (other fields are ignored)
- **Outputs**:
  - `logs/yyyy/manifest.json` per year — array of `{ id, title, path }` for that year's logs
  - `logs/manifest.json` — top-level index, array of `{ year, path }` pointing to each yearly manifest
- **Pagination**: Uses `list_objects_v2` paginator so it handles buckets with more than 1,000 log files correctly
- **IAM**: The Lambda execution role needs `s3:GetObject`, `s3:ListBucket`, and `s3:PutObject` on the target bucket
- **Runtime**: Python 3.x with only stdlib + `boto3` (pre-installed in Lambda)
