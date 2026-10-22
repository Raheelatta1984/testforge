#!/usr/bin/env python3
"""Zero-LLM gate for the market comparison.

Verifies every TestForge capability claim against the repository, prints the
result, and exits non-zero when a claim no longer has evidence. Intended for CI
and for anyone about to quote the matrix.

    python -m tools.market_audit
    python -m tools.market_audit --json
    python -m tools.market_audit --write-report docs/market-position.html
    python -m tools.market_audit --structure-only

No network, no browser, no model. Exit codes: 0 verified, 1 a claim failed,
2 the matrix data itself is structurally invalid.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import market_intel, market_report  # noqa: E402


def _print_human(receipt: dict, matrix: dict) -> None:
    summary = receipt["summary"]
    print("TestForge market claim audit")
    print(f"  matrix reviewed   {matrix['last_reviewed']}  ({matrix['schema']})")
    print(f"  capabilities      {summary['capabilities']}")
    print(f"  claimed           {summary['claimed']}  "
          f"(yes/partial; {summary['not_claimed']} not claimed)")
    print(f"  verified          {summary['verified']}")
    print(f"  failed            {summary['failed']}")
    print(f"  receipt           {receipt['receipt_sha256']}")
    print("")
    failed = [row for row in receipt["results"] if row["rating"] in ("yes", "partial") and not row["verified"]]
    if failed:
        print("FAILED CLAIMS")
        for row in failed:
            print(f"  - {row['id']} ({row['rating']})")
            for probe in row["probes"]:
                if not probe["ok"]:
                    print(f"      {probe['probe'].get('kind')} {probe['probe'].get('path')}"
                          f"{(' · ' + probe['probe']['contains']) if probe['probe'].get('contains') else ''}"
                          f" -> {probe['detail']}")
        print("")
    if receipt["structural_problems"]:
        print("STRUCTURAL PROBLEMS")
        for problem in receipt["structural_problems"]:
            print(f"  - {problem}")
        print("")
    gaps = matrix["gaps"]
    print(f"OPEN GAPS ({len(gaps)}): a compared tool claims these and TestForge does not")
    for gap in gaps[:12]:
        print(f"  - {gap['name']}  [{gap['our_rating']}]  leaders: {', '.join(gap['leaders'][:4])}")
    if len(gaps) > 12:
        print(f"  … {len(gaps) - 12} more (GET /api/market/gaps)")
    print("")
    print("PASS: every claimed capability still has evidence." if summary["passed"]
          else "FAIL: a claim lost its evidence or the matrix is invalid.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print the receipt as JSON")
    parser.add_argument("--structure-only", action="store_true", help="validate the data file, skip probe checks")
    parser.add_argument("--write-report", metavar="PATH", help="write the standalone HTML report to PATH")
    parser.add_argument("--write-markdown", metavar="PATH", help="write the Markdown report to PATH")
    parser.add_argument("--root", metavar="DIR", help="repository root to verify against (defaults to this checkout)")
    args = parser.parse_args(argv)

    problems = market_intel.validate()
    if problems:
        for problem in problems:
            print(f"STRUCTURE: {problem}", file=sys.stderr)
        if args.json:
            print(json.dumps({"passed": False, "structural_problems": problems}, indent=2))
        return 2
    if args.structure_only:
        print(f"PASS: {len(market_intel.capabilities())} capabilities, "
              f"{len(market_intel.tools())} tools, no structural problems.")
        return 0

    root = Path(args.root).resolve() if args.root else None
    code, receipt = market_intel.audit_gate(root)
    matrix = market_intel.matrix(root=root, audit=receipt)
    if args.json:
        print(json.dumps({"receipt": receipt, "summary": matrix["summary"], "gaps": matrix["gaps"],
                          "differentiators": matrix["differentiators"]}, indent=2, ensure_ascii=True))
    else:
        _print_human(receipt, matrix)
    if args.write_report:
        target = Path(args.write_report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(market_report.html_report(matrix), encoding="utf-8")
        print(f"HTML report: {target}")
    if args.write_markdown:
        target = Path(args.write_markdown)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(market_report.markdown_report(matrix), encoding="utf-8")
        print(f"Markdown report: {target}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
