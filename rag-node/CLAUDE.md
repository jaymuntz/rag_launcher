# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This directory contains two Lambda functions that power the DealersEdge marketing chatbot:

- **`index.mjs`** — `{AppName}-rag-node`: Node.js streaming function. Accepts a user question via HTTP POST, retrieves KB chunks via Bedrock, streams a response token-by-token using `ConverseStream`, and writes an audit log to S3. Exposed via a Lambda Function URL with response streaming.
- **`lambda_function.py`** — `{AppName}-rag`: Python function. Accepts a user question via HTTP POST, uses `agentic_retrieve_stream` for retrieval and generation, inserts inline citation markers, and returns a complete JSON response. Invoked via API Gateway.

Both functions read `KNOWLEDGE_BASE_ID` and `DATA_BUCKET` from environment variables (set by CloudFormation).

## Deployment

### Node function (`{AppName}-rag-node`)

```bash
zip -r function.zip index.mjs node_modules/
aws lambda update-function-code --function-name {AppName}-rag-node --zip-file fileb://function.zip
```

### Python function (`{AppName}-rag`)

```bash
zip function.zip lambda_function.py
aws lambda update-function-code --function-name {AppName}-rag --zip-file fileb://function.zip
```

## AWS resources

- **Knowledge Base**: from `KNOWLEDGE_BASE_ID` env var (Bedrock)
- **Guardrail** (Python only): `8kow3zjhftvg` v1 — currently disabled (commented out) for both input and output
- **S3 bucket**: from `DATA_BUCKET` env var — stores `system_prompt.txt` (cold-start load) and `logs/YYYY/MM/DD/<request_id>.json` audit logs

## Architecture — Node (`index.mjs`)

Uses a two-step approach: `RetrieveCommand` to fetch KB chunks, then `ConverseStreamCommand` to stream the answer. Citation markers (`[1]`, `[2]`, etc.) are included in the prompt and the model is instructed to cite inline. Sources are sent in the final `done` SSE event.

## Architecture — Python (`lambda_function.py`)

Uses `bedrock_agent_runtime.agentic_retrieve_stream`, which streams events containing both retrieved results and a generated response with citations. Citation markers are inserted into the answer text at the character positions from `citations[*].endIndex`, applied in reverse order to preserve positions.

## Re-enabling guardrails (Python only)

The `apply_guardrail` function is implemented but both call sites (input check before retrieval, output check before return) are commented out. Uncomment those blocks to re-enable content filtering.
