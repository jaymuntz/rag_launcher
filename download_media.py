#!/usr/bin/env python3
"""Download Special Report PDFs and Webinar MP4s from media URLs in products.csv."""

import csv
import os
import urllib.request
import urllib.error

MULTIMEDIA_DIR = os.path.join(os.path.dirname(__file__), "multimedia")
CSV_PATH = os.path.join(os.path.dirname(__file__), "products.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

PRODUCT_TYPE_EXT = {
    "Special Report": ".pdf",
    "Webinar": "_sd.mp4",
    "book": ".pdf",
}


def download_file(url, dest_path):
    if os.path.exists(dest_path):
        print(f"  [skip] already exists: {os.path.basename(dest_path)}")
        return True
    print(f"  Downloading -> {os.path.basename(dest_path)}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(dest_path, "wb") as f:
            while chunk := resp.read(65536):
                f.write(chunk)
        return True
    except urllib.error.HTTPError as e:
        print(f"  [error] HTTP {e.code}: {url}")
        return False
    except Exception as e:
        print(f"  [error] {e}: {url}")
        return False


def main():
    os.makedirs(MULTIMEDIA_DIR, exist_ok=True)

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    targets = [
        r for r in rows
        if r["Product Type"] in PRODUCT_TYPE_EXT and r["Media Url"]
    ]

    print(f"Found {len(targets)} downloadable rows in products.csv\n")

    ok = errors = skipped = 0
    for row in targets:
        sku = row["SKU"]
        url = row["Media Url"]
        ext = PRODUCT_TYPE_EXT[row["Product Type"]]
        dest = os.path.join(MULTIMEDIA_DIR, f"{sku}{ext}")
        print(f"{sku} ({row['Product Type']}): {url}")
        result = download_file(url, dest)
        if result:
            if os.path.exists(dest):
                ok += 1
            else:
                skipped += 1
        else:
            errors += 1

    print(f"\nDone. {ok} downloaded, {skipped} skipped, {errors} errors.")


if __name__ == "__main__":
    main()
