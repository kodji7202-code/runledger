"""runledger command line.

  runledger list [--project PATH] [--all] [--agent AGENT] [--limit N]
  runledger receipt [SESSION | --latest] [--project PATH] [--agent AGENT] [--format html|md|json]
                    [-o FILE] [--ai] [--ai-model MODEL] [--review] [--review-model MODEL] [--open]
  runledger serve [--host 127.0.0.1] [--port 8787] [--db runledger.db]
                  [--tls-cert FILE --tls-key FILE] [--secure-cookies] [--trust-proxy]
                  [--trusted-proxy CIDR ...]
  runledger team create NAME [--db runledger.db]
  runledger backup [--db runledger.db] [--dir backups] [--keep 14]
  runledger key create --team-id N --label L --role admin|member|viewer [--db runledger.db]
  runledger key list --team-id N [--db runledger.db]
  runledger key revoke ID [--db runledger.db]
  runledger key rotate ID [--db runledger.db]
  runledger push [SESSION | --latest] [--project PATH] [--agent AGENT] [--server URL] [--key KEY] [--user NAME]
                 [--review] [--review-model MODEL]
  runledger guard                       Claude Code PreToolUse hook (reads the event on stdin)
  runledger guard install [--project PATH | --global]
  runledger guard uninstall [--project PATH | --global]
  runledger guard test 'EVENT_JSON'
  runledger checkpoint [--label TEXT] [--run RUN_ID] [--cwd DIR]
  runledger checkpoints [--cwd DIR]
  runledger checkpoints prune --keep N [--cwd DIR]
  runledger rollback [CHECKPOINT | --run RUN_ID | --last] [--dry-run] [--yes] [--cwd DIR]

SESSION is a session file of any supported agent; the agent is detected from the file.
AGENT is one of claude-code, codex, droid, aider, native (the RunLedger format, docs/format.md).
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
from typing import List, Optional, Tuple

from . import __version__
from . import adapters
from .gitinfo import current_branch, file_diffs, repo_context, session_base, toplevel, working_changes
from .guard import redact
from .guard_receipt import load_guard_decisions
from .parser import _ts
from .paths import is_abs
from .pricing import apply_costs
from .quality import analyze
from .receipt import md_trailer, redact_run, render
from .safe import make_safe
from .risk import assess, is_test_source
from .review import DEFAULT_REVIEW_MODEL, ai_review
from .summarize import DEFAULT_AI_MODEL, ai_summaries, apply_templates, file_changes

AGENTS = ("claude-code", "codex", "droid", "aider", "native")


def build(session_path: str, use_ai: bool = False, ai_model: str = DEFAULT_AI_MODEL,
          review: bool = False, review_model: str = DEFAULT_REVIEW_MODEL):
    """Parse, price and assess a session. Returns (run, score, level, risks, note).

    `use_ai` adds Claude step summaries; `review` adds the Claude risk review (run.ai_review).
    The rule-based score and risks are the same with or without them. When an AI step fails,
    the deterministic receipt is kept and `note` says why, one line per failed step.
    Secrets are masked in everything the receipt shows, after the rules have scored the raw text."""
    path = Path(session_path)
    run = adapters.detect(path).parse(path)
    reconcile_git(run)
    run.guard = load_guard_decisions(run)
    from .rollback import checkpoint_for_run
    run.checkpoint_id = checkpoint_for_run(run.cwd, run.session_id)
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
    redact_run(run, risks)
    return run, score, level, risks, "\n".join(notes) or None


def reconcile_git(run) -> None:
    """Fill what the session log leaves out from the run's Git working tree: the
    branch when none was recorded, line counts for files written through the shell, and
    the diffs of changed test files. When the run's changes were committed after it began,
    both compare the working tree with the last commit before the session start (run.git_base)."""
    with repo_context():
        if not run.git_branch:
            run.git_branch = current_branch(run.cwd)
        run.git_base = session_base(run.cwd, _ts(run.started))
        changes = file_changes(run)
        shell_paths = [p for p, fc in changes.items()
                       if fc.shell or (not fc.added and not fc.removed and not fc.deleted)]
        if shell_paths:
            run.git_changes = working_changes(run.cwd, shell_paths, run.git_base)
        test_paths = [p for p, fc in changes.items()
                      if not fc.deleted and not is_abs(p) and is_test_source(p)]
        if test_paths:
            run.test_diffs = file_diffs(run.cwd, test_paths, run.git_base)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _write_stdout(text: str) -> None:
    """Receipts are UTF-8 documents, whatever the console encoding (cp1252 on Windows)."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def _tolerant_console() -> None:
    """Status lines print `·`, `−` and `⚠️`; a console that cannot encode them gets `?` instead of a crash."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _run(args) -> int:
    try:
        return args.func(args)
    except BrokenPipeError:
        # The reader (`| head`) went away. Point stdout at devnull so the interpreter's
        # final flush cannot raise a second BrokenPipeError on exit.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if os.environ.get("RUNLEDGER_DEBUG") == "1":
            raise
        message = _one_line(exc) or type(exc).__name__
        print(f"error: {message} (set RUNLEDGER_DEBUG=1 for details)", file=sys.stderr)
        return 1


def cmd_checkpoint(args) -> int:
    from .rollback import CheckpointError, Repository, hook_main
    if args.hook:
        return hook_main()
    try:
        run_id = args.run or os.environ.get("RUNLEDGER_RUN_ID") or os.environ.get("RUNLEDGER_SESSION_ID")
        checkpoint = Repository(args.cwd).create(label=args.label or "", run_id=run_id)
        print(checkpoint.id)
        return 0
    except (CheckpointError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_checkpoints(args) -> int:
    from .rollback import CheckpointError, Repository
    try:
        repo = Repository(args.cwd)
        if args.checkpoints_cmd == "prune":
            removed = repo.prune(args.keep)
            print(f"Pruned {len(removed)} checkpoint(s). Kept the newest {args.keep}.")
            return 0
        checkpoints = repo.checkpoints()
        if not checkpoints:
            print("No checkpoints. Create one with 'runledger checkpoint'.")
            return 0
        tree = repo.snapshot_tree()
        for cp in checkpoints:
            plan = repo.plan(cp, tree)
            label = repr(redact(cp.label))
            print(f"{cp.id}  {cp.created_at}  {label}  "
                  f"({len(plan.restore) + len(plan.remove) + len(plan.keep_ignored)} file(s) differ)")
            for path in plan.restore:
                print(f"  restore {path!r}")
            for path in plan.remove:
                print(f"  remove  {path!r}")
            for path in plan.keep_ignored:
                print(f"  keep ignored {path!r}")
        return 0
    except (CheckpointError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_rollback(args) -> int:
    from .rollback import CheckpointError, Repository
    try:
        repo = Repository(args.cwd)
        checkpoint = repo.select(args.checkpoint, run_id=args.run)
        plan = repo.plan(checkpoint)
        print(f"Rollback to {checkpoint.id} in {str(repo.root)!r}")
        for path in plan.restore:
            print(f"  restore {path!r}")
        for path in plan.remove:
            print(f"  remove  {path!r}")
        for path in plan.keep_ignored:
            print(f"  keep ignored {path!r}")
        print(f"{len(plan.restore)} restore, {len(plan.remove)} remove. "
              "HEAD, branch, and index stay unchanged.")
        if args.dry_run:
            print("Dry run. No files restored and no safety checkpoint created.")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise CheckpointError("Rollback requires confirmation. Use --dry-run to preview "
                                      "or --yes to confirm in a non-interactive shell.")
            try:
                confirmed = input("Create a safety checkpoint and restore these files? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                confirmed = ""
            if confirmed.strip().lower() not in ("y", "yes"):
                print("Cancelled. Nothing restored.")
                return 1
        safety = repo.rollback(plan)
        print(f"Restored {checkpoint.id}. Safety checkpoint: {safety.id}")
        print(f"Undo: runledger rollback {safety.id}")
        return 0
    except (CheckpointError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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
    from . import session_cache
    from .parser import _fs_str

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
    entries, dirty = session_cache.load()
    print(f"{'when':<16}  {'id':<8}  {'agent':<12}  {'steps':>5}  first request")
    limit = max(0, args.limit) if args.limit is not None else 20
    for f in files[:limit]:
        try:
            st = os.stat(_fs_str(f))
        except OSError:  # removed after it was listed
            continue
        key = str(f)
        cached = entries.get(key)
        if (isinstance(cached, dict)
                and cached.get("mtime_ns") == st.st_mtime_ns
                and cached.get("size") == st.st_size
                and isinstance(cached.get("id"), str)
                and isinstance(cached.get("agent"), str)
                and isinstance(cached.get("steps"), int)
                and isinstance(cached.get("first"), str)
                and (not args.all or "list_cwd" in cached)):
            sid = cached["id"]
            agent = cached["agent"]
            steps_count = cached["steps"]
            first = cached["first"]
            cwd = cached.get("list_cwd")
        else:
            try:
                adapter = adapters.detect(f)
                peek_fn = getattr(adapter, "peek", None)
                if callable(peek_fn):
                    sid, agent, steps_count, raw_first, cwd = peek_fn(f)
                else:
                    run = adapter.parse(f)
                    sid = run.session_id
                    agent = run.agent
                    steps_count = len(run.steps)
                    raw_first = run.prompts[0] if run.prompts else ""
                    cwd = run.cwd
            except (OSError, ValueError) as exc:
                print(f"skipped: {exc}", file=sys.stderr)
                continue
            first = redact(raw_first).replace("\n", " ")[:60]
            updated = dict(cached) if isinstance(cached, dict) and cached.get("mtime_ns") == st.st_mtime_ns and cached.get("size") == st.st_size else {
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
            }
            updated["id"] = sid
            updated["agent"] = agent
            updated["steps"] = steps_count
            updated["first"] = first
            updated["list_cwd"] = cwd
            entries[key] = updated
            dirty = True
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {sid[:8]:<8}  {agent[:12]:<12}  {steps_count:>5} steps  {first}")
        if args.all:
            print(f"                  {cwd}")
    if dirty:
        session_cache.save(entries)
    return 0


def default_safe(fmt: str, target: Optional[Path]) -> bool:
    """Receipts written inside a Git work tree (and md/json written to stdout) are safe by
    default. Files written outside any repository keep full local paths unless --safe is passed."""
    if target is None and fmt != "html":
        return True
    try:
        folder = target.resolve().parent if target is not None else Path(os.getcwd()).resolve()
    except (OSError, RuntimeError):
        return True
    return toplevel(str(folder)) is not None


def _default_receipt_path(stem: str, ext: str) -> Path:
    candidate = Path(f"runledger-{stem}.{ext}")
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = Path(f"runledger-{stem}-{n}.{ext}")
        if not candidate.exists():
            return candidate
        n += 1


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
    ext = {"html": "html", "md": "md", "json": "json"}[args.format]
    if args.output == "-":
        target: Optional[Path] = None
    elif args.output:
        target = Path(args.output)
    else:
        target = _default_receipt_path(_file_stem(run.session_id), ext)
    safe = args.safe if args.safe is not None else default_safe(args.format, target)
    if safe:
        make_safe(run, risks)
    out = render(run, score, level, risks, args.format)
    if target is None and args.format == "md":
        out = out.rstrip("\n") + "\n\n" + md_trailer(run)
    if note:
        print(note, file=sys.stderr)
    if target is None:
        _write_stdout(out)
        if args.open:
            if args.format != "html":
                print(f"warning: --open is ignored for --format {args.format} "
                      "(only HTML receipts can be opened in a browser)", file=sys.stderr)
            else:
                print("warning: --open is ignored when writing to stdout (-o -)", file=sys.stderr)
    else:
        try:
            target.write_text(out, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {target}: {exc.strerror or _one_line(exc)}", file=sys.stderr)
            return 1
        print(f"Receipt: {target}  ·  risk {score}/100 ({level})  ·  {len(run.steps)} steps")
        print("Safe mode: on (paths shortened, e-mails masked; --no-safe shows them)" if safe
              else "Safe mode: off (full local paths; --safe hides them)", file=sys.stderr)
        if args.open:
            if args.format == "html":
                webbrowser.open(target.resolve().as_uri())
            else:
                print(f"warning: --open is ignored for --format {args.format} "
                      "(only HTML receipts can be opened in a browser)", file=sys.stderr)
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
    except ValueError as exc:  # invalid trusted proxy or retention setting
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
    if server.retention_days:
        print(f"Retention: runs, approvals and audit events older than {server.retention_days} days are deleted")
    server.start_maintenance()
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


_LEGACY_BACKUP_RE = re.compile(r"^runledger-(\d{8}-\d{6})(?:-(\d+))?\.db$")
_STEM_BACKUP_RE = re.compile(r"^runledger-(.+)-(\d{8}-\d{6})(?:-(\d+))?\.db$")
_STEM_RESERVED_RE = re.compile(r"^runledger-(.+)-(\d{8}-\d{6})(?:-(\d+))?\.db\.lock$")
_BACKUP_MARKER = ".lock"
_BACKUP_STEM_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def _backup_stem(db_path: str) -> str:
    """A file-name-safe stem for the backups of one database. Characters outside the safe set are
    percent-encoded, so every database file keeps its own retention namespace (`team a.db` and
    `team_a.db` no longer share one) and the encoding can be reversed exactly."""
    raw = Path(db_path).stem or "runledger"
    encoded = []
    for byte in raw.encode("utf-8"):
        char = chr(byte)
        encoded.append(char if char in _BACKUP_STEM_SAFE else "%%%02X" % byte)
    return "".join(encoded) or "runledger"


def _backup_seq(name: str, stem: str, stamp: str) -> Optional[int]:
    """The sequence number a backup or a reservation name carries for this database and second."""
    m = _STEM_BACKUP_RE.match(name) or _STEM_RESERVED_RE.match(name)
    if m and m.group(1) == stem and m.group(2) == stamp:
        return int(m.group(3) or 0)
    return None


def _next_backup_seq(folder: Path, stem: str, stamp: str) -> int:
    """Reservations count as taken here, so a run that is still copying keeps its name and a
    sequence number freed by pruning is never reused."""
    max_seq = -1
    for path in folder.glob(f"runledger-{stem}-{stamp}*"):
        seq = _backup_seq(path.name, stem, stamp)
        if seq is not None and seq > max_seq:
            max_seq = seq
    return max_seq + 1


def _reserve_backup_target(folder: Path, stem: str, stamp: str) -> Tuple[Path, Path]:
    """Reserve the next backup name and return `(target, reservation)`. The reservation is a
    marker file beside the target, never the `.db` name itself: concurrent backups in the same
    second take different names, and a copy that has not finished yet is invisible to retention
    (which lists `.db` files only), so it cannot consume the retention allowance. The caller
    removes its own marker.

    The sequence is read before the marker exists, so a run that paused there can read a number
    another run has since used, finished and released. The claim therefore also checks the final
    target, which the other run's completed copy keeps after its marker is gone."""
    seq = _next_backup_seq(folder, stem, stamp)
    while True:
        suffix = "" if seq == 0 else f"-{seq}"
        target = folder / f"runledger-{stem}-{stamp}{suffix}.db"
        reservation = target.with_name(target.name + _BACKUP_MARKER)
        try:
            handle = os.open(str(reservation), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            seq += 1
            continue
        os.close(handle)
        if not os.path.lexists(str(target)):
            return target, reservation
        _discard_reserved_backup(reservation)
        seq += 1


def _discard_reserved_backup(reservation: Optional[Path]) -> None:
    """Drop this run's own name reservation, so a failed copy leaves no placeholder behind."""
    if reservation is None:
        return
    try:
        reservation.unlink()
    except OSError:
        pass


def _finished_backup(path: Path) -> bool:
    """A finished backup holds data. A copy in progress is still under its `.tmp` name, and a
    placeholder an interrupted older RunLedger reserved is empty, so both stay out of retention."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _prune_backups(folder: Path, stem: str, keep: int) -> List[Path]:
    """Prune old copies of one database. Unfinished files are not backups: they neither count
    against `keep` nor make a finished copy older than they are, and they are left in place for
    the run that owns them to clean up."""
    parsed = []
    for path in folder.glob("runledger-*.db"):
        if not _finished_backup(path):
            continue
        m_leg = _LEGACY_BACKUP_RE.match(path.name)
        if m_leg:
            parsed.append((None, m_leg.group(1), int(m_leg.group(2) or 0), path))
            continue
        m_stem = _STEM_BACKUP_RE.match(path.name)
        if m_stem:
            parsed.append((m_stem.group(1), m_stem.group(2), int(m_stem.group(3) or 0), path))
    other_stems = {s for s, _, _, _ in parsed if s is not None and s != stem}
    include_legacy = not other_stems
    pool = [
        (ts, seq, path.name, path)
        for s, ts, seq, path in parsed
        if s == stem or (s is None and include_legacy)
    ]
    pool.sort(key=lambda item: (item[0], item[1], item[2]))
    old = [item[3] for item in pool[:-keep]] if keep > 0 else []
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass
    return old


def _backup_size(target: Path) -> Optional[int]:
    """The size of the copy this run wrote, or None when a concurrent retention removed it first."""
    try:
        return target.stat().st_size
    except OSError:
        return None


def cmd_backup(args) -> int:
    """Copy the team database to DIR/runledger-<stem>-YYYYmmdd-HHMMSS[-N].db and keep the newest --keep copies."""
    from .server.db import Database

    folder = Path(args.dir)
    if args.keep < 1:
        print("error: --keep must be 1 or more", file=sys.stderr)
        return 1
    if not Path(args.db).is_file():
        print(f"error: no database at {args.db}", file=sys.stderr)
        return 1
    stem = _backup_stem(args.db)
    reservation: Optional[Path] = None
    size: Optional[int] = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target, reservation = _reserve_backup_target(folder, stem, stamp)
        db = Database(args.db)
        try:
            db.backup_to(str(target))
        finally:
            db.close()
        size = _backup_size(target)
    except (OSError, sqlite3.Error) as exc:
        _discard_reserved_backup(reservation)
        print(f"error: backup failed: {exc}", file=sys.stderr)
        return 1
    _discard_reserved_backup(reservation)
    old = _prune_backups(folder, stem, args.keep)
    detail = f"{size} bytes" if size is not None else "size n/a (already pruned by another backup)"
    print(f"Backup: {target}  ({detail}; removed {len(old)} older)")
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


def print_guard_help() -> None:
    """Print the `runledger guard` help text (used when stdin is an interactive TTY)."""
    p = argparse.ArgumentParser(prog="runledger guard",
                                description="Real-time policy guard and checkpoint hooks for Claude Code.")
    gsub = p.add_subparsers(dest="guard_cmd", metavar="{install,uninstall,test}")
    gsub.add_parser("install", help="add guard and automatic checkpoint hooks to Claude Code")
    gsub.add_parser("uninstall", help="remove the guard hook from Claude Code settings.json")
    gsub.add_parser("test", help="print the decision for one PreToolUse event (JSON)")
    p.print_help()


def cmd_guard(args) -> int:
    from . import guard

    if args.guard_cmd == "install":
        return guard.cmd_install(args.project, args.global_scope,
                                 portable=getattr(args, "portable", False))
    if args.guard_cmd == "uninstall":
        return guard.cmd_uninstall(args.project, args.global_scope)
    if args.guard_cmd == "test":
        return guard.cmd_test(args.event)
    if sys.stdin is not None and sys.stdin.isatty():
        if hasattr(args, "_guard_parser"):
            args._guard_parser.print_help()
        else:
            print_guard_help()
        return 2
    return guard.main()


def _positive_int(raw: str) -> int:
    try:
        val = int(raw, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if val < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return val


def _non_negative_int(raw: str) -> int:
    try:
        val = int(raw, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be 0 or a positive integer") from None
    if val < 0:
        raise argparse.ArgumentTypeError("must be 0 or a positive integer")
    return val


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
    pl.add_argument("--limit", type=_positive_int, default=20)
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("receipt", help="build a receipt for a session")
    r_target = pr.add_mutually_exclusive_group()
    r_target.add_argument("session", nargs="?", help="session file of any supported agent (default: latest for this folder)")
    r_target.add_argument("--latest", action="store_true", help="use the latest session (default)")
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
    safe = pr.add_mutually_exclusive_group()
    safe.add_argument("--safe", dest="safe", action="store_true", default=None,
                      help="shorten local paths and mask e-mails (default inside a Git repo or for md/json to stdout)")
    safe.add_argument("--no-safe", dest="safe", action="store_false",
                      help="keep full local paths and e-mail addresses")
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

    pbk = sub.add_parser("backup", help="copy the team database safely while the server runs")
    pbk.add_argument("--db", default="runledger.db", help="SQLite database file (default runledger.db)")
    pbk.add_argument("--dir", default="backups", help="folder for the copies (default backups)")
    pbk.add_argument("--keep", type=int, default=14, help="how many copies to keep (default 14)")
    pbk.set_defaults(func=cmd_backup)

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
    p_target = pp.add_mutually_exclusive_group()
    p_target.add_argument("session", nargs="?", help="session file of any supported agent (default: latest for this folder)")
    p_target.add_argument("--latest", action="store_true", help="use the latest session (default)")
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

    pc = sub.add_parser("checkpoint", help="snapshot the working tree without changing HEAD or index")
    pc.add_argument("--label", help="checkpoint description")
    pc.add_argument("--run", "--session-id", help="associate a run/session id (or $RUNLEDGER_RUN_ID)")
    pc.add_argument("--cwd", help="Git working tree folder (default: current folder)")
    pc.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    pc.set_defaults(func=cmd_checkpoint)

    pcs = sub.add_parser("checkpoints", help="list or prune local Git checkpoints")
    pcs.add_argument("--cwd", help="Git working tree folder (default: current folder)")
    cssub = pcs.add_subparsers(dest="checkpoints_cmd")
    prune = cssub.add_parser("prune", help="delete old checkpoint refs, including safety checkpoints")
    prune.add_argument("--keep", type=_non_negative_int, required=True, help="number of newest checkpoints to keep")
    prune.add_argument("--cwd", default=argparse.SUPPRESS, help="Git working tree folder")
    pcs.set_defaults(func=cmd_checkpoints)

    prb = sub.add_parser("rollback", help="preview and restore a checkpoint, saving an undo checkpoint first")
    target = prb.add_mutually_exclusive_group()
    target.add_argument("checkpoint", nargs="?", help="checkpoint id (default: latest, including safety checkpoints)")
    target.add_argument("--run", help="restore the earliest checkpoint for a run/session id")
    target.add_argument("--last", action="store_true", help="restore the latest checkpoint, including safety checkpoints")
    prb.add_argument("--dry-run", action="store_true", help="preview without restoring files or creating a ref")
    prb.add_argument("--yes", action="store_true", help="confirm restoration without a terminal prompt")
    prb.add_argument("--cwd", help="Git working tree folder (default: current folder)")
    prb.set_defaults(func=cmd_rollback)

    pg = sub.add_parser("guard", help="real-time policy guard and checkpoint hooks for Claude Code")
    gsub = pg.add_subparsers(dest="guard_cmd", metavar="{install,uninstall,test}")
    pgi = gsub.add_parser("install", help="add guard and automatic checkpoint hooks to Claude Code")
    scope = pgi.add_mutually_exclusive_group()
    scope.add_argument("--project", metavar="PATH", help="project folder: writes PATH/.claude/settings.json (default: current folder)")
    scope.add_argument("--global", dest="global_scope", action="store_true", help="writes ~/.claude/settings.json")
    pgi.add_argument("--portable", action="store_true",
                     help="write bare 'runledger ...' hook commands instead of the current Python interpreter path")
    pgu = gsub.add_parser("uninstall", help="remove the guard hook from Claude Code settings.json")
    uscope = pgu.add_mutually_exclusive_group()
    uscope.add_argument("--project", metavar="PATH", help="project folder: edits PATH/.claude/settings.json (default: current folder)")
    uscope.add_argument("--global", dest="global_scope", action="store_true", help="edits ~/.claude/settings.json")
    pgt = gsub.add_parser("test", help="print the decision for one PreToolUse event (JSON)")
    pgt.add_argument("event", help="the event as a JSON object")
    pg.set_defaults(func=cmd_guard, _guard_parser=pg)

    args = p.parse_args(argv)
    _tolerant_console()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
