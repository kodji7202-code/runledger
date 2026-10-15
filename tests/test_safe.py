"""Safe mode core (runledger/safe.py): path rules, text masking, idempotency and make_safe."""
import pytest

from runledger.parser import Run, Step, Usage
from runledger.risk import Risk
from runledger.safe import SafeContext, context, make_safe, safe_cwd, safe_path, safe_text
from runledger.summarize import file_changes

ROOT = "/srv/work/acme/app"
CTX = SafeContext(root=ROOT, home="/home/ana", prefixes=["/mnt/corp"])


@pytest.mark.parametrize("path, expected", [
    (f"{ROOT}/src/a.py", "src/a.py"),
    (ROOT, "."),
    ("/home/ana/.ssh/config", "~/.ssh/config"),
    ("/home/bob/notes.txt", "~/notes.txt"),
    ("/Users/carol/x", "~/x"),
    ("/root/.bashrc", "~/.bashrc"),
    ("/mnt/corp/share/doc.md", "…/share/doc.md"),
    ("/tmp/build/out.log", "/tmp/build/out.log"),
    ("/srv/work/acme/lib/util.py", "…/lib/util.py"),
    ("/usr/bin/python3", "/usr/bin/python3"),
    ("src/rel.py", "src/rel.py"),
    ("", ""),
])
def test_safe_path_rules(path, expected):
    assert safe_path(path, CTX) == expected


def test_windows_paths():
    ctx = SafeContext(root="C:/Users/dev/proj")
    assert safe_path("C:\\Users\\dev\\proj\\src\\a.py", ctx) == "src/a.py"
    assert safe_path("C:\\Users\\dev\\Desktop\\x.txt", ctx) == "~/Desktop/x.txt"
    assert safe_path("D:\\data\\x", ctx) == "D:\\data\\x"  # no rule applies: kept as written
    text = safe_text(r"edited C:\Users\dev\proj\src\a.py and C:\Users\eve\b.txt", ctx)
    assert text == "edited src/a.py and ~/b.txt"


def test_extra_prefixes_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RUNLEDGER_SAFE_PREFIXES", "/opt/secret-co")
    ctx = context(None, root=ROOT)
    assert safe_path("/opt/secret-co/build/x", ctx) == "…/build/x"


def test_free_text_paths_emails_urls_and_tokens():
    text = (f"cd {ROOT} && cat /home/ana/key.txt > {ROOT}/out.txt; mail ana@example.com; "
            "git clone https://bob:hunter2@git.example.com/r.git; see https://example.com/docs/a; "
            f"export GITHUB_TOKEN=ghp_{'a' * 36}. Done in {ROOT}/src.")
    out = safe_text(text, CTX)
    assert ROOT not in out and "/home/ana" not in out
    assert "cd . && cat ~/key.txt > out.txt" in out
    assert "[EMAIL]" in out and "ana@example.com" not in out
    assert "https://[REDACTED]@git.example.com/r.git" in out and "hunter2" not in out
    assert "https://example.com/docs/a" in out
    assert "ghp_" + "a" * 36 not in out
    assert out.endswith("Done in src.")


def test_colon_separated_paths_are_each_rewritten():
    text = f"RUNLEDGER_CORPUS={ROOT}/a.jsonl:/home/ana/b.jsonl:/srv/work/acme/c.jsonl"
    assert safe_text(text, CTX) == "RUNLEDGER_CORPUS=a.jsonl:~/b.jsonl:…/c.jsonl"
    assert safe_text("see https://example.com/home/ana/x", CTX) == "see https://example.com/home/ana/x"


@pytest.mark.parametrize("text", [
    f"wrote {ROOT}/a.py and /home/ana/b and /srv/work/acme/lib/c and /mnt/corp/d",
    "user@example.org at https://u:p@h.example/x",
    "C:\\Users\\dev\\x.txt",
])
def test_safe_text_is_idempotent(text):
    once = safe_text(text, CTX)
    assert safe_text(once, CTX) == once


