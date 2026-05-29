#!/usr/bin/env python3
"""Parity oracle for brief builds — guards the "no content change" constraint.

Snapshot a rendered brief into a stable JSON fingerprint (included article URLs,
item count, dates shown, source/translation markers, and a normalized HTML hash),
then diff two snapshots to catch any content regression introduced by a perf change.

Usage:
    # capture a baseline before a change, and an "after" snapshot following a rebuild
    python scripts/brief_parity.py snapshot <brief-html-url-or-file> baseline.json
    python scripts/brief_parity.py snapshot <brief-html-url-or-file> after.json
    python scripts/brief_parity.py diff baseline.json after.json

A clean diff means the optimization left delivered content untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from html import unescape
from urllib.request import urlopen


def _load(source: str) -> str:
    if source.startswith(("http://", "https://")):
        with urlopen(source, timeout=30) as response:  # noqa: S310 (local trusted URL)
            return response.read().decode("utf-8", "replace")
    with open(source, encoding="utf-8") as handle:
        return handle.read()


def _meta_lines(html: str) -> list[str]:
    return [unescape(m.group(1)).strip() for m in re.finditer(r'class="meta"[^>]*>(.*?)</span>', html)]


def _article_urls(html: str) -> list[str]:
    urls = []
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        u = unescape(m.group(1))
        # skip internal anchors / asset hosts that aren't article links
        if "/api/explore/" in u:
            continue
        urls.append(u)
    # de-dup preserving order
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _titles(html: str) -> list[str]:
    pat = r'class="(?:lead-title|story-title|media-title)"[^>]*>\s*<a[^>]*>(.*?)</a>'
    return [unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() for m in re.finditer(pat, html, re.S)]


def _normalized_hash(html: str) -> str:
    # Drop volatile bits (generated timestamp, token counts) so the hash reflects
    # content, not build metadata.
    stripped = re.sub(r"Generated[^<·]*", "", html)
    stripped = re.sub(r"Token detail:[^<]*", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def snapshot(source: str) -> dict:
    html = _load(source)
    titles = _titles(html)
    meta = _meta_lines(html)
    return {
        "source": source,
        "item_count": len(titles),
        "titles": titles,
        "article_urls": _article_urls(html),
        "meta_lines": meta,
        "undated_count": sum(1 for line in meta if "Date unknown" in line),
        "content_hash": _normalized_hash(html),
    }


def diff(a: dict, b: dict) -> int:
    problems = []
    if a["item_count"] != b["item_count"]:
        problems.append(f"item_count changed: {a['item_count']} -> {b['item_count']}")
    lost = [t for t in a["titles"] if t not in b["titles"]]
    gained = [t for t in b["titles"] if t not in a["titles"]]
    if lost:
        problems.append(f"titles dropped ({len(lost)}): {lost[:8]}")
    if gained:
        problems.append(f"titles added ({len(gained)}): {gained[:8]}")
    if a["undated_count"] != b["undated_count"]:
        problems.append(f"undated_count changed: {a['undated_count']} -> {b['undated_count']}")
    if a["content_hash"] != b["content_hash"]:
        problems.append("normalized content hash changed (inspect meta_lines / dates)")
    if not problems:
        print("PARITY OK — delivered content is unchanged.")
        return 0
    print("PARITY DIFF:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot", help="capture a snapshot to a JSON file")
    snap.add_argument("source", help="brief HTML url or file path")
    snap.add_argument("out", help="output JSON path")
    d = sub.add_parser("diff", help="diff two snapshot JSON files")
    d.add_argument("baseline")
    d.add_argument("after")
    args = parser.parse_args()

    if args.cmd == "snapshot":
        snap_data = snapshot(args.source)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(snap_data, handle, indent=2, ensure_ascii=False)
        print(f"wrote {args.out}: {snap_data['item_count']} items, "
              f"{snap_data['undated_count']} undated, hash {snap_data['content_hash'][:12]}")
        return 0
    return diff(json.load(open(args.baseline, encoding="utf-8")),
                json.load(open(args.after, encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(main())
