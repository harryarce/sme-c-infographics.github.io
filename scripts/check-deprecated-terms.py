#!/usr/bin/env python3
"""
Scan HTML pages for deprecated product names / outdated terminology
defined in scripts/terminology.json, and optionally rewrite them.

Idempotent: running twice in a row with --apply produces no changes on
the second run. Rules skip matches that are already adjacent to their
replacement (e.g. "Microsoft Entra ID (previously Azure Active
Directory)" is left alone).

Modes:
    python3 scripts/check-deprecated-terms.py
        # report only; exits 0. Writes reports/deprecated-terms.json
        # and prints a short summary.

    python3 scripts/check-deprecated-terms.py --check
        # exits 1 if any deprecated term is found; does not modify files.
        # Suitable for warn-only CI.

    python3 scripts/check-deprecated-terms.py --apply
        # rewrites matches per terminology.json. By default only rules
        # at severity=high are applied; pass --min-severity medium or
        # --min-severity low to include lower-severity rules.

Redirect stubs (<meta http-equiv="refresh">) and the root index.html are
skipped, matching the pattern used by ensure-tracking.py /
ensure-back-button.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMS_PATH = os.path.join(os.path.dirname(__file__), "terminology.json")
ROOT_INDEX = os.path.join(REPO_ROOT, "index.html")

META_REFRESH_RE = re.compile(
    r'<meta\s+http-equiv=["\']refresh["\']', re.IGNORECASE
)
SKIP_DIRS = {".git", "node_modules", ".github", "reports"}
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def load_rules() -> list[dict[str, Any]]:
    with open(TERMS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    rules = data.get("rules", [])
    for rule in rules:
        rule["compiled"] = re.compile(rule["pattern"])
        if rule.get("severity") not in SEVERITY_ORDER:
            raise ValueError(
                f"rule '{rule.get('id')}' has invalid severity "
                f"'{rule.get('severity')}'; expected one of {sorted(SEVERITY_ORDER)}"
            )
    return rules


def is_already_fixed(content: str, match: re.Match, replacement: str) -> bool:
    """True if the match already sits inside a parenthetical clarification
    right after its replacement, e.g.
        'Microsoft Entra ID (previously Azure Active Directory)'
    We want to leave those alone so history/migration copy still reads.
    """
    start = match.start()
    # Look back up to 80 characters for the replacement phrase.
    window_start = max(0, start - 80)
    preceding = content[window_start:start]
    if replacement in preceding and "(previously" in preceding.lower():
        return True
    # Also skip when the match is inside href="..." or src="..." since
    # URLs are stable identifiers we shouldn't rewrite blindly.
    quote_left = content.rfind('"', 0, start)
    quote_right = content.find('"', match.end())
    if quote_left != -1 and quote_right != -1:
        attr_window = content[max(0, quote_left - 20):quote_left]
        if re.search(r"\b(?:href|src)\s*=\s*$", attr_window, re.IGNORECASE):
            return True
    return False


def scan_file(path: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    if META_REFRESH_RE.search(content):
        return []

    hits: list[dict[str, Any]] = []
    for rule in rules:
        for m in rule["compiled"].finditer(content):
            if is_already_fixed(content, m, rule["replacement"]):
                continue
            line = content.count("\n", 0, m.start()) + 1
            hits.append(
                {
                    "rule_id": rule["id"],
                    "pattern": rule["pattern"],
                    "match": m.group(0),
                    "replacement": rule["replacement"],
                    "severity": rule.get("severity", "medium"),
                    "line": line,
                    "span": [m.start(), m.end()],
                }
            )
    return hits


def apply_fixes(
    path: str, rules: list[dict[str, Any]], min_severity: int
) -> int:
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if META_REFRESH_RE.search(content):
        return 0

    total = 0
    for rule in rules:
        if SEVERITY_ORDER[rule.get("severity", "medium")] < min_severity:
            continue
        pattern: re.Pattern[str] = rule["compiled"]
        replacement: str = rule["replacement"]

        # Walk matches right-to-left so indices stay valid across edits.
        matches = list(pattern.finditer(content))
        for m in reversed(matches):
            if is_already_fixed(content, m, replacement):
                continue
            content = content[: m.start()] + replacement + content[m.end():]
            total += 1

    if total:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return total


def iter_html_files() -> list[str]:
    results: list[str] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fname.lower().endswith(".html"):
                continue
            full = os.path.join(root, fname)
            if os.path.abspath(full) == os.path.abspath(ROOT_INDEX):
                continue
            results.append(full)
    return sorted(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any deprecated term is found; do not modify files.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite deprecated terms per terminology.json.",
    )
    parser.add_argument(
        "--min-severity",
        choices=sorted(SEVERITY_ORDER, key=lambda s: SEVERITY_ORDER[s]),
        default="high",
        help=(
            "Severity threshold for --apply. Defaults to 'high' so "
            "lower-severity rules require an explicit opt-in. Has no "
            "effect in --check or report mode (those always report all "
            "rules)."
        ),
    )
    parser.add_argument(
        "--report",
        default=os.path.join("reports", "deprecated-terms.json"),
        help="Where to write the JSON report (relative to repo root).",
    )
    args = parser.parse_args()

    rules = load_rules()
    files = iter_html_files()
    min_sev = SEVERITY_ORDER[args.min_severity]

    report: dict[str, Any] = {"files": {}, "summary": {}}
    total_hits = 0
    total_applied = 0

    for path in files:
        hits = scan_file(path, rules)
        rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
        if args.apply:
            applied = apply_fixes(path, rules, min_sev)
            total_applied += applied
            # Re-scan post-apply so the report reflects remaining issues
            # (should be zero for rules at/above the severity threshold
            # if our is_already_fixed guard is correct).
            hits = scan_file(path, rules)
        if hits:
            report["files"][rel] = hits
            total_hits += len(hits)

    report["summary"] = {
        "files_scanned": len(files),
        "files_with_hits": len(report["files"]),
        "total_hits": total_hits,
        "total_applied": total_applied,
        "mode": "apply" if args.apply else ("check" if args.check else "report"),
        "min_severity_for_apply": args.min_severity,
    }

    out_path = args.report
    if not os.path.isabs(out_path):
        out_path = os.path.join(REPO_ROOT, out_path)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    summary = report["summary"]
    print(
        f"deprecated-terms: {summary['total_hits']} hit(s) across "
        f"{summary['files_with_hits']}/{summary['files_scanned']} file(s). "
        f"mode={summary['mode']}. report={out_path}"
    )
    if args.apply and total_applied:
        print(f"  applied {total_applied} replacement(s).")

    if args.check and total_hits:
        print("::warning::Deprecated terminology found. See report.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
