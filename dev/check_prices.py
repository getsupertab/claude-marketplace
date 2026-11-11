#!/usr/bin/env python3
"""Compares TokenTab's price table with the published one.

Reads the model pricing table on platform.claude.com/docs/en/about-claude/pricing (its
Markdown copy) and plugins/tokentab/tokentab/prices.json, and prints every model the
page lists that the table lacks and every rate that differs. Exit 1 when there is any.

  python3 dev/check_prices.py              # fetch the page
  python3 dev/check_prices.py --page FILE  # a saved copy of the Markdown
"""
import json
import os
import re
import sys
import urllib.request

URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
PRICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "tokentab", "tokentab", "prices.json")
COLUMNS = {"base input tokens": "input", "5m cache writes": "cache_write_5m", "1h cache writes": "cache_write_1h",
           "cache hits and refreshes": "cache_read", "output tokens": "output"}


def model_id(name):
    """'Claude Mythos 5.2 ([limited availability](...))' -> 'claude-mythos-5-2'; None for a
    row the table does not model (Haiku 5.5 over 100K tokens)."""
    if "over 100,000" in name:
        return None
    name = re.sub(r"\(.*", "", name).strip().lower()
    return re.sub(r"[\s.]+", "-", name)


def published(markdown):
    """model id -> rate key -> USD per MTok, from the first table under '## Model pricing'."""
    section = markdown.split("## Model pricing", 1)[1].split("\n## ", 1)[0]
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.lstrip().startswith("|")]
    keys = [COLUMNS.get(cell.lower()) for cell in rows[0]]
    if sorted(filter(None, keys)) != sorted(COLUMNS.values()):
        raise SystemExit(f"the pricing table's columns changed: {rows[0]}")
    out = {}
    for row in rows[2:]:
        mid = model_id(row[0])
        if mid:
            out[mid] = {key: float(re.search(r"\$([\d.]+)", cell).group(1)) for key, cell in zip(keys, row) if key}
    return out


def compare(page, table):
    """Lines naming each published model the table lacks and each rate that differs. A
    dated id in the table (claude-haiku-4-5-20251001) is held to its base model's row."""
    models, problems = table.get("models", {}), []
    for mid, rates in page.items():
        if mid not in models:
            problems.append(f"missing: {mid} {rates}")
    for mid, rates in models.items():
        want = page.get(re.sub(r"-\d{8}$", "", mid))
        for key, usd in (want or {}).items():
            if abs(rates.get(key, -1) - usd) > 1e-9:
                problems.append(f"differs: {mid} {key} is {rates.get(key)}, published {usd}")
    return problems


def main(argv):
    if "--page" in argv:
        with open(argv[argv.index("--page") + 1], encoding="utf-8") as f:
            markdown = f.read()
    else:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "tokentab-price-check"}), timeout=30) as r:
            markdown = r.read().decode("utf-8")
    with open(PRICES, encoding="utf-8") as f:
        table = json.load(f)
    problems = compare(published(markdown), table)
    print("\n".join(problems) if problems else "prices.json matches the published prices")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
