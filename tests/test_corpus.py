"""Receipts for real sessions, checked end to end. Skipped unless RUNLEDGER_CORPUS is set.

  RUNLEDGER_CORPUS         session files, separated by os.pathsep
  RUNLEDGER_CORPUS_EXPECT  optional JSON file kept outside the repo:
                           {"<session file name>": {"score": 35, "level": "Medium", "files": 4,
                                                    "run_cwd": "/folder/to/run/from"}}
  RUNLEDGER_CORPUS_OUT     optional folder that keeps the receipts written by the run

Each session (and its Droid .settings.json) is copied first, so a live session cannot change
between the receipt and the expected values read from its settings.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from runledger.cli import main
from runledger.parser import Usage
from runledger.paths import is_abs
from runledger.pricing import cost_of, price_for
from schema_check import SCHEMA, validate

SESSIONS = [p for p in os.environ.get("RUNLEDGER_CORPUS", "").split(os.pathsep) if p.strip()]
pytestmark = pytest.mark.skipif(not SESSIONS, reason="set RUNLEDGER_CORPUS to run the corpus checks")

TIME_BUDGET_S = 30
# The top two folders of where the suite runs (e.g. /workspace/projects): local layout that a
# safe receipt must not show, even for sessions whose log records a relative cwd.
_LAYOUT = "/" + "/".join(Path.cwd().as_posix().strip("/").split("/")[:2])
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SETTINGS_KEYS = (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                  ("cache_write_tokens", "cacheCreationTokens"), ("cache_read_tokens", "cacheReadTokens"))


def _expectations():
    path = os.environ.get("RUNLEDGER_CORPUS_EXPECT")
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def _freeze(session: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / session.name
    shutil.copyfile(session, copy)
    settings = session.with_suffix(".settings.json")
    if settings.is_file():
        shutil.copyfile(settings, copy.with_suffix(".settings.json"))
    return copy


def _status(cwd):
    if not cwd or not os.path.isdir(cwd) or shutil.which("git") is None:
        return None
    result = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _receipt(capsys, path, fmt, *extra):
    rc = main(["receipt", str(path), "--format", fmt, *extra])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _settings_totals(settings_path: Path):
    """(input, output, cache_write, cache_read) from inclusiveTokenUsage when it includes the
    session's own usage, else tokenUsage; None without a readable settings file."""
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    own = settings.get("tokenUsage") if isinstance(settings, dict) else None
    if not isinstance(own, dict):
        return None
    pick = lambda raw: tuple(int(raw.get(k) or 0) for _, k in _SETTINGS_KEYS)  # noqa: E731
    inclusive = settings.get("inclusiveTokenUsage")
    if isinstance(inclusive, dict) and all(a >= b for a, b in zip(pick(inclusive), pick(own))):
        return pick(inclusive)
    return pick(own)


