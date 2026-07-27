import hashlib
import json
import logging
import os
import urllib.request
import boto3
from google.oauth2.service_account import Credentials
import gspread

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
GOOGLE_SHEETS_SECRET_ARN = os.environ["GOOGLE_SHEETS_SECRET_ARN"]
SPREADSHEET_ID = "1nZqaZoXqtv2TRrSqg_VOlXJWEmO05UQiSwDviY4Ieso"
S3_BUCKET = os.environ["DATA_BUCKET"]
S3_PREFIX = "product-files/"
SHEET_HASH_KEY = f"{S3_PREFIX}.sheet_hash"
REGION = "us-east-1"
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_ID = os.environ["DATA_SOURCE_ID"]

PRODUCT_TYPE_EXT = {
    "special report": ".pdf",
    "webinar": ".mp4",
    "book": ".pdf",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_sheet_rows():
    sm = boto3.client("secretsmanager", region_name=REGION)
    secret = json.loads(sm.get_secret_value(SecretId=GOOGLE_SHEETS_SECRET_ARN)["SecretString"])
    creds = Credentials.from_service_account_info(secret, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).sheet1.get_all_records()


def s3_object_exists(s3, key):
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=key, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def stream_to_s3(s3, url, s3_key):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        s3.upload_fileobj(resp, S3_BUCKET, s3_key)


def make_metadata(row, sku):
    def attr(value):
        return {"value": {"type": "STRING", "stringValue": value}, "includeForEmbedding": True}

    attrs = {
        "productId":   attr(sku),
        "productUrl":  attr(row["Product Url"]),
        "memberUrl":   attr(row["Member Url"]),
        "title":       attr(row["TITLE"]),
        "productType": attr(row["Product Type"]),
    }
    if row.get("IMAGE"):
        attrs["productImage"] = attr(row["IMAGE"])
    if row.get("Air Date"):
        attrs["airDate"] = attr(row["Air Date"])
    if row.get("Speaker"):
        attrs["speaker"] = attr(row["Speaker"])

    return {"metadataAttributes": attrs}


def hash_rows(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def get_stored_hash(s3):
    try:
        return s3.get_object(Bucket=S3_BUCKET, Key=SHEET_HASH_KEY)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return None


def store_hash(s3, digest):
    s3.put_object(Bucket=S3_BUCKET, Key=SHEET_HASH_KEY, Body=digest.encode(), ContentType="text/plain")


def lambda_handler(event, context):
    s3 = boto3.client("s3", region_name=REGION)

    rows = get_sheet_rows()
    current_hash = hash_rows(rows)
    if current_hash == get_stored_hash(s3):
        return {"statusCode": 200, "body": json.dumps({"message": "no changes detected"})}

    targets = [
        r for r in rows
        if r["Product Type"].lower() in PRODUCT_TYPE_EXT and r["Media Url"]
    ]

    downloaded, skipped, errors = [], [], []

    for row in targets:
        sku = row["SKU"]
        ext = PRODUCT_TYPE_EXT[row["Product Type"].lower()]
        filename = f"{sku}{ext}"
        s3_key = f"{S3_PREFIX}{filename}"
        meta_key = f"{s3_key}.metadata.json"

        if s3_object_exists(s3, s3_key):
            skipped.append(filename)
            logger.info("SKIPPED %s — already exists in S3", filename)
            continue

        try:
            stream_to_s3(s3, row["Media Url"], s3_key)
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=meta_key,
                Body=json.dumps(make_metadata(row, sku), ensure_ascii=False, indent=2),
                ContentType="application/json",
            )
            downloaded.append(filename)
            logger.info("DOWNLOADED %s — %s", filename, row["Media Url"])
            if len(downloaded) >= 5:
                break
        except Exception as e:
            errors.append({"file": filename, "error": str(e)})
            logger.error("ERROR %s — %s", filename, e)

    if downloaded:
        bedrock = boto3.client("bedrock-agent", region_name=REGION)
        bedrock.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )

    if len(downloaded) < 5:
        store_hash(s3, current_hash)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "downloaded": len(downloaded),
            "skipped": len(skipped),
            "errors": len(errors),
            "details": {"downloaded": downloaded, "skipped": skipped, "errors": errors},
        }),
    }
