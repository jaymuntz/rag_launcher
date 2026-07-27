#!/usr/bin/env python3
"""Scrape title, air date, and speaker from dealersedge.com product pages.

For each row in products.csv whose Product Url ends in 'nm':
  - Fetches the page and extracts og:title as TITLE.
  - If the Media Url is an MP4 (Webinar), also extracts Air Date and Speaker.

Adds Air Date and Speaker columns if not already present.
Skips rows that already have a TITLE filled in.
"""

import csv
import html as html_module
import random
import re
import time
import urllib.request

CSV_PATH = "products.csv"

COLUMNS = ["TITLE", "SKU", "Product Url", "Member Url", "Media Url", "IMAGE", "Product Type", "Air Date", "Speaker"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def make_headers():
    return {**BASE_HEADERS, "User-Agent": random.choice(USER_AGENTS)}


def fetch(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=make_headers())
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 10 * (2 ** attempt)
                print(f"  [429] rate limited, waiting {wait}s ...")
                time.sleep(wait)
            else:
                raise


def scrape_page(page):
    title = air_date = speaker = ""

    m = re.search(r'og:title[^>]*content=["\']([^"\']+)["\']', page, re.I)
    if not m:
        m = re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']', page, re.I)
    if m:
        raw = m.group(1).strip()
        title = re.sub(r"^DealersEdge\s*\|\s*", "", raw)

    m = re.search(r"Air Date:\s*([^<\n]+)", page, re.I)
    if m:
        air_date = m.group(1).strip()

    m = re.search(r"Featuring:\s*([^<\n]+)", page, re.I)
    if m:
        speaker = m.group(1).strip()

    return html_module.unescape(title), html_module.unescape(air_date), html_module.unescape(speaker)


def is_webinar(media_url):
    return media_url.lower().endswith(".mp4")


def main():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    page_cache = {}
    updated = 0

    for row in rows:
        if row.get("TITLE", "").strip():
            continue
        product_url = row.get("Product Url", "")
        if not product_url.endswith("nm"):
            continue

        if product_url not in page_cache:
            print(f"  Fetching {product_url} ...")
            try:
                page_cache[product_url] = fetch(product_url)
                time.sleep(5)
            except Exception as e:
                print(f"  [error] {product_url}: {e}")
                page_cache[product_url] = ""
                time.sleep(30)

        page = page_cache[product_url]
        title, air_date, speaker = scrape_page(page)

        row["TITLE"] = title
        if is_webinar(row.get("Media Url", "")):
            row["Air Date"] = air_date
            row["Speaker"] = speaker
        updated += 1

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in COLUMNS})

    print(f"Done. Updated {updated} rows.")


if __name__ == "__main__":
    main()