@pytest.mark.parametrize("session", SESSIONS, ids=[Path(p).name for p in SESSIONS])
def test_corpus_session(session, tmp_path, capsys, monkeypatch):
    src = Path(session)
    expect = _expectations().get(src.name, {})
    if expect.get("run_cwd"):
        monkeypatch.chdir(expect["run_cwd"])
    frozen = _freeze(src, tmp_path / "in")
    keep = os.environ.get("RUNLEDGER_CORPUS_OUT")
    out_dir = Path(keep) if keep else tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    problems = []

    start = time.perf_counter()
    rc, raw_json, err = _receipt(capsys, frozen, "json", "-o", "-", "--no-safe")
    assert rc == 0 and "Traceback" not in err, err
    full = json.loads(raw_json)
    run_cwd = full["cwd"]
    before = _status(run_cwd)

    outputs = {}
    for fmt in ("md", "json"):
        rc, out, err = _receipt(capsys, frozen, fmt, "-o", "-")
        assert rc == 0 and "Traceback" not in err, (fmt, err)
        outputs[fmt] = out
    html_path = out_dir / f"{src.stem}.html"
    rc, _, err = _receipt(capsys, frozen, "html", "-o", str(html_path))
    assert rc == 0 and "Traceback" not in err, err
    elapsed = time.perf_counter() - start
    (out_dir / f"{src.stem}.md").write_text(outputs["md"], encoding="utf-8")
    (out_dir / f"{src.stem}.json").write_text(outputs["json"], encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    data = json.loads(outputs["json"])
    md = outputs["md"]

    if elapsed > TIME_BUDGET_S:
        problems.append(f"took {elapsed:.1f}s (budget {TIME_BUDGET_S}s)")
    for name, receipt in (("safe", data), ("full", full)):
        problems += [f"schema ({name}): {e}" for e in validate(receipt, SCHEMA)[:10]]
    if not data["safe"] or full["safe"]:
        problems.append("stdout JSON should be safe and --no-safe should not")

    # file counts agree across formats and modes
    counts = {
        "json": data["totals"]["files_changed"], "json files[]": len(data["files"]),
        "json --no-safe": full["totals"]["files_changed"],
        "md": int(re.search(r"\*\*Files changed:\*\* (\d+)", md).group(1)),
        "html": int(re.search(r"<span>Files changed</span><b>(\d+)</b>", html).group(1)),
    }
    if len(set(counts.values())) != 1:
        problems.append(f"file counts differ: {counts}")
    for f in data["files"]:
        if f["path"][:1] in ("'", '"') or is_abs(f["path"]):
            problems.append(f"file path not repo-relative: {f['path']!r}")

    # safe outputs leak nothing local
    home = os.path.expanduser("~")
    for fmt in ("md", "json"):
        text = outputs[fmt]
        layout = _LAYOUT if _LAYOUT.count("/") == 2 and not _LAYOUT.startswith("/tmp") else None
        for needle in filter(None, {home, layout, run_cwd if run_cwd and is_abs(run_cwd) else None}):
            if needle in text:
                problems.append(f"safe {fmt} contains {needle}")
        emails = sorted(set(_EMAIL.findall(text)))
        if emails:
            problems.append(f"safe {fmt} contains e-mail addresses: {emails[:3]}")

    # unknown usage is never shown as a zero cost
    if re.search(r"n/a tok, \$0\.0000", md):
        problems.append("md shows `n/a tok, $0.0000`")
    for s in data["steps"]:
        if not s["tokens_known"] and s["cost_usd"] is not None:
            problems.append(f"step {s['n']} has unknown tokens but cost {s['cost_usd']}")
    totals = data["totals"]
    if totals["usage_source"] == "missing":
        if totals["cost_usd"] is not None:
            problems.append("missing usage but a run cost")
        if "**Tokens:** n/a" not in md:
            problems.append("missing usage but md tokens are not n/a")
        lines = [ln for ln in md.splitlines() if re.match(r"^\d+\. ", ln)]
        bad = [ln for ln in lines if not ln.endswith("n/a tok, n/a")]
        if bad:
            problems.append(f"{len(bad)} step line(s) do not end `n/a tok, n/a`: {bad[0][-60:]!r}")

    # Droid totals follow the settings file
    settings = frozen.with_suffix(".settings.json")
    expected = _settings_totals(settings) if data["agent_id"] == "droid" and settings.is_file() else None
    if expected is not None:
        if totals["usage_source"] not in ("settings", "messages"):
            problems.append(f"usage_source is {totals['usage_source']}")
        got = tuple(totals[k] for k, _ in _SETTINGS_KEYS)
        if got != expected:
            problems.append(f"totals {got} != settings {expected}")
        models = [m for m, v in data["models"].items() if v["tokens"] > 0]
        step_sum = sum(s["tokens"] for s in data["steps"] if s["tokens_known"])
        if step_sum > totals["tokens"]:
            problems.append(f"step tokens {step_sum} > total {totals['tokens']}")
        if len(models) == 1:
            if price_for(models[0]) is not None:
                want = cost_of(Usage(*expected), models[0])
                if totals["cost_usd"] is None or abs(totals["cost_usd"] - want) > 1e-9:
                    problems.append(f"cost {totals['cost_usd']} != {want}")
            if totals["usage_source"] == "settings" and step_sum != totals["tokens"]:
                problems.append(f"step tokens {step_sum} != total {totals['tokens']}")
        elif len(models) > 1 and any(price_for(m) is None for m in models) and not totals["cost_partial"]:
            problems.append("mixed models with an unpriced one, but cost_partial is false")

    for key, got in (("score", data["risk"]["score"]), ("level", data["risk"]["level"]),
                     ("files", data["totals"]["files_changed"])):
        if key in expect and expect[key] != got:
            problems.append(f"{key} {got} != expected {expect[key]}")

    if before is not None and _status(run_cwd) != before:
        problems.append("the run's working tree changed")

    assert not problems, "\n".join(problems)
