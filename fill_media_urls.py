#!/usr/bin/env python3
"""Fill Media Url and IMAGE columns in products.csv from text.txt + iplayerhd pages."""

import csv
import json
import os
import re
import urllib.request

CSV_PATH = os.path.join(os.path.dirname(__file__), "products.csv")
TEXT_FILE = os.path.join(os.path.dirname(__file__), "text.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

COLUMNS = ["TITLE", "SKU", "Product Url", "Member Url", "Media Url", "IMAGE", "Product Type"]


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_text_file(path):
    """Return dict: code -> {iplayer_url, pdf_url}"""
    entries = {}
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        if len(line) == 4 and not line.startswith("http"):
            code = line
            info = {"iplayer_url": None, "pdf_url": None}
            j = i + 1
            while j < len(lines) and (len(lines[j]) != 4 or lines[j].startswith("http")):
                l = lines[j]
                if "iplayerhd.com" in l:
                    info["iplayer_url"] = l
                elif l.lower().endswith(".pdf"):
                    info["pdf_url"] = l
                j += 1
            entries[code] = info
            i = j
        else:
            i += 1
    return entries


def scrape_iplayer(url):
    """Return (sd_mp4_url, splash_url) from an iplayerhd share page."""
    html = fetch(url)
    m = re.search(r'var config = (\{.+?\});', html)
    if not m:
        raise ValueError(f"No config JSON found on {url}")
    config = json.loads(m.group(1))
    video = config["videos"][0]

    splash = video.get("splash", "")

    # Pick SD quality (lowest bitrate) for Media Url
    qualities = video.get("qualities", {})
    quality_names = video.get("qualityNames", {})
    sd_url = ""
    # Find the key whose qualityName is "SD", else pick lowest bitrate
    sd_key = None
    for k, label in quality_names.items():
        if label.upper() == "SD":
            sd_key = k
            break
    if sd_key and sd_key in qualities:
        sd_url = qualities[sd_key]["url"]
    elif qualities:
        # Fall back to lowest bitrate by numeric value
        def bitrate_val(k):
            m = re.search(r'\d+', k)
            return int(m.group()) if m else 0
        sd_key = min(qualities.keys(), key=bitrate_val)
        sd_url = qualities[sd_key]["url"]

    return sd_url, splash


def main():
    text_entries = parse_text_file(TEXT_FILE)
    print(f"Parsed {len(text_entries)} codes from text.txt\n")

    # Scrape iplayerhd pages
    media_info = {}  # code -> {sd_url, splash, pdf_url}
    for code, info in sorted(text_entries.items()):
        entry = {"sd_url": "", "splash": "", "pdf_url": info["pdf_url"] or ""}
        if info["iplayer_url"]:
            print(f"Scraping {code}: {info['iplayer_url']}")
            try:
                sd_url, splash = scrape_iplayer(info["iplayer_url"])
                entry["sd_url"] = sd_url
                entry["splash"] = splash
                print(f"  SD: {sd_url}")
                print(f"  Splash: {splash}")
            except Exception as e:
                print(f"  [error] {e}")
        else:
            print(f"{code}: no iplayerhd URL")
        media_info[code] = entry

    # Update CSV
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    updated = 0
    for row in rows:
        sku = row["SKU"]
        ptype = row["Product Type"]
        if sku not in media_info:
            continue
        info = media_info[sku]
        if ptype == "Special Report":
            row["Media Url"] = info["pdf_url"]
            row["IMAGE"] = info["splash"]
            updated += 1
        elif ptype == "Webinar":
            row["Media Url"] = info["sd_url"]
            row["IMAGE"] = info["splash"]
            updated += 1

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nUpdated {updated} rows in products.csv")


if __name__ == "__main__":
    main()
