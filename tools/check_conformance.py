#!/usr/bin/env python3
"""Run the committed receipt conformance matrix.

The matrix in tests/fixtures/conformance.json freezes the externally visible facts of the
fixture sessions: adapter detection, totals, cost provenance, changed files, risk outcomes and
guard counts. The checker also validates both JSON receipt modes against the shipped schema and
checks that Markdown, HTML and JSON agree. It is intentionally dependency-free so release
verification can run it from a source checkout without the development tools.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The matrix freezes the bundled price table. Importing runledger.prices loads RUNLEDGER_PRICES
# or ~/.runledger/prices.json once, so pin the override before importing the CLI.
os.environ["RUNLEDGER_PRICES"] = str(ROOT / "runledger" / "prices.json")

from runledger.cli import main as cli_main  # noqa: E402
from runledger.receipt_schema import SCHEMA, validate  # noqa: E402

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EXPECTED_KEYS = {
    "agent_id", "session_id", "cwd_source", "git_branch", "steps", "tokens", "input_tokens",
    "output_tokens", "cache_write_tokens", "cache_read_tokens", "cost_usd", "usage_source",
    "files_changed", "file_paths", "risk_score", "risk_level", "risk_codes", "guard_counts",
    "guard_events",
}


def _copy_case(fixtures: Path, folder: Path, case: dict) -> Path:
    source = fixtures / case["session"]
    target = folder / source.name
    if case.get("rewrite_cwd"):
        events = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        events[0]["cwd"] = str(folder)
        target.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    else:
        shutil.copyfile(str(source), str(target))
    for sidecar in case.get("sidecars", []):
        shutil.copyfile(str(fixtures / sidecar), str(folder / Path(sidecar).name))
    if case.get("guard_log"):
        log = folder / ".runledger" / "guard.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(fixtures / case["guard_log"]), str(log))
    return target


def _receipt(path: Path, fmt: str, *extra: str):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(["receipt", str(path), "--format", fmt, "-o", "-", *extra])
    return rc, out.getvalue(), err.getvalue()


def _html_receipt(path: Path, target: Path):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(["receipt", str(path), "--format", "html", "-o", str(target)])
    return rc, err.getvalue()


def _actual(data: dict) -> dict:
    totals = data["totals"]
    risk = data["risk"]
    return {
        "agent_id": data.get("agent_id"),
        "session_id": data.get("session_id"),
        "cwd_source": data.get("cwd_source"),
        "git_branch": data.get("git_branch"),
        "steps": totals["steps"],
        "tokens": totals["tokens"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_write_tokens": totals["cache_write_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cost_usd": totals["cost_usd"],
        "usage_source": totals["usage_source"],
        "files_changed": totals["files_changed"],
        "file_paths": sorted(item["path"] for item in data["files"]),
        "risk_score": risk["score"],
        "risk_level": risk["level"],
        "risk_codes": [item["code"] for item in risk["reasons"]],
        "guard_counts": totals.get("guard"),
        "guard_events": len(data.get("guard", [])),
    }


def _same(got, want) -> bool:
    if isinstance(want, float) or isinstance(got, float):
        return got is not None and want is not None and abs(float(got) - float(want)) < 1e-9
    return got == want


def _compare_expectations(actual: dict, expect: dict):
    problems = []
    for key in sorted(expect):
        if key not in EXPECTED_KEYS:
            problems.append(f"manifest uses unknown expectation key {key!r}")
            continue
        got = actual.get(key)
        want = expect[key]
        if not _same(got, want):
            problems.append(f"{key}: got {got!r}, expected {want!r}")
    return problems


def _format_count(fmt: str, text: str):
    if fmt == "md":
        match = re.search(r"\*\*Files changed:\*\* (\d+)", text)
    else:
        match = re.search(r"<span>Files changed</span><b>(\d+)</b>", text)
    return int(match.group(1)) if match else None


def _check_case(fixtures: Path, folder: Path, case: dict):
    problems = []
    path = _copy_case(fixtures, folder, case)

    rc, raw_full, err = _receipt(path, "json", "--no-safe")
    if rc != 0 or "Traceback" in err:
        return {"name": case["name"], "ok": False, "problems": [f"full receipt failed: {err.strip()}"]}
    try:
        full = json.loads(raw_full)
    except ValueError as exc:
        return {"name": case["name"], "ok": False, "problems": [f"full receipt is not JSON: {exc}"]}
    if not isinstance(full, dict):
        return {"name": case["name"], "ok": False, "problems": ["full receipt is not a JSON object"]}

    rc, raw_safe, err = _receipt(path, "json")
    if rc != 0 or "Traceback" in err:
        problems.append(f"safe receipt failed: {err.strip()}")
        safe = None
    else:
        try:
            safe = json.loads(raw_safe)
        except ValueError as exc:
            safe = None
            problems.append(f"safe receipt is not JSON: {exc}")
        if not isinstance(safe, dict):
            problems.append("safe receipt is not a JSON object")
            safe = None

    rc, markdown, err = _receipt(path, "md")
    if rc != 0 or "Traceback" in err:
        problems.append(f"markdown receipt failed: {err.strip()}")
        markdown = ""
    if not markdown.strip():
        problems.append("markdown receipt is empty")
    html_path = folder / "receipt.html"
    rc, err = _html_receipt(path, html_path)
    if rc != 0 or "Traceback" in err:
        problems.append(f"html receipt failed: {err.strip()}")
        html = ""
    else:
        html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
    if not html.strip():
        problems.append("html receipt is empty")

    for label, data in (("full", full), ("safe", safe)):
        if not isinstance(data, dict):
            continue
        for error in validate(data, SCHEMA)[:10]:
            problems.append(f"{label} schema: {error}")
    if isinstance(safe, dict):
        if not safe.get("safe") or full.get("safe"):
            problems.append("safe mode flags are wrong")
        if safe["totals"]["files_changed"] != len(safe["files"]):
            problems.append("totals.files_changed differs from len(files)")

    actual = _actual(full)
    problems.extend(_compare_expectations(actual, case["expect"]))

    if markdown:
        if not re.search(r"<!-- runledger: safe=true schema=1 -->\n?$", markdown):
            problems.append("markdown does not end with the safe/schema marker")
        md_count = _format_count("md", markdown)
        if md_count != full["totals"]["files_changed"]:
            problems.append(f"markdown file count {md_count} != {full['totals']['files_changed']}")
    if html:
        html_count = _format_count("html", html)
        if html_count != full["totals"]["files_changed"]:
            problems.append(f"html file count {html_count} != {full['totals']['files_changed']}")

    if safe is not None:
        safe_texts = [json.dumps(safe, ensure_ascii=False), markdown]
        needles = {os.path.expanduser("~"), str(folder)}
        for text in safe_texts:
            for needle in {n for n in needles if n and len(n) > 3}:
                if needle in text:
                    problems.append(f"safe output contains local path {needle!r}")
            emails = sorted(set(EMAIL.findall(text)))
            if emails:
                problems.append(f"safe output contains e-mail addresses: {emails[:3]}")

    for step in full["steps"]:
        if not step["tokens_known"] and step["cost_usd"] is not None:
            problems.append(f"step {step['n']} has unknown tokens but cost {step['cost_usd']}")
            break

    return {"name": case["name"], "ok": not problems, "problems": problems, "actual": actual}


def run_matrix(root: Path):
    fixtures = root / "tests" / "fixtures"
    manifest = json.loads((fixtures / "conformance.json").read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(prefix="runledger-conformance-") as temp:
        base = Path(temp)
        os.environ["RUNLEDGER_CONFIG"] = str(base / "no-user-config.json")
        for index, case in enumerate(manifest["cases"]):
            folder = base / f"{index:02d}-{case['name']}"
            folder.mkdir(parents=True)
            try:
                results.append(_check_case(fixtures, folder, case))
            except Exception as exc:  # the report must name the broken case, not lose the rest
                results.append({"name": case["name"], "ok": False, "problems": [f"unchecked exception: {exc!r}"]})
    passed = sum(1 for result in results if result["ok"])
    return {
        "version": manifest["version"],
        "summary": {"cases": len(results), "passed": passed, "failed": len(results) - passed},
        "cases": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository or source-tree root")
    parser.add_argument("--json", action="store_true", help="write the machine-readable report")
    args = parser.parse_args(argv)

    report = run_matrix(args.root.resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for result in report["cases"]:
            if result["ok"]:
                print(f"ok {result['name']}")
            else:
                print(f"FAIL {result['name']}")
                for problem in result["problems"]:
                    print(f"  - {problem}")
        summary = report["summary"]
        print(f"conformance: {summary['passed']}/{summary['cases']} cases passed")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
