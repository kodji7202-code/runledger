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


def test_truncated_path_inside_root_is_hidden():
    from runledger.safe import SafeContext, safe_text
    ctx = SafeContext(root="/private/var/folders/ab/T/pytest-0/case/repo")
    assert safe_text("Ran `cd /private/var/folders/ab/T/pytest-0/case/r…`", ctx) == "Ran `cd …`"
    assert safe_text("cat /private/var/folders/ab/T/pytest-0/case/repo/src/app.…", ctx) == "cat src/app.…"
    assert safe_text("cat /home/alice/notes/to…", SafeContext(root="/work/repo")) == "cat ~/notes/to…"


# ---------------------------------------------------------------- Spaced homes, git@ remotes, foreign paths (VAL-CORE-032..034)

def test_home_folders_with_spaces_are_fully_masked():
    ctx = SafeContext(root="D:/work/repo")
    win_out = safe_text(r"edited C:\Users\John Smith\proj\a.py today", ctx)
    assert win_out == "edited ~/proj/a.py today"
    assert "John" not in win_out and "Smith" not in win_out

    posix_out = safe_text("cat /home/john smith/notes.txt", ctx)
    assert posix_out == "cat ~/notes.txt"
    assert "john" not in posix_out and "smith" not in posix_out

    mac_out = safe_text("open /Users/Mary Jane Doe/notes.txt", ctx)
    assert mac_out == "open ~/notes.txt"
    for name in ("Mary", "Jane", "Doe"):
        assert name not in mac_out


def test_scp_and_ssh_git_remotes_are_not_redacted_as_emails():
    ctx = SafeContext(root=ROOT)
    cmd = (
        "git clone git@github.com:org/repo.git && "
        "git remote set-url origin git@gitlab.internal.corp:team/service.git && "
        "git fetch ssh://git@github.com/org/repo.git && "
        "git fetch ssh://alice@host.example.com/repo.git && "
        "git config user.email bob@example.com && "
        "echo 'Co-authored-by: alice@corp.io' && "
        "git clone https://user:pass@github.com/org/repo.git"
    )
    out = safe_text(cmd, ctx)
    assert "git@github.com:org/repo.git" in out
    assert "git@gitlab.internal.corp:team/service.git" in out
    assert "ssh://git@github.com/org/repo.git" in out
    assert "ssh://[REDACTED]@host.example.com/repo.git" in out
    assert "alice@host.example.com" not in out
    assert "git config user.email [EMAIL]" in out
    assert "Co-authored-by: [EMAIL]" in out
    assert "bob@example.com" not in out and "alice@corp.io" not in out
    assert "https://[REDACTED]@github.com/org/repo.git" in out
    assert "user:pass" not in out


def test_foreign_absolute_paths_are_trimmed_when_root_is_known():
    for root in ("C:/Users/dev/proj", "/home/dev/proj"):
        ctx = SafeContext(root=root)
        win_foreign = safe_path(r"D:\secret-corp\client-acme\payroll\q1.xlsx", ctx)
        assert win_foreign == "…/payroll/q1.xlsx"
        assert "secret-corp" not in win_foreign and "client-acme" not in win_foreign

        posix_foreign = safe_path("/opt/proprietary-vendor/sdk-v3/include/header.h", ctx)
        assert posix_foreign == "…/include/header.h"
        assert "proprietary-vendor" not in posix_foreign and "sdk-v3" not in posix_foreign

        text_out = safe_text(
            r"copied D:\secret-corp\client-acme\payroll\q1.xlsx and /opt/proprietary-vendor/sdk-v3/include/header.h",
            ctx,
        )
        assert text_out == "copied …/payroll/q1.xlsx and …/include/header.h"

        for sys_path in ("/usr/bin/python3", "/etc/hosts", "/bin/sh"):
            assert safe_path(sys_path, ctx) == sys_path
        assert safe_path("/tmp/build.log", ctx) == "/tmp/build.log"


def test_spaced_foreign_windows_paths_are_fully_masked():
    """CORE-SCR-013: a foreign drive path containing spaces is masked as a whole, quoted
    or bare, so neither the drive nor the folders above the last two parts survive."""
    ctx = SafeContext(root="D:/repo", home="C:/Users/local")
    assert safe_text(r'type "E:\Work\Client X\secret.txt"', ctx) == 'type "…/Client X/secret.txt"'
    assert safe_text(r"type E:\Work\Client X\secret.txt", ctx) == "type …/Client X/secret.txt"
    assert safe_text(r"type E:\Work\Client X\secret.txt now", ctx) == "type …/Client X/secret.txt now"
    assert safe_text(r"copy D:\other\proj\file.py E:\Work\Client X\secret.txt", ctx) == (
        "copy …/proj/file.py …/Client X/secret.txt")
    for leaked in ("E:", "E:\\", "Work", "D:\\other"):
        assert leaked not in safe_text(
            r'type "E:\Work\Client X\secret.txt" and E:\Work\Client X\secret.txt and D:\other\proj\file.py', ctx)


def test_spaced_and_non_ascii_home_usernames_are_fully_masked():
    """CORE-SCR-014: an unknown home user name is consumed up to a separator, quote or end
    of line with no ASCII or capitalisation restriction, so a surname never leaks."""
    ctx = SafeContext(root="D:/repo", home="C:/Users/local")
    posix = safe_text("cat /home/john smith/notes.txt today", ctx)
    assert posix == "cat ~/notes.txt today"
    assert safe_text("cd /home/john smith/work && ls", ctx) == "cd ~/work && ls"
    mac = safe_text("ls /Users/mary jane doe", ctx)
    assert mac == "ls ~"
    win = safe_text(r"cat C:\Users\José Smith\Documents\x.txt", ctx)
    assert win == "cat ~/Documents/x.txt"
    for leaked in ("john", "smith", "mary", "jane", "doe", "José", "Smith"):
        assert leaked not in posix + mac + win


def test_git_scp_exemption_needs_a_remote_path():
    """CORE-SCR-015: only `git@host:path` with a non-empty path is an scp remote; a real
    address followed by a prose colon, a bare colon or a slash is still masked."""
    ctx = SafeContext(root="D:/repo")
    out = safe_text("Contact git@example.com: please respond", ctx)
    assert out == "Contact [EMAIL]: please respond"
    assert "git@example.com" not in out
    assert safe_text("git@example.com:", ctx) == "[EMAIL]:"
    assert safe_text("git@example.com/ x", ctx) == "[EMAIL]/ x"
    assert safe_text("git clone git@github.com:org/repo.git", ctx) == (
        "git clone git@github.com:org/repo.git")
    assert safe_text("git remote add o git@gitlab.internal.corp:team/service.git", ctx) == (
        "git remote add o git@gitlab.internal.corp:team/service.git")
    assert safe_text("git fetch ssh://git@github.com/org/repo", ctx) == (
        "git fetch ssh://git@github.com/org/repo")
