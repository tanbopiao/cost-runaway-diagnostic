#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for cost_diagnose.py: synthetic fixtures under /tmp, two runs,
--compare round-trip, ID stability. Run: python test_cost_diag.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost_diagnose.py")
PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("PASS  %s" % name)
    else:
        FAIL.append(name)
        print("FAIL  %s  %s" % (name, detail))


def run_tool(project, outdir, extra=()):
    env = dict(os.environ)
    # simulate an installed model client so check_tracking sees one
    env["_TEST_PIP_HINT"] = "openai"
    r = subprocess.run([sys.executable, TOOL, project, "--output", outdir, *extra],
                       capture_output=True, text=True, env=env)
    return r


def make_fixture(root):
    os.makedirs(root, exist_ok=True)
    # unbounded retry loop calling the API, no max-retry guard
    with open(os.path.join(root, "agent_loop.py"), "w") as f:
        f.write(
            "import openai\n"
            "def run(prompt):\n"
            "    while True:\n"
            "        resp = openai.ChatCompletion.create(model='gpt-4', messages=[{'role':'user','content':prompt}])\n"
            "        if resp:\n"
            "            return resp\n"
        )
    # a guarded retry loop (should NOT be flagged)
    with open(os.path.join(root, "safe_retry.py"), "w") as f:
        f.write(
            "import openai\n"
            "from tenacity import retry, stop_after_attempt\n"
            "@retry(stop=stop_after_attempt(3))\n"
            "def call(prompt):\n"
            "    return openai.ChatCompletion.create(\n"
            "        model='gpt-4o-mini', max_tokens=500,\n"
            "        messages=[{'role':'user','content':prompt}])\n"
        )
    # config with budget hint (second run only, to drive --compare diff)
    with open(os.path.join(root, "config.py"), "w") as f:
        f.write("MONTHLY_CAP = 500\n")
    # expensive model with no fallback hint
    with open(os.path.join(root, "router.json"), "w") as f:
        json.dump({"primary_model": "gpt-4o"}, f)


def main():
    base = tempfile.mkdtemp(prefix="cost_diag_test_", dir="/tmp")
    proj = os.path.join(base, "proj")
    make_fixture(proj)

    # Run 1
    r1dir = os.path.join(base, "r1")
    r1 = run_tool(proj, r1dir)
    check("run1 exit 0", r1.returncode == 0, r1.stderr[-200:])
    rep1 = json.load(open(os.path.join(r1dir, "cost_diagnostic_report.json")))
    ids1 = [f["id"] for f in rep1["findings"]]

    check("unbounded retry flagged",
          any(i.startswith("COST-UNBOUNDED-RETRY:agent_loop") for i in ids1),
          str(ids1))
    check("guarded retry not flagged",
          not any("safe_retry" in i for i in ids1), str(ids1))
    check("no tracking flagged", "COST-NO-TRACKING" in ids1, str(ids1))
    check("no budget flagged", "COST-NO-BUDGET" in ids1, str(ids1))
    check("max tokens flagged", "COST-NO-MAX-TOKENS" in ids1, str(ids1))
    check("expensive model flagged", "COST-EXPENSIVE-MODEL" in ids1, str(ids1))
    check("all findings have hours",
          all("self_fix_hours" in f for f in rep1["findings"]))
    check("tool version", rep1["tool_version"] == "1.0.0")
    check("schema version", rep1["schema_version"] == 2)

    # Run 2 with --compare against run 1 (same fixture: all persisting)
    r2dir = os.path.join(base, "r2")
    r2 = run_tool(proj, r2dir, extra=("--compare",
                                      os.path.join(r1dir, "cost_diagnostic_report.json")))
    check("run2 exit 0", r2.returncode == 0, r2.stderr[-200:])
    rep2 = json.load(open(os.path.join(r2dir, "cost_diagnostic_report.json")))
    cmp2 = rep2["compare"]
    check("compare round-trip: 0 new", len(cmp2["new"]) == 0, str(len(cmp2["new"])))
    check("compare round-trip: 0 resolved", len(cmp2["resolved"]) == 0)
    check("compare round-trip: all persisting",
          len(cmp2["persisting"]) == len(rep1["findings"]))
    check("ID stability across runs",
          sorted(i for i in ids1) == sorted(f["id"] for f in rep2["findings"]))

    # Run 3: remove the unbounded loop + add tracking lib hint -> resolved diff
    os.remove(os.path.join(proj, "agent_loop.py"))
    r3dir = os.path.join(base, "r3")
    r3 = run_tool(proj, r3dir, extra=("--compare",
                                      os.path.join(r1dir, "cost_diagnostic_report.json")))
    rep3 = json.load(open(os.path.join(r3dir, "cost_diagnostic_report.json")))
    cmp3 = rep3["compare"]
    check("resolved diff detects fix",
          any(i.startswith("COST-UNBOUNDED-RETRY") for i in
              [r["id"] for r in cmp3["resolved"]]),
          str([r["id"] for r in cmp3["resolved"]]))
    check("summary high decreased",
          rep3["summary"]["high"] < rep1["summary"]["high"],
          "%s -> %s" % (rep1["summary"]["high"], rep3["summary"]["high"]))

    # Markdown renders compare section
    md = open(os.path.join(r3dir, "cost_diagnostic_report.md")).read()
    check("markdown has compare section", "Comparison with previous report" in md)

    shutil.rmtree(base, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
