"""runledger command line.

  runledger list [--project PATH] [--all] [--agent AGENT] [--limit N]
  runledger receipt [SESSION | --latest] [--project PATH] [--agent AGENT] [--format html|md|json]
                    [-o FILE] [--ai] [--ai-model MODEL] [--open]
  runledger serve [--host 127.0.0.1] [--port 8787] [--db runledger.db]
  runledger team create NAME [--db runledger.db]
  runledger push [SESSION | --latest] [--project PATH] [--agent AGENT] [--server URL] [--key KEY] [--user NAME]
  runledger guard                       Claude Code PreToolUse hook (reads the event on stdin)
  runledger guard install [--project PATH | --global]
  runledger guard test 'EVENT_JSON'

SESSION is a session file of any supported agent; the agent is detected from the file.
AGENT is one of claude-code, codex, aider, native (the RunLedger format, docs/format.md).
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import adapters
from .pricing import apply_costs
from .receipt import render
from .risk import assess
from .summarize import DEFAULT_AI_MODEL, ai_summaries, apply_templates

AGENTS = ("claude-code", "codex", "aider", "native")


def build(session_path: str, use_ai: bool = False, ai_model: str = DEFAULT_AI_MODEL):
    path = Path(session_path)
    run = adapters.detect(path).parse(path)
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


def _file_stem(session_id: str) -> str:
    """Session ids come from agents' files, so keep only file-name-safe characters."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:8] or "run"


def _latest(project: Optional[str], agent: Optional[str]) -> Optional[str]:
    """Path of the newest session for the project (of `agent` only, if given).
    Prints the reason and returns None when there is none."""
    try:
        files = adapters.find_sessions(project, agent)
    except KeyError as exc:  # the agent's adapter is not installed
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return None
    if not files:
        kind = f"{adapters.label(agent)} " if agent else ""
        print(f"No {kind}sessions found for this folder. Pass a session file or use --project.", file=sys.stderr)
        return None
    return str(files[0])


def _session_path(args) -> Optional[str]:
    if args.session:
        if getattr(args, "agent", None):
            print("note: --agent is ignored when a session file is given", file=sys.stderr)
        return args.session
    return _latest(args.project or os.getcwd(), getattr(args, "agent", None))


def cmd_list(args) -> int:
    project = None if args.all else (args.project or os.getcwd())
    try:
        files = adapters.find_sessions(project, args.agent)
    except KeyError as exc:  # the agent's adapter is not installed
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1
    if not files:
        where = "any project" if args.all else project
        kind = f"{adapters.label(args.agent)} " if args.agent else ""
        print(f"No {kind}sessions found for {where}.", file=sys.stderr)
        return 1
    print(f"{'when':<16}  {'id':<8}  {'agent':<12}  {'steps':>5}  first request")
    for f in files[: args.limit]:
        try:
            run = adapters.detect(f).parse(f)
        except (OSError, ValueError) as exc:
            print(f"skipped: {exc}", file=sys.stderr)
            continue
        when = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        first = (run.prompts[0] if run.prompts else "").replace("\n", " ")[:60]
        print(f"{when}  {run.session_id[:8]:<8}  {run.agent[:12]:<12}  {len(run.steps):>5} steps  {first}")
        if args.all:
            print(f"                  {run.cwd}")
    return 0


