import json

import pytest

from runledger.cli import main
from runledger.parser import Run, Step, Usage
from runledger.paths import (is_unc, is_windows_style, join, label_spaced_drive_path, native,
                             relative, show_spaced_drive_paths, spaced_drive_labels,
                             strip_long_prefix, within)
from runledger.shell_writes import shell_deletes, shell_writes
from runledger.summarize import file_changes


@pytest.mark.parametrize("path,cwd,expected", [
    ("/p/src/a.py", "/p", "src/a.py"),
    ("/p/src/a.py", "/p/", "src/a.py"),
    ("/p/./src/../src/a.py", "/p", "src/a.py"),
    ("/pp/a.py", "/p", "/pp/a.py"),
    ("/p", "/p", "/p"),
    ("/other/a.py", "/p", "/other/a.py"),
    ("src/a.py", "/p", "src/a.py"),
    ("C:\\repo\\src\\a.py", "C:\\repo", "src/a.py"),
    ("c:/Repo/src/A.py", "C:\\repo", "src/A.py"),
    ("C:\\repo\\src/a.py", "C:/repo/", "src/a.py"),
    ("C:\\repo2\\a.py", "C:\\repo", "C:\\repo2\\a.py"),
    ("D:\\repo\\a.py", "C:\\repo", "D:\\repo\\a.py"),
    ("src\\a.py", "C:\\repo", "src/a.py"),
    ("/p/a.py", None, "/p/a.py"),
    ("", "/p", ""),
])
def test_relative_handles_posix_and_windows(path, cwd, expected):
    assert relative(path, cwd) == expected


@pytest.mark.parametrize("command,expected", [
    ('rm "a b.txt"', ["a b.txt"]),
    ("rm -rf build/ && pytest -q", ["build"]),
    ("rm -f a.py b.py; echo done > log.txt", ["a.py", "b.py"]),
    ("cd sub && rm x.py", ["sub/x.py"]),
    ("git rm old.py", ["old.py"]),
    ("git rm -r --cached secrets.env", []),
    ("git -C . rm --cached k.py", []),
    ("git -C repo rm k.py", ["repo/k.py"]),
    ("unlink stale.lock", ["stale.lock"]),
    ("sudo rm -- -weird.txt", []),
    ("rm *.pyc $TMP/x", []),
    ("rm /dev/null", []),
    ("echo rm a.py", []),
    ("rm 'unterminated", []),
])
def test_shell_deletes(command, expected):
    assert shell_deletes(command) == expected


def _changes(command, cwd="/p"):
    run = Run("s", "/x.jsonl", cwd, None, None, None, [],
              [Step(1, "Bash", {"command": command}, "t", None, None)], "", Usage(), {})
    return file_changes(run)


def test_rm_targets_are_relative_unquoted_and_skip_outside_and_temp():
    fc = _changes('rm "a b.txt" /p/src/x.py /tmp/t.log /etc/other.conf; git rm --cached k.py')
    assert sorted(fc) == ["a b.txt", "src/x.py"]
    assert all(c.deleted and c.shell for c in fc.values())


