#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cost Runaway Diagnostic — local health-check for AI agent cost controls.

Read-only survey of an agent project's cost visibility. Produces a report that
contains NO secret values and NO file contents — only names, counts, versions
and recommendations, so the report is safe to share back for a remediation
proposal.

Usage:
    python cost_diagnose.py [project_dir] [--output DIR] [--json]
    python cost_diagnose.py [project_dir] --compare OLD_REPORT.json
"""
import argparse
import json
import os
import re
import subprocess
import sys

TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = 2

SEV_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Stable finding ID -> self-fix effort baseline (person-hours, rough).
# IDs with a <suffix> part look up by the base ID before the colon.
SELF_FIX_HOURS = {
    "COST-NO-TRACKING": 3,
    "COST-NO-BUDGET": 2,
    "COST-UNBOUNDED-RETRY": 2,
    "COST-NO-MAX-TOKENS": 1,
    "COST-NO-CACHING": 3,
    "COST-EXPENSIVE-MODEL": 2,
    "COST-STACK-OK": 0,
    "COST-NO-STACK": 0,
}


def fix_hours(fid):
    base = fid.split(":", 1)[0]
    return SELF_FIX_HOURS.get(base, 0)


def finding(fid, severity, title, detail="", advice=""):
    return {
        "id": fid,
        "severity": severity,
        "title": title,
        "detail": detail,
        "advice": advice,
        "self_fix_hours": fix_hours(fid),
    }


def safe_read(path, limit=200_000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def walk_files(root, depth=2, max_files=2000):
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            "site-packages", "dist-info", ".cache", "data", "logs"}
    count = 0
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if dirpath.rstrip(os.sep).count(os.sep) - root_depth >= depth:
            dirnames[:] = []
        for name in filenames:
            yield os.path.join(dirpath, name)
            count += 1
            if count >= max_files:
                return


MODEL_CLIENT_PKGS = {
    "openai", "anthropic", "dashscope", "google-generativeai", "litellm",
    "cohere", "mistralai", "groq", "together", "zhipuai", "qianfan",
    "moonshot", "deepseek", "minimax", "sparkai",
}
USAGE_TRACKING_PKGS = {"langfuse", "helicone", "litellm", "openllmetry",
                       "promptlayer", "wandb", "langsmith"}
CACHE_PKGS = {"diskcache", "cachetools", "gptcache", "redis", "aiocache",
              "requests-cache", "filelock"}
BUDGET_ENV_HINTS = ("BUDGET", "MONTHLY_CAP", "SPEND_LIMIT", "COST_LIMIT",
                    "RATE_LIMIT", "QUOTA")
EXPENSIVE_MODEL_RE = re.compile(
    r"\b(o1(-preview|-mini)?|gpt-4o?(-\w+)?|gpt-4\.5|claude-(3|4)-opus|"
    r"claude-opus-\d|gemini-\d\.?-?pro|deepseek-r\d)\b", re.IGNORECASE)
CHEAP_MODEL_HINT = re.compile(
    r"\b(gpt-4o-mini|gpt-3\.5|mini|flash|lite|haiku|cheap|fallback|"
    r"downgrade|small-model)\b", re.IGNORECASE)

RETRY_LOOP_RE = re.compile(
    r"(while\s+True|while\s+not\s+\w+|for\s+attempt|for\s+i\s+in\s+range\s*\(\s*\d{3,})",
    re.IGNORECASE)
MAX_RETRY_RE = re.compile(
    r"(max[_ ]?(retries|attempts|try)|backoff|tenacity|retrying|max_tries)",
    re.IGNORECASE)
API_CALL_RE = re.compile(
    r"(chat\.completions\.create|messages\.create|completions\.create|"
    r"generate_content|ChatCompletion|invoke\(|agenerate|generate\()")
MAX_TOKENS_RE = re.compile(
    r"(max_tokens|max_completion_tokens|maxOutputTokens|max_output_tokens)",
    re.IGNORECASE)


def pip_packages():
    pkgs = set()
    try:
        out = subprocess.run([sys.executable, "-m", "pip", "list", "--format=freeze"],
                             capture_output=True, text=True, timeout=60)
        for line in out.stdout.splitlines():
            if "==" in line:
                pkgs.add(line.split("==", 1)[0].strip().lower())
    except Exception:
        pass
    return pkgs


def check_tracking(pip, project_dir):
    findings, facts = [], {}
    has_client = bool(pip & MODEL_CLIENT_PKGS)
    has_tracking = bool(pip & USAGE_TRACKING_PKGS)
    # self-built usage logging: a file that logs token usage
    usage_log_files = []
    client_import_files = []
    for fp in walk_files(project_dir, depth=2):
        if not fp.endswith((".py", ".js", ".ts")):
            continue
        text = safe_read(fp, 100_000)
        if not text:
            continue
        if re.search(r"(import\s+(openai|anthropic|dashscope|cohere|litellm)|"
                     r"from\s+(openai|anthropic|dashscope|cohere|litellm))", text):
            client_import_files.append(fp)
        if re.search(r"(usage|token)", text, re.IGNORECASE) and \
           re.search(r"(log|record|write|insert|append)", text, re.IGNORECASE):
            usage_log_files.append(fp)
    has_client = has_client or bool(client_import_files)
    facts["model_client_installed"] = has_client
    facts["client_import_files"] = [os.path.relpath(p, project_dir)
                                    for p in client_import_files[:10]]
    facts["usage_tracking_lib"] = sorted(pip & USAGE_TRACKING_PKGS)
    facts["usage_log_files"] = [os.path.relpath(p, project_dir) for p in usage_log_files[:10]]

    if not has_client and not usage_log_files:
        findings.append(finding(
            "COST-NO-STACK", "info",
            "No model client libraries detected",
            "No common model API clients (openai, anthropic, dashscope, ...) "
            "found via pip, and no usage-logging code.",
            "If your agents run elsewhere (another venv, another machine), "
            "run this tool there too — one report per deployment."))
        return findings, facts

    if has_tracking or usage_log_files:
        findings.append(finding(
            "COST-STACK-OK", "info",
            "Usage tracking is in place",
            "Tracking libs: %s; usage-logging files: %d" % (
                ", ".join(sorted(pip & USAGE_TRACKING_PKGS)) or "none",
                len(usage_log_files)),
            "Make sure usage numbers are persisted per call and per workflow, "
            "so cost per business process is visible — not just a monthly bill."))
    else:
        findings.append(finding(
            "COST-NO-TRACKING", "medium",
            "Model clients present but no usage/cost tracking detected",
            "Model clients installed: %s. No usage-tracking library and no "
            "self-built usage logging found." % ", ".join(sorted(pip & MODEL_CLIENT_PKGS)[:6]),
            "Add per-call token accounting (most model APIs return usage in the "
            "response). Persist it per workflow so you can see cost per business "
            "process and catch runaway loops early."))
    return findings, facts


def check_budget(project_dir):
    findings, facts = [], {}
    budget_hits = []
    for name in sorted(os.environ):
        if any(h in name.upper() for h in BUDGET_ENV_HINTS):
            budget_hits.append(("env", name))
    for rel in (".env", ".env.local", ".env.production", "config.ini",
                "settings.py", "config.py", "config.json", "config.yaml"):
        p = os.path.join(project_dir, rel)
        if os.path.isfile(p):
            text = safe_read(p)
            for i, line in enumerate(text.splitlines(), 1):
                if any(h in line.upper() for h in BUDGET_ENV_HINTS):
                    budget_hits.append((rel, "line %d" % i))
    facts["budget_config_hits"] = [{"where": w, "what": t} for w, t in budget_hits[:10]]
    if budget_hits:
        findings.append(finding(
            "COST-NO-BUDGET", "info",
            "Budget/limit configuration detected",
            "%d budget-related setting(s) found (names only)." % len(budget_hits),
            "Verify the limits are enforced in code, not just declared — a cap "
            "nobody reads is a wish, not a control."))
    else:
        findings.append(finding(
            "COST-NO-BUDGET", "medium",
            "No budget or spend-limit configuration found",
            "No BUDGET / MONTHLY_CAP / QUOTA-style settings in environment "
            "variables or common config files.",
            "Set a monthly spend cap and a per-workflow limit, and alert when "
            "either is approached. Without a cap, one runaway loop can burn "
            "the whole month's budget overnight."))
    return findings, facts


def check_retry_loops(project_dir):
    findings, facts = [], {}
    flagged = []
    for fp in walk_files(project_dir, depth=2):
        if not fp.endswith((".py", ".js", ".ts")):
            continue
        text = safe_read(fp, 100_000)
        if not text:
            continue
        has_loop = RETRY_LOOP_RE.search(text)
        if not has_loop:
            continue
        has_api_call = API_CALL_RE.search(text)
        has_guard = MAX_RETRY_RE.search(text)
        if has_api_call and not has_guard:
            flagged.append(os.path.relpath(fp, project_dir))
    facts["unbounded_retry_files"] = flagged
    for rel in flagged:
        findings.append(finding(
            "COST-UNBOUNDED-RETRY:%s" % rel.replace(os.sep, "-"),
            "high",
            "Unbounded retry loop calling model APIs: %s" % rel,
            "The file contains a loop (while True / attempt loop) that calls a "
            "model API, with no max-retry count or backoff library detected. "
            "Content is not included in this report.",
            "Add a max-retry count and exponential backoff (e.g. tenacity). An "
            "unbounded loop against a rate-limited API is the classic way a "
            "quiet afternoon becomes a five-figure bill."))
    if not flagged:
        findings.append(finding(
            "COST-UNBOUNDED-RETRY", "info",
            "No unbounded retry loops detected",
            "Scanned source files with retry loops all have max-retry or "
            "backoff guards.",
            "Keep it that way: every model-API retry path should have a cap."))
    return findings, facts


def check_max_tokens(project_dir):
    findings, facts = [], {}
    call_files, guarded_files = [], []
    for fp in walk_files(project_dir, depth=2):
        if not fp.endswith((".py", ".js", ".ts")):
            continue
        text = safe_read(fp, 100_000)
        if not text:
            continue
        if API_CALL_RE.search(text):
            call_files.append(fp)
            if MAX_TOKENS_RE.search(text):
                guarded_files.append(fp)
    facts["files_with_model_calls"] = len(call_files)
    facts["files_setting_max_tokens"] = len(guarded_files)
    if call_files and len(guarded_files) < len(call_files):
        findings.append(finding(
            "COST-NO-MAX-TOKENS", "medium",
            "%d of %d model-calling files set no max_tokens cap" % (
                len(call_files) - len(guarded_files), len(call_files)),
            "Files calling model APIs without a visible max_tokens / "
            "max_output_tokens setting.",
            "Set a per-call output cap. Unbounded generation on a chatty "
            "workflow multiplies token spend with no upside."))
    elif call_files:
        findings.append(finding(
            "COST-NO-MAX-TOKENS", "info",
            "All model-calling files set an output cap",
            "%d file(s) with model calls, all setting max_tokens." % len(call_files),
            "Review the caps periodically — a cap set once and forgotten can "
            "be silently wrong after a model upgrade."))
    return findings, facts


def check_caching(project_dir, pip):
    findings, facts = [], {}
    has_cache_lib = bool(pip & CACHE_PKGS)
    cache_files = [fp for fp in walk_files(project_dir, depth=2)
                   if os.path.basename(fp).startswith(("cache", ".cache"))
                   or fp.endswith(".cache.json")]
    facts["cache_libs"] = sorted(pip & CACHE_PKGS)
    facts["cache_files"] = [os.path.relpath(p, project_dir) for p in cache_files[:10]]
    if has_cache_lib or cache_files:
        findings.append(finding(
            "COST-NO-CACHING", "info",
            "Caching is present",
            "Cache libs: %s; cache-like files: %d" % (
                ", ".join(sorted(pip & CACHE_PKGS)) or "none", len(cache_files)),
            "Check the hit rate — a cache nobody measures is just extra memory."))
    else:
        findings.append(finding(
            "COST-NO-CACHING", "low",
            "No caching detected for repeated model calls",
            "No cache libraries and no cache-like files found, while the "
            "project calls model APIs.",
            "If the same prompts recur (templates, fixed contexts), add a "
            "response cache. Even a 30% hit rate cuts the bill by a third."))
    return findings, facts


def check_model_choice(project_dir):
    findings, facts = [], {}
    expensive_hits = []
    for fp in walk_files(project_dir, depth=2):
        if not fp.endswith((".py", ".js", ".ts", ".json", ".yaml", ".yml",
                            ".env", ".sh", ".md")):
            continue
        text = safe_read(fp, 100_000)
        if not text:
            continue
        m = EXPENSIVE_MODEL_RE.search(text)
        if m and not CHEAP_MODEL_HINT.search(text):
            expensive_hits.append((os.path.relpath(fp, project_dir), m.group(1)))
    facts["expensive_model_refs"] = [{"file": f, "model": m} for f, m in expensive_hits[:10]]
    if expensive_hits:
        files = ", ".join(sorted({f for f, _ in expensive_hits})[:5])
        findings.append(finding(
            "COST-EXPENSIVE-MODEL", "low",
            "Flagship model referenced with no cheaper fallback path",
            "Files: %s (model names only, no content)." % files,
            "Route easy tasks to a cheaper model and keep the flagship for the "
            "hard ones. A simple difficulty router often cuts spend 50%+ with "
            "no quality loss on routine calls."))
    else:
        findings.append(finding(
            "COST-EXPENSIVE-MODEL", "info",
            "No hardcoded flagship models without fallback",
            "No expensive-model references found without a cheaper-path hint.",
            "Keep a routing policy: cheap model first, escalate on failure or "
            "difficulty."))
    return findings, facts


def build_report(project_dir):
    pip = pip_packages()
    all_findings = []
    facts = {"generated_at": __import__("datetime").datetime.now().isoformat(),
             "project_dir": os.path.abspath(project_dir)}
    for fn in (check_tracking, check_budget, check_retry_loops,
               check_max_tokens, check_caching, check_model_choice):
        if fn is check_tracking:
            f, fa = fn(pip, project_dir)
        elif fn is check_caching:
            f, fa = fn(project_dir, pip)
        else:
            f, fa = fn(project_dir)
        all_findings.extend(f)
        facts.update(fa)

    all_findings.sort(key=lambda x: (SEV_RANK.get(x["severity"], 9), x.get("id", "")))
    summary = {s: sum(1 for f in all_findings if f["severity"] == s)
               for s in ("high", "medium", "low", "info")}
    return {
        "report_type": "cost_runaway_diagnostic",
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generated_at": facts["generated_at"],
        "summary": summary,
        "findings": all_findings,
        "facts": facts,
        "note": ("This report contains NO secret values and NO file contents — "
                 "only names, counts, versions and recommendations. It is safe "
                 "to share back for a cost-remediation proposal."),
    }


def compare_reports(current, old_path):
    """Diff two diagnostic reports by stable finding ID: new/resolved/persisting."""
    with open(old_path, encoding="utf-8") as f:
        old = json.load(f)

    def idmap(rep):
        return {f["id"]: f for f in rep.get("findings", []) if f.get("id")}

    cur, oldm = idmap(current), idmap(old)
    new_ids = sorted(set(cur) - set(oldm))
    resolved_ids = sorted(set(oldm) - set(cur))
    persist_ids = sorted(set(cur) & set(oldm))
    sev_changed = [{"id": i, "from": oldm[i]["severity"], "to": cur[i]["severity"]}
                   for i in persist_ids if oldm[i]["severity"] != cur[i]["severity"]]
    return {
        "compared_against": os.path.abspath(old_path),
        "old_generated_at": old.get("generated_at"),
        "new": [cur[i] for i in new_ids],
        "resolved": [{"id": i, "severity": oldm[i]["severity"], "title": oldm[i]["title"]}
                     for i in resolved_ids],
        "persisting": [cur[i] for i in persist_ids],
        "severity_changed": sev_changed,
    }


def render_markdown(report):
    lines = [
        "# Cost Runaway Diagnostic Report",
        "",
        "Generated: %s  |  Project: %s  |  Tool: v%s (schema v%s)" % (
            report["generated_at"], report["facts"].get("project_dir", ""),
            report.get("tool_version", "?"), report.get("schema_version", "?")),
        "",
        "**Summary:** %d high · %d medium · %d low · %d info" % (
            report["summary"]["high"], report["summary"]["medium"],
            report["summary"]["low"], report["summary"]["info"]),
        "",
        "> %s" % report["note"],
        "",
    ]
    for f in report["findings"]:
        hours = f.get("self_fix_hours")
        hours_txt = " · self-fix ~%s h" % hours if hours else ""
        lines.append("### [%s] %s" % (f["severity"].upper(), f["title"]))
        lines.append("- Finding ID: `%s`%s" % (f.get("id", "?"), hours_txt))
        if f["detail"]:
            lines.append("- %s" % f["detail"])
        if f["advice"]:
            lines.append("- **Advice:** %s" % f["advice"])
        lines.append("")
    cmp_ = report.get("compare")
    if cmp_:
        lines += ["## Comparison with previous report", "",
                  "- Baseline: `%s` (generated %s)" % (
                      cmp_["compared_against"], cmp_.get("old_generated_at") or "unknown"), ""]
        if cmp_["resolved"]:
            lines.append("**Resolved:**")
            for r in cmp_["resolved"]:
                lines.append("- RESOLVED `%s` %s" % (r["id"], r["title"]))
            lines.append("")
        if cmp_["new"]:
            lines.append("**New:**")
            for n in cmp_["new"]:
                lines.append("- NEW `%s` %s" % (n["id"], n["title"]))
            lines.append("")
        if cmp_["severity_changed"]:
            lines.append("**Severity changed:**")
            for s in cmp_["severity_changed"]:
                lines.append("- CHANGED `%s`: %s -> %s" % (s["id"], s["from"], s["to"]))
            lines.append("")
        if cmp_["persisting"]:
            lines.append("**Persisting** (not yet fixed, follow the advice):")
            for p in cmp_["persisting"]:
                lines.append("- `%s` %s" % (p["id"], p["title"]))
            lines.append("")
        if not (cmp_["resolved"] or cmp_["new"] or cmp_["severity_changed"] or cmp_["persisting"]):
            lines.append("No changes between the two runs.", "")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Cost runaway diagnostic (read-only)")
    ap.add_argument("project_dir", nargs="?", default=os.getcwd())
    ap.add_argument("--output", default=None,
                    help="Directory for reports (default: <project_dir>/cost_diag_reports)")
    ap.add_argument("--compare", metavar="OLD_REPORT.json",
                    help="Compare with a previous JSON report (new/resolved/persisting)")
    ap.add_argument("--json", action="store_true", help="Also print JSON to stdout")
    args = ap.parse_args()

    report = build_report(args.project_dir)
    if args.compare:
        if not os.path.exists(args.compare):
            print("Compare failed: old report not found: %s" % args.compare)
            sys.exit(2)
        try:
            report["compare"] = compare_reports(report, args.compare)
        except Exception as e:
            print("Compare failed: old report unreadable (%s)" % type(e).__name__)
            sys.exit(2)

    outdir = args.output or os.path.join(args.project_dir, "cost_diag_reports")
    os.makedirs(outdir, exist_ok=True)
    md_path = os.path.join(outdir, "cost_diagnostic_report.md")
    js_path = os.path.join(outdir, "cost_diagnostic_report.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    s = report["summary"]
    print("Cost runaway diagnostic complete.")
    print("  high: %d | medium: %d | low: %d | info: %d" % (
        s["high"], s["medium"], s["low"], s["info"]))
    cmp_ = report.get("compare")
    if cmp_:
        print("  compare: %d new | %d resolved | %d persisting" % (
            len(cmp_["new"]), len(cmp_["resolved"]), len(cmp_["persisting"])))
    print("Report: %s" % md_path)
    print("        %s" % js_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
