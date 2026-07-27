import json
import os
import boto3
from collections import defaultdict

s3 = boto3.client('s3')

BUCKET_NAME = os.environ["DATA_BUCKET"]
DOCS_PREFIX = "logs/"

def lambda_handler(event, context):
    by_year = defaultdict(list)

    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix=DOCS_PREFIX)

    for page in pages:
        if 'Contents' not in page:
            continue

        for obj in page['Contents']:
            key = obj['Key']

            if key.endswith('/') or not key.endswith('.json'):
                continue

            # Skip the manifest files themselves
            if key.endswith('manifest.json'):
                continue

            # Expected key shape: logs/yyyy/mm/dd/filename.json
            parts = key.split('/')
            if len(parts) < 5:
                print(f"Skipping unexpected path shape: {key}")
                continue

            year = parts[1]

            try:
                response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                file_data = json.loads(response['Body'].read().decode('utf-8'))

                by_year[year].append({
                    "id": file_data.get("request_id", "unknown"),
                    "title": file_data.get("question", "Untitled Document"),
                    "path": key
                })
            except Exception as e:
                print(f"Failed parsing {key}: {str(e)}")

    errors = []
    total = 0

    # Write per-year manifests
    for year, entries in sorted(by_year.items()):
        yearly_key = f"logs/{year}/manifest.json"
        try:
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=yearly_key,
                Body=json.dumps(entries, indent=2),
                ContentType="application/json"
            )
            total += len(entries)
        except Exception as e:
            errors.append(f"{yearly_key}: {str(e)}")

    # Write top-level manifest pointing to each yearly manifest
    top_level = [
        {"year": year, "path": f"logs/{year}/manifest.json"}
        for year in sorted(by_year.keys(), reverse=True)
    ]
    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key="logs/manifest.json",
            Body=json.dumps(top_level, indent=2),
            ContentType="application/json"
        )
    except Exception as e:
        errors.append(f"logs/manifest.json: {str(e)}")

    if errors:
        return {
            'statusCode': 500,
            'body': f'Completed with errors: {"; ".join(errors)}'
        }

    return {
        'statusCode': 200,
        'body': f'Successfully indexed {total} files across {len(by_year)} year(s).'
    }
