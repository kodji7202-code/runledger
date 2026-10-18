"""Safe mode wiring: the default rule, --safe/--no-safe, the JSON `safe` key and the PR comment."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from runledger import github
from runledger.cli import main

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
README = Path(__file__).resolve().parents[1] / "README.md"


def _session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    steps = [
        {"tool": "Write", "input": {"file_path": str(repo / "src" / "app.py"), "content": "a\nb\n"}},
        {"tool": "Bash", "input": {"command": f"cd {repo} && echo hi > notes.txt"}},
        {"tool": "Edit", "input": {"file_path": str(repo / "README.md"), "old_string": "a",
                                   "new_string": "b"}},
    ]
    path = tmp_path / "s.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "agent": "demo", "session_id": "safe-1",
                                "cwd": str(repo), "prompts": [f"Fix {repo}/src/app.py for dev@example.com"],
                                "steps": steps}), encoding="utf-8")
    return path


def _receipt(capsys, *args):
    rc = main(["receipt", *args])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return captured.out, captured.err


@pytest.mark.parametrize("fmt", ["md", "json"])
def test_stdout_md_and_json_are_safe_by_default(tmp_path, capsys, fmt):
    out, err = _receipt(capsys, str(_session(tmp_path)), "--format", fmt, "-o", "-")
    assert str(tmp_path) not in out
    assert "dev@example.com" not in out and "[EMAIL]" in out
    assert err == ""  # stdout stays a clean document; the JSON `safe` key records the mode
    if fmt == "json":
        data = json.loads(out)
        assert data["safe"] is True
        assert data["cwd"] == "repo"
        assert [f["path"] for f in data["files"]] == ["src/app.py", "notes.txt", "README.md"]
        assert data["totals"]["files_changed"] == 3
    else:
        assert "**Files changed:** 3" in out


def test_html_is_not_safe_by_default(tmp_path, capsys):
    target = tmp_path / "r.html"
    _, err = _receipt(capsys, str(_session(tmp_path)), "--format", "html", "-o", str(target))
    assert str(tmp_path / "repo") in target.read_text(encoding="utf-8")
    assert "Safe mode: off" in err


def test_file_outside_a_repository_is_not_safe_by_default(tmp_path, capsys):
    target = tmp_path / "r.json"
    _receipt(capsys, str(_session(tmp_path)), "--format", "json", "-o", str(target))
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["safe"] is False
    assert data["cwd"] == str(tmp_path / "repo")


@needs_git
def test_file_inside_a_repository_is_safe_by_default(tmp_path, capsys):
    out_repo = tmp_path / "other"
    out_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(out_repo)], check=True)
    target = out_repo / "r.md"
    _receipt(capsys, str(_session(tmp_path)), "--format", "md", "-o", str(target))
    assert str(tmp_path) not in target.read_text(encoding="utf-8")


@needs_git
def test_html_safe_inside_repo(tmp_path, capsys):
    sess = str(_session(tmp_path))
    out_repo = tmp_path / "repo_out"
    out_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(out_repo)], check=True)
    target = out_repo / "r.html"
    _, err = _receipt(capsys, sess, "--format", "html", "-o", str(target))
    body = target.read_text(encoding="utf-8")
    assert str(tmp_path) not in body
    assert "dev@example.com" not in body and "[EMAIL]" in body
    assert "src/app.py" in body
    assert "Safe mode: on" in err and "--no-safe" in err

    _, err_full = _receipt(capsys, sess, "--format", "html", "--no-safe", "-o", str(target))
    body_full = target.read_text(encoding="utf-8")
    assert str(tmp_path / "repo") in body_full
    assert "dev@example.com" in body_full
    assert "Safe mode: off" in err_full and "--safe" in err_full


def test_readme_safe_mode_default_matches_the_code():
    """`cli.default_safe`: a receipt written inside a repo is safe whatever its format, HTML
    included; only Markdown and JSON to stdout are always safe."""
    paragraph = README.read_text(encoding="utf-8")
    paragraph = " ".join(paragraph.split("**Safe mode**", 1)[1].split("\n\n", 1)[0].split())
    assert "HTML receipts keep full paths" not in paragraph
    assert "inside a Git repository (HTML included)" in paragraph
    assert "Markdown and JSON written to stdout" in paragraph
    assert "written outside a repository keeps full paths unless `--safe` is passed" in paragraph



def test_overrides(tmp_path, capsys):
    session = str(_session(tmp_path))
    out, _ = _receipt(capsys, session, "--format", "json", "-o", "-", "--no-safe")
    data = json.loads(out)
    assert data["safe"] is False and data["cwd"] == str(tmp_path / "repo")
    target = tmp_path / "r.html"
    _receipt(capsys, session, "--format", "html", "-o", str(target), "--safe")
    assert str(tmp_path) not in target.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["receipt", session, "--safe", "--no-safe"])


def test_pr_comment_is_always_safe(tmp_path, capsys, monkeypatch):
    for name in ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY",
                 "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    assert github.main(["comment", "--session", str(_session(tmp_path)), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path) not in out and "dev@example.com" not in out
    assert "src/app.py" in out


def test_summary_path_cut_inside_a_long_root_is_hidden(tmp_path, capsys):
    """Regression (macOS CI): the step summary cuts a long `cd <root>` path with `…`
    inside the root folder name; the partial path must not leak the folder layout."""
    deep = tmp_path / ("very-long-folder-name-" * 4) / "nested" / "repo"
    deep.mkdir(parents=True)
    steps = [{"tool": "Bash", "input": {"command": f"cd {deep} && echo hi > notes.txt"}}]
    path = tmp_path / "long.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "agent": "demo", "session_id": "safe-long",
                                "cwd": str(deep), "prompts": ["x"], "steps": steps}), encoding="utf-8")
    for fmt in ("md", "json"):
        out, _ = _receipt(capsys, str(path), "--format", fmt, "-o", "-")
        assert str(tmp_path) not in out
        assert "very-long-folder-name" not in out


def test_sentence_final_email_is_masked_in_md_json_and_html(tmp_path, capsys):
    """VAL-PUSH-001 / VAL-CROSS-009: an address that ends a sentence is masked in every
    receipt format, and the full stop that ends the sentence stays in the text."""
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "sf.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-sentence-final",
        "cwd": str(repo),
        "prompts": ["Fix src/app.py and mail dev@example.com."],
        "final_message": "Pinged dev@example.com, then stopped.",
        "steps": [{"tool": "Bash", "input": {"command": "git config user.email dev@example.com."}}],
    }), encoding="utf-8")
    for fmt in ("md", "json"):
        out, _ = _receipt(capsys, str(path), "--format", fmt, "-o", "-", "--safe")
        assert "example.com" not in out and "dev@example.com" not in out
        assert "[EMAIL]." in out
    target = tmp_path / "r.html"
    _receipt(capsys, str(path), "--format", "html", "-o", str(target), "--safe")
    body = target.read_text(encoding="utf-8")
    assert "dev@example.com" not in body and "[EMAIL]." in body


def test_safe_receipt_masks_spaced_homes_foreign_paths_and_keeps_git_remotes(tmp_path, capsys):
    steps = [
        {"tool": "Write", "input": {"file_path": r"C:\Users\John Smith\proj\a.py", "content": "x = 1\n"}},
        {"tool": "Write", "input": {"file_path": "/home/john smith/notes.txt", "content": "y = 1\n"}},
        {"tool": "Write", "input": {"file_path": "/Users/Mary Jane Doe/notes.txt", "content": "z = 1\n"}},
        {"tool": "Read", "input": {"file_path": r"D:\secret-corp\client-acme\payroll\q1.xlsx"}},
        {"tool": "Read", "input": {"file_path": "/opt/proprietary-vendor/sdk-v3/include/header.h"}},
        {"tool": "Bash", "input": {"command": "git clone git@github.com:org/repo.git"}},
        {"tool": "Bash", "input": {"command": "git remote set-url origin git@gitlab.internal.corp:team/service.git"}},
        {"tool": "Bash", "input": {"command": "git fetch ssh://git@github.com/org/repo.git"}},
        {"tool": "Bash", "input": {"command": "git fetch ssh://alice@host.example.com/repo.git"}},
        {"tool": "Bash", "input": {"command": "git config user.email bob@example.com"}},
        {"tool": "Bash", "input": {"command": "git clone https://user:pass@github.com/org/repo.git"}},
        {"tool": "Bash", "input": {"command": "/usr/bin/python3 /tmp/build.log"}},
    ]
    path = tmp_path / "f3.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-f3",
        "cwd": "C:/Users/dev/proj",
        "prompts": [
            r"Check C:\Users\John Smith\proj\a.py, /home/john smith/notes.txt, /Users/Mary Jane Doe/notes.txt; Co-authored-by: alice@corp.io"
        ],
        "steps": steps,
    }), encoding="utf-8")
    for fmt in ("md", "json"):
        out, _ = _receipt(capsys, str(path), "--format", fmt, "-o", "-", "--safe")
        text = out if fmt == "md" else json.dumps(json.loads(out), ensure_ascii=False)
        assert "~/proj/a.py" in text
        assert "~/notes.txt" in text
        for leaked in ("John", "Smith", "john", "smith", "Mary", "Jane", "Doe",
                       "secret-corp", "client-acme", "proprietary-vendor", "sdk-v3",
                       "bob@example.com", "alice@corp.io", "alice@host.example.com", "user:pass"):
            assert leaked not in text
        assert "git@github.com:org/repo.git" in text
        assert "git@gitlab.internal.corp:team/service.git" in text
        assert "ssh://git@github.com/org/repo.git" in text
        assert "ssh://[REDACTED]@host.example.com/repo.git" in text
        assert "https://[REDACTED]@github.com/org/repo.git" in text
        assert "[EMAIL]" in text
        assert "…/payroll/q1.xlsx" in text
        assert "…/include/header.h" in text
        assert "/usr/bin/python3" in text
        assert "/tmp/build.log" in text


def test_safe_receipt_masks_an_unquoted_spaced_redirect_target(tmp_path, capsys):
    """The residual leak from the paths/safe scrutiny: `echo hi > E:\\Work\\Client y\\z.txt` showed
    the fragment `E:\\Work\\Client` in the safe receipt, with no part of it masked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "scr.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-redirect",
        "cwd": str(repo),
        "prompts": [r"echo hi > E:\Work\Client y\z.txt"],
        "steps": [{"tool": "Bash", "input": {"command": r"echo hi > E:\Work\Client y\z.txt"}}],
    }), encoding="utf-8")

    md, _ = _receipt(capsys, str(path), "--format", "md", "-o", "-", "--safe")
    assert "E:" not in md and "Work" not in md
    assert "…/Client y/z.txt" in md

    raw, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--safe")
    assert "E:" not in raw and "Work" not in raw
    data = json.loads(raw)
    assert data["safe"] is True
    outside = [r for r in data["risk"]["reasons"] if r["code"] == "shell_outside"]
    assert [(r["severity"], r["reason"]) for r in outside] == [
        ("low", "Shell command wrote outside the working folder (…/Client y/z.txt)")]

    full, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--no-safe")
    assert json.loads(full)["risk"]["reasons"][0]["reason"].endswith("(E:\\Work\\Client y\\z.txt)")


