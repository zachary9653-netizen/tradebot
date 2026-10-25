"""
Refuse to publish a page that talks about the account.

Run this on anything heading somewhere public. It exits non-zero if the page
mentions a balance, a holding, an order, an account number or a key, so a
future change to the dashboard cannot quietly start leaking.

    python check_public.py public/index.html

Styles and scripts are stripped before the wording checks, because CSS is
full of words like "position" that mean nothing about a portfolio.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns checked against the whole file, markup included.
STRUCTURAL = {
    "an Alpaca account number": r"\bPA[A-Z0-9]{7,}\b",
    "an API key":               r"\b(?:PK|SK)[A-Z0-9]{16,}\b",
    "an order id":              r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                r"[0-9a-f]{4}-[0-9a-f]{12}",
}

# Patterns checked against visible text only.
WORDING = {
    "a cash figure":        r"(?i)\bcash\b",
    "buying power":         r"(?i)buying power",
    "a portfolio value":    r"(?i)\bportfolio\b",
    "an account value":     r"(?i)account value",
    "a holding":            r"(?i)\b(?:share\(s\)|unrealis|unrealiz|avg )",
    "the orders table":     r"(?i)orders on the account",
    "the run log":          r"(?i)recent scheduled runs",
    "an account section":   r"(?i)\bposition\b",
}


def visible_text(html: str) -> str:
    """Drop styles, scripts and tags, leaving what a reader would see."""
    html = re.sub(r"<style\b.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)


def check(path: Path) -> int:
    page = path.read_text(encoding="utf-8")
    text = visible_text(page)
    problems = []

    for label, pattern in {**STRUCTURAL}.items():
        for hit in re.findall(pattern, page):
            problems.append((label, hit if isinstance(hit, str) else hit[0]))

    for label, pattern in WORDING.items():
        for hit in re.findall(pattern, text):
            problems.append((label, hit if isinstance(hit, str) else hit[0]))

    print(f"Checking {path}  ({len(page):,} bytes)")
    if problems:
        print(f"\nFAIL - {len(problems)} thing(s) that should not be public:\n")
        for label, hit in problems:
            print(f"  {label}: {hit!r}")
        print("\nDo not publish this page.")
        return 1

    for label in (*STRUCTURAL, *WORDING):
        print(f"  clean  {label}")
    print("\nPASS - nothing account-related. Safe to publish.")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "public/index.html")
    if not target.exists():
        sys.exit(f"No such file: {target}")
    raise SystemExit(check(target))
