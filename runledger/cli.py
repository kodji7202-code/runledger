"""runledger command line.

  runledger list [--project PATH] [--all]
  runledger receipt [SESSION.jsonl | --latest] [--project PATH] [--format html|md|json]
                    [-o FILE] [--ai] [--ai-model MODEL] [--open]
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__
from .parser import find_sessions, parse_session
from .pricing import apply_costs
from .receipt import render
from .risk import assess
from .summarize import DEFAULT_AI_MODEL, ai_summaries, apply_templates


def build(session_path: str, use_ai: bool = False, ai_model: str = DEFAULT_AI_MODEL):
    run = parse_session(session_path)
    apply_costs(run)
    apply_templates(run)
    ai_note = None
    if use_ai:
        try:
            ai_summaries(run, model=ai_model)
        except Exception as exc:  # keep the deterministic receipt
            ai_note = f"AI summaries skipped: {exc}"
    score, level, risks = assess(run)
    return run, score, level, risks, ai_note


def cmd_list(args) -> int:
    project = None if args.all else (args.project or os.getcwd())
    files = find_sessions(project)
    if not files:
        where = "any project" if args.all else project
        print(f"No Claude Code sessions found for {where}.", file=sys.stderr)
        return 1
    for f in files[: args.limit]:
        run = parse_session(f)
        when = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        first = (run.prompts[0] if run.prompts else "").replace("\n", " ")[:60]
        print(f"{when}  {f.stem[:8]}  {len(run.steps):>4} steps  {first}")
        if args.all:
            print(f"                  {run.cwd}")
    return 0


def cmd_receipt(args) -> int:
    if args.session:
        path = args.session
    else:
        files = find_sessions(args.project or os.getcwd())
        if not files:
            print("No Claude Code sessions found for this folder. Pass a .jsonl path or use --project.", file=sys.stderr)
            return 1
        path = str(files[0])
    run, score, level, risks, note = build(path, args.ai, args.ai_model)
    out = render(run, score, level, risks, args.format)
    if note:
        print(note, file=sys.stderr)
    if args.output == "-":
        sys.stdout.write(out)
        return 0
    ext = {"html": "html", "md": "md", "json": "json"}[args.format]
    target = Path(args.output or f"runledger-{run.session_id[:8]}.{ext}")
    target.write_text(out, encoding="utf-8")
    print(f"Receipt: {target}  ·  risk {score}/100 ({level})  ·  {len(run.steps)} steps")
    if args.open and args.format == "html":
        webbrowser.open(target.resolve().as_uri())
    return 2 if (args.fail_on is not None and score >= args.fail_on) else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="runledger", description="Turn Claude Code runs into shareable receipts.")
    p.add_argument("--version", action="version", version=f"runledger {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list recent Claude Code sessions")
    pl.add_argument("--project", help="project folder (default: current folder)")
    pl.add_argument("--all", action="store_true", help="sessions from every project")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("receipt", help="build a receipt for a session")
    pr.add_argument("session", nargs="?", help="path to a session .jsonl (default: latest for this folder)")
    pr.add_argument("--latest", action="store_true", help="use the latest session (default)")
    pr.add_argument("--project", help="project folder (default: current folder)")
    pr.add_argument("--format", choices=["html", "md", "json"], default="html")
    pr.add_argument("-o", "--output", help="output file, or - for stdout")
    pr.add_argument("--ai", action="store_true", help="plain-language summaries with Claude (needs ANTHROPIC_API_KEY)")
    pr.add_argument("--ai-model", default=DEFAULT_AI_MODEL, help=f"model for --ai (default {DEFAULT_AI_MODEL})")
    pr.add_argument("--open", action="store_true", help="open the HTML receipt in a browser")
    pr.add_argument("--fail-on", type=int, metavar="SCORE", help="exit with code 2 if risk score >= SCORE (for CI/hooks)")
    pr.set_defaults(func=cmd_receipt)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