def cmd_receipt(args) -> int:
    path = _session_path(args)
    if path is None:
        return 1
    try:
        run, score, level, risks, note = build(path, args.ai, args.ai_model)
    except (OSError, ValueError) as exc:  # missing file, or a session file that does not validate
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = render(run, score, level, risks, args.format)
    if note:
        print(note, file=sys.stderr)
    if args.output == "-":
        sys.stdout.write(out)
        return 0
    ext = {"html": "html", "md": "md", "json": "json"}[args.format]
    target = Path(args.output or f"runledger-{_file_stem(run.session_id)}.{ext}")
    target.write_text(out, encoding="utf-8")
    print(f"Receipt: {target}  ·  risk {score}/100 ({level})  ·  {len(run.steps)} steps")
    if args.open and args.format == "html":
        webbrowser.open(target.resolve().as_uri())
    return 2 if (args.fail_on is not None and score >= args.fail_on) else 0


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def cmd_serve(args) -> int:
    from .server.app import make_server

    try:
        server = make_server(args.db, host=args.host, port=args.port, log_requests=True)
    except OSError as exc:  # port in use, bad address
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:  # bad database path or file
        print(f"error: cannot open database {args.db}: {exc}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    print(f"RunLedger team server on http://{host}:{port}  (database: {args.db})")
    if host not in _LOOPBACK:
        print("Warning: listening on a network address. Put it behind HTTPS before sharing it.", file=sys.stderr)
    print("Create a team with: runledger team create NAME   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.db.close()
    return 0


def cmd_team_create(args) -> int:
    from .server.db import Database

    try:
        db = Database(args.db)
    except sqlite3.Error as exc:
        print(f"error: cannot open database {args.db}: {exc}", file=sys.stderr)
        return 1
    try:
        team_id, key = db.create_team(args.name)
    except (ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"Created team '{args.name.strip()}' (id {team_id}).")
    print("API key (shown once, store it safely):")
    print(key)
    print("Push runs with: runledger push --server URL --key KEY")
    return 0


def cmd_push(args) -> int:
    from .client import PushError, push

    server = args.server or os.environ.get("RUNLEDGER_SERVER")
    key = args.key or os.environ.get("RUNLEDGER_API_KEY")
    path = _session_path(args)
    if path is None:
        return 1
    try:
        result = push(server or "", key or "", path, user=args.user)
    except PushError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Pushed {result.get('id', '?')}  ·  risk {result.get('risk_score', '?')}/100 "
          f"({result.get('risk_level', '?')})  ·  {server.rstrip('/')}{result.get('url', '')}")
    return 0


def cmd_guard(args) -> int:
    from . import guard

    if args.guard_cmd == "install":
        return guard.cmd_install(args.project, args.global_scope)
    if args.guard_cmd == "test":
        return guard.cmd_test(args.event)
    return guard.main()


def _agent_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", choices=AGENTS,
                        help="only sessions from this agent (default: every agent)")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="runledger", description="Turn coding-agent runs into shareable receipts.")
    p.add_argument("--version", action="version", version=f"runledger {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list recent sessions")
    pl.add_argument("--project", help="project folder (default: current folder)")
    pl.add_argument("--all", action="store_true", help="sessions from every project")
    _agent_arg(pl)
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("receipt", help="build a receipt for a session")
    pr.add_argument("session", nargs="?", help="session file of any supported agent (default: latest for this folder)")
    pr.add_argument("--latest", action="store_true", help="use the latest session (default)")
    pr.add_argument("--project", help="project folder (default: current folder)")
    _agent_arg(pr)
    pr.add_argument("--format", choices=["html", "md", "json"], default="html")
    pr.add_argument("-o", "--output", help="output file, or - for stdout")
    pr.add_argument("--ai", action="store_true", help="plain-language summaries with Claude (needs ANTHROPIC_API_KEY)")
    pr.add_argument("--ai-model", default=DEFAULT_AI_MODEL, help=f"model for --ai (default {DEFAULT_AI_MODEL})")
    pr.add_argument("--open", action="store_true", help="open the HTML receipt in a browser")
    pr.add_argument("--fail-on", type=int, metavar="SCORE", help="exit with code 2 if risk score >= SCORE (for CI/hooks)")
    pr.set_defaults(func=cmd_receipt)

    ps = sub.add_parser("serve", help="run the team server (collects pushed runs, serves the dashboard)")
    ps.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ps.add_argument("--port", type=int, default=8787)
    ps.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    ps.set_defaults(func=cmd_serve)

    pt = sub.add_parser("team", help="manage teams on a team server")
    tsub = pt.add_subparsers(dest="team_cmd", required=True)
    ptc = tsub.add_parser("create", help="create a team and print its API key (shown once)")
    ptc.add_argument("name")
    ptc.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    ptc.set_defaults(func=cmd_team_create)

    pp = sub.add_parser("push", help="send a session receipt to a team server")
    pp.add_argument("session", nargs="?", help="session file of any supported agent (default: latest for this folder)")
    pp.add_argument("--latest", action="store_true", help="use the latest session (default)")
    pp.add_argument("--project", help="project folder to find sessions in (default: current folder)")
    _agent_arg(pp)
    pp.add_argument("--server", help="server URL (default: $RUNLEDGER_SERVER)")
    pp.add_argument("--key", help="team API key (default: $RUNLEDGER_API_KEY)")
    pp.add_argument("--user", help="developer name shown on the dashboard (default: git user.email or OS user)")
    pp.set_defaults(func=cmd_push)

    pg = sub.add_parser("guard", help="real-time policy guard: Claude Code PreToolUse hook")
    gsub = pg.add_subparsers(dest="guard_cmd", metavar="{install,test}")
    pgi = gsub.add_parser("install", help="add the guard hook to Claude Code settings.json")
    scope = pgi.add_mutually_exclusive_group()
    scope.add_argument("--project", metavar="PATH", help="project folder: writes PATH/.claude/settings.json (default: current folder)")
    scope.add_argument("--global", dest="global_scope", action="store_true", help="writes ~/.claude/settings.json")
    pgt = gsub.add_parser("test", help="print the decision for one PreToolUse event (JSON)")
    pgt.add_argument("event", help="the event as a JSON object")
    pg.set_defaults(func=cmd_guard)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
