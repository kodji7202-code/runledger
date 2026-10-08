"""runledger command line.

  runledger list [--project PATH] [--all] [--agent AGENT] [--limit N]
  runledger receipt [SESSION | --latest] [--project PATH] [--agent AGENT] [--format html|md|json]
                    [-o FILE] [--ai] [--ai-model MODEL] [--review] [--review-model MODEL] [--open]
  runledger serve [--host 127.0.0.1] [--port 8787] [--db runledger.db]
                  [--tls-cert FILE --tls-key FILE] [--secure-cookies] [--trust-proxy]
                  [--trusted-proxy CIDR ...]
  runledger team create NAME [--db runledger.db]
  runledger key create --team-id N --label L --role admin|member|viewer [--db runledger.db]
  runledger key list --team-id N [--db runledger.db]
  runledger key revoke ID [--db runledger.db]
  runledger key rotate ID [--db runledger.db]
  runledger push [SESSION | --latest] [--project PATH] [--agent AGENT] [--server URL] [--key KEY] [--user NAME]
                 [--review] [--review-model MODEL]
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
from .quality import analyze
from .receipt import render
from .risk import assess
from .review import DEFAULT_REVIEW_MODEL, ai_review
from .summarize import DEFAULT_AI_MODEL, ai_summaries, apply_templates

AGENTS = ("claude-code", "codex", "aider", "native")


def build(session_path: str, use_ai: bool = False, ai_model: str = DEFAULT_AI_MODEL,
          review: bool = False, review_model: str = DEFAULT_REVIEW_MODEL):
    """Parse, price and assess a session. Returns (run, score, level, risks, note).

    `use_ai` adds Claude step summaries; `review` adds the Claude risk review (run.ai_review).
    The rule-based score and risks are the same with or without them. When an AI step fails,
    the deterministic receipt is kept and `note` says why, one line per failed step."""
    path = Path(session_path)
    run = adapters.detect(path).parse(path)
    apply_costs(run)
    apply_templates(run)
    notes: List[str] = []
    if use_ai:
        try:
            ai_summaries(run, model=ai_model)
        except Exception as exc:  # keep the deterministic receipt
            notes.append(f"AI summaries skipped: {_one_line(exc)}")
    score, level, risks = assess(run)
    analyze(run, risks)
    if review:
        try:
            ai_review(run, risks, model=review_model)
        except Exception as exc:  # keep the deterministic receipt
            notes.append(f"AI risk review skipped: {_one_line(exc)}")
    return run, score, level, risks, "\n".join(notes) or None


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _print_notes(notes: List[str]) -> None:
    for note in notes:
        if note:
            print(note, file=sys.stderr)


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
        run, score, level, risks, note = build(path, args.ai, args.ai_model,
                                               review=args.review, review_model=args.review_model)
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
    from .server.app import TLSConfigError, make_server

    try:
        server = make_server(
            args.db, host=args.host, port=args.port, log_requests=True,
            tls_cert=args.tls_cert, tls_key=args.tls_key,
            secure_cookies=args.secure_cookies, trust_proxy=args.trust_proxy,
            trusted_proxies=args.trusted_proxy,
        )
    except TLSConfigError as exc:  # missing or unusable certificate or key
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # a --trusted-proxy (or RUNLEDGER_TRUSTED_PROXIES) entry is not an address or range
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:  # port in use, bad address
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:  # bad database path or file
        print(f"error: cannot open database {args.db}: {exc}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    scheme = "https" if server.tls_context is not None else "http"
    print(f"RunLedger team server on {scheme}://{host}:{port}  (database: {args.db})")
    if server.trusted_proxies:
        shown = ", ".join(str(net) for net in server.trusted_proxies)
        print(f"Trusting X-Forwarded-For from: {shown}")
    if host not in _LOOPBACK and server.tls_context is None:
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


def cmd_key(args) -> int:
    """Local key administration on the team database. The server does not need to be running.
    Changes are audited with the actor "cli"."""
    from .server.auth import InvalidKey
    from .server.db import Database

    try:
        db = Database(args.db)
    except sqlite3.Error as exc:
        print(f"error: cannot open database {args.db}: {exc}", file=sys.stderr)
        return 1
    try:
        if args.key_cmd == "list":
            return _key_list(db, args.team_id)
        if args.key_cmd == "create":
            if db.team_name(args.team_id) is None:
                print(f"error: no team with id {args.team_id}", file=sys.stderr)
                return 1
            try:
                view = db.create_key(args.team_id, args.label, args.role, actor="cli")
            except (InvalidKey, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"Created {view['role']} key '{view['label']}' (id {view['id']}) for team {args.team_id}.")
            print("API key (shown once, store it safely):")
            print(view["key"])
            return 0
        team_id = db.key_team_id(args.key_id)
        if team_id is None:
            print(f"error: no key with id {args.key_id}", file=sys.stderr)
            return 1
        if args.key_cmd == "revoke":
            outcome, view = db.revoke_key(team_id, args.key_id, actor="cli")
            if outcome == "ok":
                print(f"Revoked key '{view['label']}' (id {args.key_id}). Its sessions end at once.")
                return 0
            if outcome == "already_revoked":
                print("error: this key is already revoked", file=sys.stderr)
            else:
                print("error: this is the team's last active admin key. Create or rotate another admin key first.",
                      file=sys.stderr)
            return 1
        outcome, view = db.rotate_key(team_id, args.key_id, actor="cli")
        if outcome != "ok":
            print("error: a revoked key cannot be rotated", file=sys.stderr)
            return 1
        print(f"Rotated key '{view['label']}' (id {args.key_id}). The old key no longer works.")
        print("New API key (shown once, store it safely):")
        print(view["key"])
        return 0
    finally:
        db.close()


def _key_list(db, team_id: int) -> int:
    if db.team_name(team_id) is None:
        print(f"error: no team with id {team_id}", file=sys.stderr)
        return 1
    keys = db.list_keys(team_id)
    if not keys:
        print(f"No keys for team {team_id}.", file=sys.stderr)
        return 1
    print(f"{'id':<16}  {'label':<20}  {'role':<7}  {'prefix':<8}  {'created':<20}  {'last used':<20}  status")
    for k in keys:
        status = f"revoked {k['revoked_at']}" if k["revoked_at"] else "active"
        print(f"{k['id']:<16}  {k['label'][:20]:<20}  {k['role']:<7}  {(k['prefix'] or '-'):<8}  "
              f"{k['created_at']:<20}  {(k['last_used_at'] or 'never'):<20}  {status}")
    return 0


def cmd_push(args) -> int:
    from .client import PushError, push

    server = args.server or os.environ.get("RUNLEDGER_SERVER")
    key = args.key or os.environ.get("RUNLEDGER_API_KEY")
    path = _session_path(args)
    if path is None:
        return 1
    notes: List[str] = []
    try:
        result = push(server or "", key or "", path, user=args.user,
                      review=args.review, review_model=args.review_model, notes=notes)
    except PushError as exc:
        _print_notes(notes)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_notes(notes)
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
    pr.add_argument("--review", action="store_true",
                    help="AI risk review of the flagged steps with Claude (opt-in; needs ANTHROPIC_API_KEY; "
                         "sends redacted commands and edits, see docs/analysis.md)")
    pr.add_argument("--review-model", default=DEFAULT_REVIEW_MODEL,
                    help=f"model for --review (default {DEFAULT_REVIEW_MODEL})")
    pr.add_argument("--open", action="store_true", help="open the HTML receipt in a browser")
    pr.add_argument("--fail-on", type=int, metavar="SCORE", help="exit with code 2 if risk score >= SCORE (for CI/hooks)")
    pr.set_defaults(func=cmd_receipt)

    ps = sub.add_parser("serve", help="run the team server (collects pushed runs, serves the dashboard)")
    ps.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ps.add_argument("--port", type=int, default=8787)
    ps.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    ps.add_argument("--tls-cert", metavar="FILE", help="PEM certificate: serve HTTPS (TLS 1.2+); needs --tls-key")
    ps.add_argument("--tls-key", metavar="FILE", help="PEM private key for --tls-cert")
    ps.add_argument("--secure-cookies", action="store_true",
                    help="set the Secure flag on the dashboard cookie (also RUNLEDGER_SECURE_COOKIES=1)")
    ps.add_argument("--trust-proxy", action="store_true",
                    help="honour X-Forwarded-Proto: https from a reverse proxy (Caddy, nginx) for the Secure flag; "
                         "only from --trusted-proxy addresses when any are given")
    ps.add_argument("--trusted-proxy", action="append", default=[], metavar="CIDR",
                    help="address or range of a reverse proxy whose X-Forwarded-For gives the client address "
                         "(repeatable; also $RUNLEDGER_TRUSTED_PROXIES, comma separated)")
    ps.set_defaults(func=cmd_serve)

    pt = sub.add_parser("team", help="manage teams on a team server")
    tsub = pt.add_subparsers(dest="team_cmd", required=True)
    ptc = tsub.add_parser("create", help="create a team and print its API key (shown once)")
    ptc.add_argument("name")
    ptc.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    ptc.set_defaults(func=cmd_team_create)

    pk = sub.add_parser("key", help="manage a team's API keys on a team database")
    ksub = pk.add_subparsers(dest="key_cmd", required=True)
    pkc = ksub.add_parser("create", help="create a key with a role and print it (shown once)")
    pkc.add_argument("--team-id", type=int, required=True)
    pkc.add_argument("--label", required=True, help="what the key is for, e.g. 'laptop' or 'CI'")
    pkc.add_argument("--role", required=True, choices=["admin", "member", "viewer"])
    pkc.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    pkc.set_defaults(func=cmd_key)
    pkl = ksub.add_parser("list", help="list a team's keys (never their secrets)")
    pkl.add_argument("--team-id", type=int, required=True)
    pkl.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    pkl.set_defaults(func=cmd_key)
    pkr = ksub.add_parser("revoke", help="revoke a key by id; its sessions end at once")
    pkr.add_argument("key_id")
    pkr.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    pkr.set_defaults(func=cmd_key)
    pkt = ksub.add_parser("rotate", help="replace a key's secret; the old one stops working at once")
    pkt.add_argument("key_id")
    pkt.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    pkt.set_defaults(func=cmd_key)

    pp = sub.add_parser("push", help="send a session receipt to a team server")
    pp.add_argument("session", nargs="?", help="session file of any supported agent (default: latest for this folder)")
    pp.add_argument("--latest", action="store_true", help="use the latest session (default)")
    pp.add_argument("--project", help="project folder to find sessions in (default: current folder)")
    _agent_arg(pp)
    pp.add_argument("--server", help="server URL (default: $RUNLEDGER_SERVER)")
    pp.add_argument("--key", help="team API key (default: $RUNLEDGER_API_KEY)")
    pp.add_argument("--user", help="developer name shown on the dashboard (default: git user.email or OS user)")
    pp.add_argument("--review", action="store_true",
                    help="include an AI risk review from Claude (opt-in; needs ANTHROPIC_API_KEY; "
                         "sends redacted commands and edits, see docs/analysis.md)")
    pp.add_argument("--review-model", default=DEFAULT_REVIEW_MODEL,
                    help=f"model for --review (default {DEFAULT_REVIEW_MODEL})")
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