def test_safe_cwd_is_folder_name_plus_subfolder():
    assert safe_cwd(ROOT, CTX) == "app"
    assert safe_cwd(f"{ROOT}/pkg/core", CTX) == "app/pkg/core"
    assert safe_cwd("/home/ana/elsewhere", CTX) == "~/elsewhere"
    assert safe_cwd(None, CTX) is None


def _run():
    cwd = f"{ROOT}/svc"
    steps = [
        Step(1, "Write", {"file_path": f"{cwd}/a.py", "content": "x\ny\n"}, "t1", "m", None),
        Step(2, "Bash", {"command": f"cat > {cwd}/b.txt <<'EOF'\nhi\nEOF"}, "t2", "m", None),
        Step(3, "Write", {"file_path": "/home/ana/scratch.py", "content": "z\n"}, "t3", "m", None),
    ]
    for s in steps:
        s.summary = f"did {s.input.get('file_path') or s.input.get('command')}"
        s.risks = [Risk("low", "x", f"touches {cwd}/a.py for ana@example.com", s.index)]
    return Run(session_id="sess-1", path=f"/home/ana/.factory/sessions/{'x' * 3}.jsonl", cwd=cwd,
               git_branch="feat/x", started=None, ended=None,
               prompts=[f"Fix {cwd}/a.py, ping ana@example.com"], steps=steps,
               final_message=f"Done, see {ROOT}/svc/a.py", usage=Usage(), models={"claude-x": Usage()},
               overall_summary=f"Changed {cwd}/a.py", guard=[{"reason": f"blocked {ROOT}/secret"}],
               quality={"signals": [{"note": f"{ROOT}/svc/a.py edited"}]})


def test_make_safe_rewrites_everything_shown_and_keeps_file_counts():
    run = _run()
    risks = [r for s in run.steps for r in s.risks]
    before = file_changes(run)
    make_safe(run, risks, root=ROOT)
    after = file_changes(run)
    assert list(after) == ["a.py", "b.txt", "~/scratch.py"]
    assert [(c.added, c.created) for c in after.values()] == [(c.added, c.created) for c in before.values()]
    assert run.safe
    assert run.cwd == "app/svc"
    assert run.path == "xxx.jsonl"
    assert (run.session_id, run.git_branch, list(run.models)) == ("sess-1", "feat/x", ["claude-x"])
    shown = repr((run.prompts, run.final_message, run.overall_summary, [s.summary for s in run.steps],
                  [s.input for s in run.steps], [r.reason for r in risks], run.guard, run.quality))
    assert ROOT not in shown and "/home/ana" not in shown and "ana@example.com" not in shown
    # a second pass changes nothing
    snapshot = shown
    make_safe(run, risks, root=ROOT)
    assert repr((run.prompts, run.final_message, run.overall_summary, [s.summary for s in run.steps],
                 [s.input for s in run.steps], [r.reason for r in risks], run.guard, run.quality)) == snapshot
    assert list(file_changes(run)) == ["a.py", "b.txt", "~/scratch.py"]


def test_without_a_known_root_paths_keep_only_their_last_parts():
    ctx = SafeContext(root=None, home="/home/ana")
    assert safe_path("/srv/work/acme/app/src/a.py", ctx) == "…/src/a.py"
    assert safe_path("/usr/bin/python3", ctx) == "/usr/bin/python3"
    assert safe_path("/tmp/x/y/z", ctx) == "/tmp/x/y/z"
    assert safe_path("/home/ana/p/q", ctx) == "~/p/q"
    assert context(".").root is None  # a relative cwd is never resolved against RunLedger's folder


def test_relative_cwd_is_not_resolved_against_the_current_folder(tmp_path, monkeypatch):
    import subprocess
    import shutil as _shutil
    from runledger.gitinfo import current_branch, toplevel
    from runledger.risk import _outside
    if _shutil.which("git"):
        subprocess.run(["git", "init", "-q", "-b", "here", str(tmp_path)], check=True)
        monkeypatch.chdir(tmp_path)
        assert toplevel(str(tmp_path)) is not None
    assert current_branch(".") is None and toplevel(".") is None
    assert not _outside("/srv/elsewhere/a.py", ".")