def test_safe_receipt_masks_three_word_folders_and_repeated_spaces(tmp_path, capsys):
    """CORE-SCR-R2-005/006 end to end: a three-word folder name and a home user name with
    repeated spaces leak no part of themselves in safe Markdown or JSON receipts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    steps = [
        {"tool": "Bash", "input": {"command": r'type "E:\Work\Client X Corp\secret.txt"'}},
        {"tool": "Bash", "input": {"command": r"type E:\Work\Client X Corp\secret.txt"}},
        {"tool": "Bash", "input": {"command": r'cat "C:\Users\John  Smith\Documents\x.txt"'}},
        {"tool": "Bash", "input": {"command": 'cat "/home/john  smith/notes.txt"'}},
    ]
    path = tmp_path / "scr2.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-scr2",
        "cwd": str(repo),
        "prompts": [r"type E:\Work\Client X Corp\secret.txt and C:\Users\John  Smith\Documents\x.txt"],
        "steps": steps,
    }), encoding="utf-8")
    for fmt in ("md", "json"):
        out, _ = _receipt(capsys, str(path), "--format", fmt, "-o", "-", "--safe")
        text = out if fmt == "md" else json.dumps(json.loads(out), ensure_ascii=False)
        for leaked in ("E:", "Work", "John", "Smith", "john", "smith"):
            assert leaked not in text
        assert "…/Client X Corp/secret.txt" in text
        assert "~/Documents/x.txt" in text and "~/notes.txt" in text
    full, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-", "--no-safe")
    assert json.loads(full)["safe"] is False
    for kept in (r"E:\Work\Client X Corp\secret.txt", r"C:\Users\John  Smith\Documents\x.txt"):
        assert json.dumps(kept)[1:-1] in full  # JSON escapes the backslashes


def test_receipt_flags_a_delete_operand_after_a_spaced_drive_path(tmp_path, capsys):
    """CORE-SCR-R2-004: `../outside.txt` after a drive path is an operand of its own, so the
    receipt keeps the high outside-delete finding for it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    command = f"rm {str(repo).replace(chr(92), '/')}/a.txt ../outside.txt"
    path = tmp_path / "del.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-del",
        "cwd": str(repo),
        "prompts": [command],
        "steps": [{"tool": "Bash", "input": {"command": command}}],
    }), encoding="utf-8")
    out, _ = _receipt(capsys, str(path), "--format", "json", "-o", "-")
    outside = [r for r in json.loads(out)["risk"]["reasons"] if r["code"] == "shell_outside"]
    assert [(r["severity"], r["reason"]) for r in outside] == [
        ("high", "Shell command referenced a path outside the working folder (../outside.txt)")]


