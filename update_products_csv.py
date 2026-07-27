#!/usr/bin/env python3
"""Update products.csv: add Product Type column and append multimedia entries."""

import csv
import json
import os

MULTIMEDIA_DIR = os.path.join(os.path.dirname(__file__), "multimedia")
CSV_PATH = os.path.join(os.path.dirname(__file__), "products.csv")

COLUMNS = ["TITLE", "SKU", "Product Url", "Member Url", "Media Url", "IMAGE", "Product Type"]

FILE_TYPE_TO_PRODUCT_TYPE = {
    "pdf": "Special Report",
    "video_sd": "Webinar",
    "video_hd": "Webinar",
    "video": "Webinar",
}


def read_existing_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows


def get_attr(attrs, key):
    entry = attrs.get(key)
    if entry:
        return entry["value"].get("stringValue", "")
    return ""


def load_multimedia_rows():
    rows = []
    for fname in sorted(os.listdir(MULTIMEDIA_DIR)):
        if not fname.endswith(".metadata.json"):
            continue
        path = os.path.join(MULTIMEDIA_DIR, fname)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        attrs = data["metadataAttributes"]
        file_type = get_attr(attrs, "fileType")
        product_type = FILE_TYPE_TO_PRODUCT_TYPE.get(file_type)
        if product_type is None:
            print(f"  [skipped] unknown fileType '{file_type}' in {fname}")
            continue
        rows.append({
            "TITLE": get_attr(attrs, "title"),
            "SKU": get_attr(attrs, "productId"),
            "Product Url": get_attr(attrs, "sourceUrl"),
            "Member Url": "",
            "Media Url": "",
            "IMAGE": "",
            "Product Type": product_type,
        })
    return rows


def main():
    existing = read_existing_rows()
    multimedia = load_multimedia_rows()

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in existing:
            writer.writerow({
                "TITLE": row.get("TITLE", ""),
                "SKU": row.get("SKU", ""),
                "Product Url": row.get("Product Url", ""),
                "Member Url": row.get("Member Url", ""),
                "Media Url": row.get("Media Url", ""),
                "IMAGE": row.get("IMAGE", ""),
                "Product Type": "book",
            })
        for row in multimedia:
            writer.writerow(row)

    print(f"Done. {len(existing)} existing rows (type=book) + {len(multimedia)} multimedia rows added.")
    print(f"Total rows: {len(existing) + len(multimedia)}")


if __name__ == "__main__":
    main()
