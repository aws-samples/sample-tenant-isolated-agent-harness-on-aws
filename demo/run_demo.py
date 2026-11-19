# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Command-line demo for the deployed split harness.

    python -m demo.run_demo --profile <profile>              # set up, three turns, all checks
    python -m demo.run_demo --profile <profile> --checks-only
    python -m demo.run_demo --profile <profile> --json       # machine-readable output

Writes the results to .build/evidence/live-demo.md (git-ignored) and exits 1 if any check fails.
The committed docs/evidence/*.md files are example output from an earlier run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import checks  # noqa: E402
from demo.client import STACK_NAME, Demo  # noqa: E402

EVIDENCE = ROOT / ".build" / "evidence" / "live-demo.md"
TURNS = [
    ("anycompany-plumbing", "Which jobs are open?"),
    ("anycompany-plumbing", "uploads/prices.csv is our supplier price list. It has a header row and the columns sku, description, unit_price_aud. Add up the unit_price_aud column and tell me the total."),
    ("example-corp-electrical", "Create a quote for job E1: switchboard upgrade, $640."),
]


def path_of(trace: list[str] | None) -> str:
    if trace is None:
        return "unknown"
    if "execute_process" in trace:
        return "runtime session started"
    return "Gateway tools only" if trace else "model only"


def run_turns(demo: Demo, log) -> list[dict]:
    results = []
    for tenant, message in TURNS:
        started = time.time()
        status, body = demo.submit(tenant, message)
        final = demo.wait(tenant, body["requestId"]) if status == 202 else {"state": f"HTTP {status}", **body}
        row = {"tenant": tenant, "message": message, "state": final.get("state"),
               "path": path_of(final.get("toolTrace")), "tools": final.get("toolTrace"),
               "result": (final.get("result") or final.get("error") or final.get("message") or "")[:300],
               "seconds": round(time.time() - started, 1)}
        results.append(row)
        log(f"\n[{tenant}] {message}\n  {row['state']} in {row['seconds']}s, {row['path']} {row['tools'] or ''}\n"
            f"  {row['result']}")
    return results


def write_evidence(region: str, stack: str, turns: list[dict], results: list[dict]) -> None:
    def cell(value) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = ["# Live demo evidence", "",
             f"Run at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} against stack `{stack}` in "
             f"{region}, by `python -m demo.run_demo`.", ""]
    if turns:
        lines += ["## Turns", "", "| Tenant | Message | State | Path | Seconds | Result (first 300 characters) |",
                  "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {cell(t['tenant'])} | {cell(t['message'])} | {cell(t['state'])} | {cell(t['path'])} | "
                  f"{t['seconds']} | {cell(t['result'])} |" for t in turns]
        lines.append("")
    passed = sum(r["passed"] for r in results)
    lines += ["## Isolation checks", "", f"{passed} of {len(results)} passed.", "",
              "| Check | Requirement | Expected | Observed | Result |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {cell(r['title'])} | {cell(r['requirement'])} | {cell(r['expected'])} | {cell(r['observed'])} | "
              f"{'PASS' if r['passed'] else 'FAIL'} |" for r in results]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--stack", default=STACK_NAME)
    parser.add_argument("--checks-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    log = (lambda *_: None) if args.json else (lambda text: print(text, flush=True))

    demo = Demo(args.profile, args.region, args.stack)
    log("Setting up demo tenants...")
    for seeded in demo.setup():
        log(f"  {seeded['tenant']}: {seeded['jobs']} jobs, {seeded['upload']}")
    turns = [] if args.checks_only else run_turns(demo, log)
    log("\nIsolation checks")
    results = []
    for check_id in checks.CHECKS:
        result = checks.run(demo, check_id)
        results.append(result)
        log(f"  {'PASS' if result['passed'] else 'FAIL'}  {result['title']}  ({result['seconds']}s)\n"
            f"        {result['observed']}")
    write_evidence(args.region, args.stack, turns, results)
    passed = sum(r["passed"] for r in results)
    if args.json:
        print(json.dumps({"turns": turns, "checks": results, "passed": passed, "total": len(results)}, indent=2))
    else:
        log(f"\n{passed} of {len(results)} checks passed. Evidence: {EVIDENCE.relative_to(ROOT)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