def test_windows_session_lists_files_relative_and_once(tmp_path, capsys):
    cwd = "C:\\Users\\dev\\app"
    steps = [
        {"tool": "Write", "input": {"file_path": cwd + "\\src\\a.py", "content": "x = 1\n"}},
        {"tool": "Edit", "input": {"file_path": "c:/users/dev/app/src/a.py",
                                   "old_string": "x = 1", "new_string": "x = 2"}},
        {"tool": "Read", "input": {"file_path": cwd + "\\README.md"}},
        {"tool": "Write", "input": {"file_path": "D:\\elsewhere\\b.py", "content": "y\n"}},
    ]
    path = tmp_path / "win.runledger.json"
    path.write_text(json.dumps({"runledger_format": 1, "session_id": "win-1", "cwd": cwd,
                                "steps": steps}), encoding="utf-8")
    assert main(["receipt", str(path), "--format", "json", "-o", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert sorted(f["path"] for f in data["files"]) == ["D:\\elsewhere\\b.py", "src/a.py"]
    assert data["steps"][2]["summary"] == "Read README.md"
    for fmt in ("md", "html"):
        assert main(["receipt", str(path), "--format", fmt, "-o", "-"]) == 0
        assert "src/a.py" in capsys.readouterr().out


# ---------------------------------------------------------------- Windows paths on any host

@pytest.mark.parametrize("base,path,expected", [
    ("/srv/app", "pkg", "/srv/app/pkg"),
    ("/srv/app/", "./pkg/../lib", "/srv/app/lib"),
    ("/srv/app", "/etc/x", "/etc/x"),
    (None, "a/./b", "a/b"),
    ("C:\\Users\\dev\\repo", "notes.txt", "C:\\Users\\dev\\repo\\notes.txt"),
    ("C:\\Users\\dev\\repo", "tests/test_api.py", "C:\\Users\\dev\\repo\\tests\\test_api.py"),
    ("C:/Users/dev/repo", "..\\other", "C:\\Users\\dev\\other"),
    ("C:\\", "x", "C:\\x"),
    ("C:\\repo", "a:b", "C:\\repo\\a:b"),
    ("/srv/app", "D:\\data\\x.db", "D:\\data\\x.db"),
])
def test_join_keeps_the_style_of_the_base(base, path, expected):
    assert join(base, path) == expected


def test_native_gives_windows_paths_backslashes():
    assert native("C:/Users/dev/repo") == "C:\\Users\\dev\\repo"
    assert native("/srv/app") == "/srv/app"
    assert native("rel/a") == "rel/a"


def test_git_toplevel_on_windows_is_a_native_path(monkeypatch):
    from runledger import gitinfo
    monkeypatch.setattr(gitinfo, "_in_repo", lambda cwd: True)
    monkeypatch.setattr(gitinfo, "_git", lambda cwd, *args: "C:/Users/dev/my-repo\n")
    assert gitinfo.toplevel("C:\\Users\\dev\\my-repo\\src") == "C:\\Users\\dev\\my-repo"


WIN_REPO = "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin\\pytest-0\\repo"


@pytest.mark.parametrize("command,expected", [
    (f"cd {WIN_REPO} && echo hi > notes.txt", [WIN_REPO + "\\notes.txt"]),
    (f"echo hi > {WIN_REPO}\\out.txt", [WIN_REPO + "\\out.txt"]),
    (f"echo hi >{WIN_REPO}\\out.txt", [WIN_REPO + "\\out.txt"]),
    (f"cd '{WIN_REPO}' && touch a.txt", [WIN_REPO + "\\a.txt"]),
    (f'cd "{WIN_REPO}" && touch a.txt', [WIN_REPO + "\\a.txt"]),
    ("cd C:\\repo; cd src && touch a.py", ["C:\\repo\\src\\a.py"]),
    ("cp a.txt b.txt C:\\repo\\out\\", ["C:\\repo\\out\\a.txt", "C:\\repo\\out\\b.txt"]),
    ("cd C:\\ && echo x > top.txt", ["C:\\top.txt"]),
    ("echo 'C:\\x' > my\\ file.txt", ["my file.txt"]),
])
def test_shell_writes_keep_windows_paths(command, expected):
    assert [w.path for w in shell_writes(command)] == expected


def test_shell_deletes_keep_windows_paths():
    assert shell_deletes("cd C:\\Users\\dev\\repo && rm old.txt") == ["C:\\Users\\dev\\repo\\old.txt"]
    assert shell_deletes("rm C:\\Users\\dev\\repo\\tmp.log") == ["C:\\Users\\dev\\repo\\tmp.log"]


# ------------------------------------------------ spaced drive paths: the display label

def test_spaced_drive_labels_map_the_leading_fragment_only():
    """The rule is the one description of "a folder name with spaces is one path" for display:
    it maps the word a shell cuts the text at to the whole path, and nothing else."""
    assert spaced_drive_labels(r"echo hi > E:\Work\Client y\z.txt") == {
        r"E:\Work\Client": r"E:\Work\Client y\z.txt"}


def test_spaced_drive_labels_ignore_quoted_paths():
    """A quoted path is one word to the shell already, so there is no fragment to relabel."""
    assert spaced_drive_labels(r'type "E:\Work\Client X\secret.txt"') == {}
    assert spaced_drive_labels(r"type 'E:\Work\Client y\z.txt'") == {}


def test_show_spaced_drive_paths_never_relabels_a_whole_path_twice():
    labels = spaced_drive_labels(r"echo hi > E:\Work\Client y\z.txt")
    reason = r"Shell command wrote outside the working folder (E:\Work\Client y\z.txt)"
    assert show_spaced_drive_paths(reason, labels) == reason
    assert show_spaced_drive_paths(r"a fragment E:\Work\Client here", labels) == \
        r"a fragment E:\Work\Client y\z.txt here"


def test_label_spaced_drive_path_matches_a_normalised_fragment():
    labels = spaced_drive_labels(r"touch C:/repo/My Folder/x.txt")
    assert label_spaced_drive_path(r"C:\repo\My", labels) == r"C:/repo/My Folder/x.txt"
    assert label_spaced_drive_path(r"C:\repo\My Folder\x.txt", labels) == r"C:\repo\My Folder\x.txt"
    assert label_spaced_drive_path(r"C:\repo\Other", labels) == r"C:\repo\Other"


def test_windows_shell_writes_are_listed_relative_to_the_project():
    command = (f"cd {WIN_REPO} && echo hi > notes.txt && python3 - <<'EOF'\n"
               "open('tests/test_api.py','w').write(s)\nEOF")
    fc = _changes(command, cwd=WIN_REPO)
    assert sorted(fc) == ["notes.txt", "tests/test_api.py"]
    assert all(c.shell for c in fc.values())


# ---------------------------------------------------------------- UNC & \\?\ long paths (VAL-CORE-027, 028)

def test_unc_paths_are_windows_style_and_compare_correctly():
    base = r"\\srv\share\proj"
    assert is_windows_style(base) is True
    assert is_windows_style("//srv/share/proj/a.py") is True
    assert is_unc(base) is True
    assert is_unc("//srv/share/proj") is True
    assert is_unc(r"\\?\UNC\srv\share\proj") is True
    assert is_unc(r"\\.\PhysicalDrive0") is False
    assert is_unc(r"\\?\GLOBALROOT\Device\HarddiskVolume1") is False
    assert within(r"\\srv\share\proj\a.py", base) is True
    assert within("//srv/share/proj/a.py", base) is True
    assert within(r"\\SRV\Share\Proj\b.py", base) is True
    assert within(r"\\srv\share2\x.py", base) is False
    assert within(r"\\srv\share\other\x.py", base) is False
    assert relative(r"\\srv\share\proj\src\a.py", base) == "src/a.py"


def test_long_path_prefix_is_normalised():
    assert strip_long_prefix(r"\\?\C:\Windows\x.txt") == r"C:\Windows\x.txt"
    assert strip_long_prefix(r"\\?\UNC\srv\share\x") == r"\\srv\share\x"
    assert strip_long_prefix(r"\\.\PhysicalDrive0") == r"\\.\PhysicalDrive0"
    assert strip_long_prefix(r"\\?\GLOBALROOT\Device\HarddiskVolume1") == r"\\?\GLOBALROOT\Device\HarddiskVolume1"
    assert within(r"\\?\C:\proj\a.py", r"C:\proj") is True
    assert within(r"\\?\C:\Windows\x.txt", r"C:\proj") is False
    assert within(r"\\?\UNC\srv\share\x", r"\\srv\share\proj") is False
    assert within(r"\\?\UNC\srv\share\proj\a.py", r"\\srv\share\proj") is True


# ---------------------------------------------------------------- >200-char Claude project folder hash (VAL-CORE-061)

def _ref_java_hash_base36(raw_path: str) -> str:
    data = raw_path.encode("utf-16-le")
    h = 0
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        h = (h * 31 + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    n = abs(h)
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def test_encode_project_path_over_200_chars_and_prefix_fallback(tmp_path, monkeypatch):
    import re
    from runledger.parser import encode_project_path, find_sessions

    # Short path (<= 200) is unchanged
    short_p = "/srv/short/project"
    assert encode_project_path(short_p) == "-srv-short-project"

    # Long BMP path (> 200 chars)
    long_p = "D:\\rl-ready\\core\\long\\" + "\\".join(f"seg{i}_{'a' * 32}" for i in range(6))
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", long_p)
    assert len(sanitized) > 200
    expected_long = f"{sanitized[:200]}-{_ref_java_hash_base36(long_p)}"
    assert encode_project_path(long_p) == expected_long

    # Non-BMP path (surrogate pair in UTF-16 code units)
    non_bmp = "/work/rocket-\U0001f680/" + "/".join(f"dir{i}_{'b' * 32}" for i in range(6))
    sanitized_nb = re.sub(r"[^a-zA-Z0-9]", "-", non_bmp)
    assert len(sanitized_nb) > 200
    assert encode_project_path(non_bmp) == f"{sanitized_nb[:200]}-{_ref_java_hash_base36(non_bmp)}"

    # Session discovery finds exact >200 hash dir and falls back to 200-char prefix scan
    from pathlib import Path
    from runledger.parser import _fs_str

    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    exact_dir = cfg / "projects" / expected_long
    Path(_fs_str(exact_dir)).mkdir(parents=True)
    sess = exact_dir / "s.jsonl"
    Path(_fs_str(sess)).write_text(
        json.dumps({"type": "user", "sessionId": "s", "cwd": long_p, "message": {"content": "hello"}}) + "\n",
        encoding="utf-8",
    )
    assert find_sessions(long_p) == [sess]

    # Rename suffix to something different with the same 200-char prefix
    fallback_dir = cfg / "projects" / f"{sanitized[:200]}-differentsuffix"
    Path(_fs_str(exact_dir)).rename(Path(_fs_str(fallback_dir)))
    assert find_sessions(long_p) == [fallback_dir / "s.jsonl"]


