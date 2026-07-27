#!/bin/bash
set -e

FUNCTION_NAME=$(cat .function-name)
REGION="us-east-1"
ZIP="deployment.zip"

rm -f "$ZIP"

# Package dependencies + handler into a single zip
cd package && zip -r -q "../$ZIP" . && cd ..
zip -q "$ZIP" lambda_function.py

echo "Deploying $ZIP to Lambda function: $FUNCTION_NAME"
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file "fileb://$ZIP" \
  --region "$REGION" \
  --profile dealersedge-ai

echo "Done."