def test_safe_receipt_masks_spaced_paths_usernames_and_prose_emails(tmp_path, capsys):
    """CORE-SCR-013/014/015 end to end: a spaced foreign drive path, a spaced or non-ASCII
    home user name and a real address followed by a colon are masked in both formats."""
    repo = tmp_path / "repo"
    repo.mkdir()
    steps = [
        {"tool": "Bash", "input": {"command": r'type "E:\Work\Client X\secret.txt"'}},
        {"tool": "Bash", "input": {"command": r"type E:\Work\Client X\secret.txt"}},
        {"tool": "Bash", "input": {"command": "cd /home/john smith/work && ls"}},
        {"tool": "Bash", "input": {"command": "ls /Users/mary jane doe"}},
        {"tool": "Bash", "input": {"command": r"cat C:\Users\José Smith\Documents\x.txt"}},
        {"tool": "Bash", "input": {"command": "Contact git@example.com: please respond"}},
        {"tool": "Bash", "input": {"command": "git clone git@github.com:org/repo.git"}},
    ]
    path = tmp_path / "scr.runledger.json"
    path.write_text(json.dumps({
        "runledger_format": 1,
        "agent": "demo",
        "session_id": "safe-scr",
        "cwd": str(repo),
        "prompts": [r"type E:\Work\Client X\secret.txt then cd /home/john smith/work"],
        "steps": steps,
    }), encoding="utf-8")
    for fmt in ("md", "json"):
        out, _ = _receipt(capsys, str(path), "--format", fmt, "-o", "-", "--safe")
        text = out if fmt == "md" else json.dumps(json.loads(out), ensure_ascii=False)
        for leaked in ("E:", "Work", "john", "smith", "Mary", "Jane", "Doe", "José", "git@example.com"):
            assert leaked not in text
        # the last two components stay (VAL-CORE-034); everything above them is gone
        assert "…/Client X/secret.txt" in text
        assert "git@github.com:org/repo.git" in text
        assert "~/work" in text and "~/Documents/x.txt" in text
        assert "[EMAIL]: please respond" in text
