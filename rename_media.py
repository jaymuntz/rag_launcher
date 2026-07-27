#!/usr/bin/env python3
"""Rename files in ./multimedia/ to four-character codes from text.txt."""

import re
import os
import urllib.request
from urllib.parse import urlparse

MULTIMEDIA_DIR = os.path.join(os.path.dirname(__file__), "multimedia")
TEXT_FILE = os.path.join(os.path.dirname(__file__), "text.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def fetch_url(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def scrape_mp4_filenames(html):
    """Return set of unique mp4 filenames found in page HTML."""
    patterns = [
        r'src=["\']([^"\']*\.mp4[^"\']*)["\']',
        r'file:["\s]*["\']([^"\']*\.mp4[^"\']*)["\']',
        r'"(https?://[^"\'<>\s]+\.mp4[^"\'<>\s]*)"',
        r"'(https?://[^\"'<>\s]+\.mp4[^\"'<>\s]*)'",
        r'(https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?)',
    ]
    filenames = set()
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            url = m.group(1)
            fname = os.path.basename(urlparse(url).path)
            if fname.endswith(".mp4"):
                filenames.add(fname)
    return filenames


def parse_text_file(path):
    """Return list of (code, iplayer_url_or_None, pdf_filename_or_None)."""
    entries = []
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        # Four-char code: letters/digits, not a URL
        if len(line) == 4 and not line.startswith("http"):
            code = line
            iplayer = None
            pdf_fname = None
            j = i + 1
            while j < len(lines) and (len(lines[j]) != 4 or lines[j].startswith("http")):
                l = lines[j]
                if "iplayerhd.com" in l:
                    iplayer = l
                elif l.lower().endswith(".pdf"):
                    pdf_fname = os.path.basename(urlparse(l).path)
                j += 1
            entries.append((code, iplayer, pdf_fname))
            i = j
        else:
            i += 1
    return entries


def file_size(fname):
    path = os.path.join(MULTIMEDIA_DIR, fname)
    return os.path.getsize(path) if os.path.exists(path) else 0


def main():
    entries = parse_text_file(TEXT_FILE)
    print(f"Parsed {len(entries)} entries from text.txt\n")

    renames = []  # (old_path, new_path)

    for code, iplayer_url, pdf_fname in entries:
        print(f"--- {code} ---")

        # PDF rename
        if pdf_fname:
            old = os.path.join(MULTIMEDIA_DIR, pdf_fname)
            new = os.path.join(MULTIMEDIA_DIR, f"{code}.pdf")
            if os.path.exists(old):
                renames.append((old, new))
                print(f"  PDF: {pdf_fname} -> {code}.pdf")
            else:
                print(f"  PDF: [not found] {pdf_fname}")
        else:
            print("  PDF: none")

        # MP4 renames
        if iplayer_url:
            try:
                html = fetch_url(iplayer_url)
            except Exception as e:
                print(f"  [error fetching iPlayer page] {e}")
                continue

            mp4_filenames = scrape_mp4_filenames(html)
            present = [f for f in mp4_filenames if os.path.exists(os.path.join(MULTIMEDIA_DIR, f))]

            if len(present) == 0:
                print("  MP4: [no files found]")
            elif len(present) == 1:
                f = present[0]
                new = os.path.join(MULTIMEDIA_DIR, f"{code}.mp4")
                renames.append((os.path.join(MULTIMEDIA_DIR, f), new))
                print(f"  MP4: {f} -> {code}.mp4 (only one version)")
            else:
                # Sort by size descending: largest = HD
                present.sort(key=file_size, reverse=True)
                hd, sd = present[0], present[1]
                sizes = {f: file_size(f) for f in present}
                new_hd = os.path.join(MULTIMEDIA_DIR, f"{code}_hd.mp4")
                new_sd = os.path.join(MULTIMEDIA_DIR, f"{code}_sd.mp4")
                renames.append((os.path.join(MULTIMEDIA_DIR, hd), new_hd))
                renames.append((os.path.join(MULTIMEDIA_DIR, sd), new_sd))
                print(f"  HD: {hd} ({sizes[hd]:,} bytes) -> {code}_hd.mp4")
                print(f"  SD: {sd} ({sizes[sd]:,} bytes) -> {code}_sd.mp4")
                if len(present) > 2:
                    print(f"  [warning] {len(present)-2} extra MP4(s) ignored: {present[2:]}")
        else:
            print("  MP4: none")

    print(f"\n=== Applying {len(renames)} renames ===")
    errors = 0
    for old, new in renames:
        if os.path.exists(new) and old != new:
            print(f"  [skip] destination exists: {os.path.basename(new)}")
            continue
        try:
            os.rename(old, new)
            print(f"  {os.path.basename(old)} -> {os.path.basename(new)}")
        except Exception as e:
            print(f"  [error] {e}")
            errors += 1

    print(f"\nDone. {len(renames) - errors} files renamed, {errors} errors.")


if __name__ == "__main__":
    main()
