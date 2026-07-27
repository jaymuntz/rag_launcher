#!/usr/bin/env python3
"""Create .metadata.json files for each file in ./multimedia/ from products.csv."""

import csv
import json
import os

MULTIMEDIA_DIR = os.path.join(os.path.dirname(__file__), "multimedia")
CSV_PATH = os.path.join(os.path.dirname(__file__), "products.csv")

# Map file suffix to candidate Product Types (tried in order)
SUFFIX_TO_PRODUCT_TYPES = {
    ".pdf":     ["Special Report", "book"],
    "_sd.mp4":  ["Webinar"],
    "_hd.mp4":  ["Webinar"],
    ".mp4":     ["Webinar"],
}


def load_csv(path):
    """Return dict (sku, product_type) -> row."""
    lookup = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["SKU"], row["Product Type"])
            lookup[key] = row
    return lookup


def candidate_product_types(filename):
    name = filename.lower()
    for suffix, ptypes in SUFFIX_TO_PRODUCT_TYPES.items():
        if name.endswith(suffix):
            return ptypes
    return []


def make_metadata(row, product_id):
    def attr(value):
        return {"value": {"type": "STRING", "stringValue": value}, "includeForEmbedding": True}

    attrs = {
        "productId":    attr(product_id),
        "productUrl":   attr(row["Product Url"]),
        "memberUrl":    attr(row["Member Url"]),
        "title":        attr(row["TITLE"]),
        "productType":  attr(row["Product Type"]),
    }

    if row.get("IMAGE"):
        attrs["productImage"] = attr(row["IMAGE"])

    if row.get("Air Date"):
        attrs["airDate"] = attr(row["Air Date"])

    if row.get("Speaker"):
        attrs["speaker"] = attr(row["Speaker"])

    return {"metadataAttributes": attrs}


def process_directory(target_dir, csv_lookup, only_codes=None):
    by_code = {}
    for fname in sorted(os.listdir(target_dir)):
        if fname.endswith(".metadata.json"):
            continue
        if not (fname.endswith(".mp4") or fname.endswith(".pdf")):
            continue
        code = fname[:4]
        if only_codes and code not in only_codes:
            continue
        by_code.setdefault(code, []).append(fname)

    print(f"Found {sum(len(v) for v in by_code.values())} media files across {len(by_code)} codes in {os.path.basename(target_dir)}/\n")

    written = errors = 0

    for code in sorted(by_code):
        print(f"--- {code} ({len(by_code[code])} file(s)) ---")
        for fname in by_code[code]:
            ptypes = candidate_product_types(fname)
            if not ptypes:
                print(f"  [skip] unrecognised file type: {fname}")
                errors += 1
                continue

            row = next((csv_lookup.get((code, pt)) for pt in ptypes if csv_lookup.get((code, pt))), None)
            if row is None:
                print(f"  [skip] no CSV row for {code} (tried: {ptypes})")
                errors += 1
                continue

            meta = make_metadata(row, code)
            meta_path = os.path.join(target_dir, fname + ".metadata.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            print(f"  Wrote: {fname}.metadata.json")
            written += 1

        print()

    return written, errors


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", help="Target directory (default: both multimedia and multimedia_bak)")
    parser.add_argument("--codes", help="Comma-separated codes to process (default: all)")
    args = parser.parse_args()

    only_codes = set(args.codes.split(",")) if args.codes else None
    csv_lookup = load_csv(CSV_PATH)
    print(f"Loaded {len(csv_lookup)} rows from products.csv\n")

    dirs = [args.dir] if args.dir else [
        os.path.join(os.path.dirname(__file__), "multimedia"),
        os.path.join(os.path.dirname(__file__), "multimedia_bak"),
    ]

    total_written = total_errors = 0
    for d in dirs:
        if not os.path.isdir(d):
            print(f"Skipping {d} (not found)")
            continue
        print(f"\n{'='*50}")
        print(f"Processing {d}")
        print(f"{'='*50}")
        w, e = process_directory(d, csv_lookup, only_codes)
        total_written += w
        total_errors += e

    print(f"\nDone. {total_written} metadata files written, {total_errors} skipped due to errors.")


if __name__ == "__main__":
    main()
