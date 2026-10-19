"""The dangerous-command rules added in v0.7.0 (validation contract, RULES area).

Every case runs through `assess_step`, so the command is read the way the guard reads it:
quoted text and heredoc bodies are blanked first, and one rule may not reach across a
command boundary (;, &, |, newline).
"""
import pytest

from runledger.parser import Step
from runledger.risk import assess_step

WIN_CWD = "D:\\runledger"
NO_POLICY = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}


def _step(tool, index=1, **inp):
    return Step(index=index, tool=tool, input=inp, tool_use_id=f"t{index}", model=None, timestamp=None)


def _command_risks(command, tool="Bash"):
    return [r for r in assess_step(_step(tool, command=command), WIN_CWD, NO_POLICY)
            if r.code == "command"]


def _hit(command, severity, needle, tool="Bash"):
    """The command produced a `command` risk at `severity` whose reason contains `needle`."""
    found = [(r.severity, r.reason) for r in _command_risks(command, tool)]
    assert any(sev == severity and needle in why for sev, why in found), found


def _clean(command, tool="Bash"):
    assert _command_risks(command, tool) == []


# ---------------------------------------------------------------- git branch -D (VAL-RULES-001)

@pytest.mark.parametrize("command", [
    "git branch -D feature/x", "git branch -fD x", "git branch -Df x",
    "git branch --delete --force x", "git branch --force --delete x", "git branch -D a b",
    "ls && git branch -D x",
])
def test_force_deleting_a_branch(command):
    _hit(command, "medium", "Force-deleted a git branch")


@pytest.mark.parametrize("command", [
    "git branch -d x", "git branch --delete x", "git branch -a", "git branch -vv",
    "git branch --list", "git branch newbranch", "git checkout -b D", 'echo "git branch -D x"',
])
def test_safe_branch_commands(command):
    _clean(command)


# ---------------------------------------------------------------- git push --delete (VAL-RULES-002)

@pytest.mark.parametrize("command", [
    "git push origin --delete feature", "git push --delete origin feature",
    "git push origin -d feature", "git push origin :feature", "git push origin --delete v1.0",
])
def test_deleting_a_remote_branch_or_tag(command):
    _hit(command, "high", "Deleted a remote git branch or tag")
    _hit(command, "medium", "Pushed to a git remote")


@pytest.mark.parametrize("command", [
    "git push origin main", "git push -u origin feature", "git push origin HEAD:refs/heads/main",
    "git push origin feature:feature",
])
def test_plain_pushes_are_not_delete(command):
    assert [r.severity for r in _command_risks(command)] == ["medium"]
    _hit(command, "medium", "Pushed to a git remote")


# ---------------------------------------------------------------- discarding work (VAL-RULES-003)

@pytest.mark.parametrize("command", [
    "git checkout .", "git checkout -- .", "git restore .", "git restore --staged --worktree .",
    "git restore -W .", "cd x && git checkout .",
])
def test_discarding_all_working_tree_changes(command):
    _hit(command, "medium", "Discarded all working-tree changes")


@pytest.mark.parametrize("command", [
    "git checkout main", "git checkout -b feature", "git checkout -- src/app.py",
    "git checkout .gitignore", "git restore src/app.py", "git restore .gitignore",
    "git restore --staged .",
])
def test_targeted_checkout_and_restore(command):
    _clean(command)


# ---------------------------------------------------------------- git stash (VAL-RULES-004)

@pytest.mark.parametrize("command", ["git stash drop", "git stash drop stash@{1}", "git stash clear"])
def test_dropping_stash_entries(command):
    _hit(command, "medium", "Dropped git stash entries")


@pytest.mark.parametrize("command", [
    "git stash", "git stash list", "git stash pop", "git stash apply", "git stash push -m x",
    "git stash show",
])
def test_keeping_stash_entries(command):
    _clean(command)


# ---------------------------------------------------------------- find (VAL-RULES-005)

@pytest.mark.parametrize("command", [
    'find . -name "*.tmp" -delete', "find /tmp/x -type f -delete", "find . -exec rm {} \\;",
    "find . -exec unlink {} +", "find . -name x -exec shred {} \\;",
])
def test_find_that_deletes(command):
    _hit(command, "medium", "Deleted files with find")


@pytest.mark.parametrize("command", [
    'find . -name "*.py"', "find . -type f -print", "find . -exec grep foo {} \\;",
    "find . -name x; echo -delete", "echo find -delete",
])
def test_find_without_deleting(command):
    _clean(command)


# ---------------------------------------------------------------- dd (VAL-RULES-006)

@pytest.mark.parametrize("command", [
    "dd if=image.iso of=/dev/sda bs=4M", "dd if=/dev/zero of=/dev/nvme0n1",
])
def test_dd_to_a_device(command):
    _hit(command, "high", "Wrote raw data to a device")


@pytest.mark.parametrize("command", [
    "dd if=a of=out.img", "dd if=/dev/urandom of=junk.bin bs=1M count=1",
])
def test_dd_to_a_file(command):
    _hit(command, "medium", "Overwrote a file with dd")


@pytest.mark.parametrize("command", [
    "dd if=/dev/zero of=/dev/null", "dd if=x of=/dev/stdout", "dd if=x of=/dev/stderr",
    "dd if=disk.img bs=1M", "dd --help",
])
def test_dd_without_a_real_target(command):
    _clean(command)


# ---------------------------------------------------------------- docker prune (VAL-RULES-007)

@pytest.mark.parametrize("command", [
    "docker system prune", "docker system prune -af", "docker builder prune",
    "docker image prune -a", "docker container prune", "docker network prune",
])
def test_docker_prune(command):
    _hit(command, "medium", "Pruned Docker resources")


@pytest.mark.parametrize("command", [
    "docker volume prune", "docker volume prune -f", "docker system prune --volumes",
    "docker system prune -a --volumes",
])
def test_docker_volume_prune(command):
    _hit(command, "high", "Pruned Docker volumes (data loss)")
    assert [r.reason for r in _command_risks(command)] == ["Pruned Docker volumes (data loss)"]


@pytest.mark.parametrize("command", [
    "docker ps", "docker images", "docker system df", "docker build .",
    "docker run hello-world", "docker volume ls",
])
def test_docker_read_only(command):
    _clean(command)


# ---------------------------------------------------------------- aws s3 (VAL-RULES-008)

@pytest.mark.parametrize("command", [
    "aws s3 rm s3://b --recursive", "aws s3 rm s3://b/prefix/ --recursive --dryrun",
    "aws s3 rb s3://b --force",
])
def test_aws_s3_bulk_delete(command):
    _hit(command, "high", "Deleted S3 objects/buckets")


def test_aws_s3_single_object_delete():
    _hit("aws s3 rm s3://b/key.txt", "medium", "Deleted an S3 object")
    assert [r.severity for r in _command_risks("aws s3 rm s3://b/key.txt")] == ["medium"]


@pytest.mark.parametrize("command", [
    "aws s3 ls", "aws s3 cp a.txt s3://b/", "aws s3 sync . s3://b", "aws s3 rb s3://b",
])
def test_aws_s3_read_only(command):
    _clean(command)


# ---------------------------------------------------------------- gh (VAL-RULES-009)

@pytest.mark.parametrize("command", ["gh repo delete org/x --yes", "gh repo delete"])
def test_gh_repo_delete(command):
    _hit(command, "high", "Deleted a GitHub repository")


@pytest.mark.parametrize("command", [
    "gh release delete v1", "gh issue delete 3", "gh gist delete abc", "gh label delete bug",
    "gh secret delete TOKEN", "gh variable delete VAR", "gh cache delete 1", "gh run delete 1",
    "gh workflow delete ci.yml",
])
def test_gh_resource_delete(command):
    _hit(command, "medium", "Deleted GitHub resources")


@pytest.mark.parametrize("command", [
    "gh repo view", "gh repo clone org/x", "gh pr list", "gh release create v1",
    "gh issue close 3", "gh run list", "gh workflow run ci.yml",
])
def test_gh_read_only(command):
    _clean(command)


# ---------------------------------------------------------------- helm (VAL-RULES-010)

@pytest.mark.parametrize("command", ["helm uninstall web", "helm delete web", "helm uninstall web -n prod"])
def test_helm_uninstall(command):
    _hit(command, "high", "Uninstalled a Helm release")


@pytest.mark.parametrize("command", [
    "helm install web ./chart", "helm list", "helm upgrade web ./chart", "helm template x",
    "helm repo remove x",
])
def test_helm_read_only(command):
    _clean(command)


# ---------------------------------------------------------------- SQL in the shell (VAL-RULES-011)

@pytest.mark.parametrize("command", [
    'psql -c "DELETE FROM users"', 'mysql -e "delete from logs;"', 'sqlite3 app.db "DELETE FROM t"',
])
def test_sql_delete_without_where(command):
    _hit(command, "high", "SQL DELETE without WHERE")


@pytest.mark.parametrize("command", [
    'psql -c "DELETE FROM users WHERE id = 1"', 'psql -c "SELECT * FROM users"',
    'echo "DELETE FROM users"', 'git commit -m "delete from users"',
    "cat <<'EOF'\nDELETE FROM users\nEOF\n",
])
def test_sql_statements_that_are_safe_or_text(command):
    _clean(command)


def test_existing_destructive_sql_rule_still_fires():
    _hit('psql -c "DROP TABLE users"', "high", "Destructive SQL (DROP/TRUNCATE)")


# ---------------------------------------------------------------- rsync (VAL-RULES-012)

@pytest.mark.parametrize("command", [
    "rsync -av --delete src/ dst/", "rsync -av --delete-after src/ dst/",
    "rsync -a --remove-source-files a/ b/", "rsync --del -a a/ b/",
])
def test_rsync_that_deletes(command):
    _hit(command, "medium", "rsync that deletes files")


@pytest.mark.parametrize("command", [
    "rsync -av src/ dst/", "rsync -avn --delete src/ dst/", "rsync -a --dry-run --delete src/ dst/",
    "rsync --list-only host:/x",
])
def test_rsync_that_copies(command):
    _clean(command)


# ---------------------------------------------------------------- reg delete (VAL-RULES-013)

@pytest.mark.parametrize("command", [
    "reg delete HKCU\\Software\\Foo /f", "reg.exe delete HKEY_CURRENT_USER\\Software\\Foo",
])
def test_reg_delete_for_one_user(command):
    _hit(command, "medium", "Deleted a registry key")


@pytest.mark.parametrize("command", [
    "reg delete HKLM\\Software\\Foo /f", "reg.exe delete HKEY_LOCAL_MACHINE\\Software\\Foo /f",
])
def test_reg_delete_machine_wide(command):
    _hit(command, "high", "Deleted a machine-wide registry key")
    assert [r.severity for r in _command_risks(command)] == ["high"]


@pytest.mark.parametrize("command", [
    "reg query HKLM\\Software", "reg add HKCU\\Software\\Foo /v x",
    "reg export HKCU\\Software\\Foo f.reg",
])
def test_reg_read_or_write(command):
    _clean(command)


# ---------------------------------------------------------------- rd /s /q (VAL-RULES-014)

@pytest.mark.parametrize("command", [
    "rd /s /q build", "rmdir /s /q build", "rmdir /q /s build", "cmd /c rd /s /q build",
])
def test_recursive_quiet_delete(command):
    _hit(command, "high", "Recursive quiet delete (rd /s /q)")


@pytest.mark.parametrize("command", ["rd emptydir", "rmdir /s build", "rmdir /q x", "rd /s"])
def test_other_remove_directory_commands(command):
    _clean(command)


# ---------------------------------------------------------------- PowerShell (VAL-RULES-015)

@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse build", "Remove-Item -Recurse -Path build", "ri -rec build",
    "del -Recurse build", "rmdir -Recurse build",
])
def test_recursive_delete_without_force(command):
    _hit(command, "medium", "Recursive delete (Remove-Item -Recurse)", tool="PowerShell")
    assert [r.reason for r in _command_risks(command, "PowerShell")] == [
        "Recursive delete (Remove-Item -Recurse)"]


@pytest.mark.parametrize("command", [
    "Remove-Item -Recurse -Force build", "Remove-Item build -Force -Recurse",
])
def test_recursive_force_delete_stays_high(command):
    hits = _command_risks(command, "PowerShell")
    assert [(r.severity, r.reason) for r in hits] == [
        ("high", "Recursive force delete (Remove-Item -Recurse -Force)")]


@pytest.mark.parametrize("command", [
    "Remove-Item build\\file.txt", "Get-ChildItem -Recurse", "Copy-Item a b -Recurse",
    'Write-Output "Remove-Item -Recurse x"',
])
def test_non_recursive_powershell_delete(command):
    _clean(command, "PowerShell")


# ---------------------------------------------------------------- boundaries and text (VAL-RULES-016)

@pytest.mark.parametrize("command", [
    "git branch --list && ls -D", "git status; docker ps -D", "find . -name x | wc -D",
    "aws s3 ls & echo --recursive",
])
def test_rules_do_not_cross_a_command_boundary(command):
    _clean(command)


@pytest.mark.parametrize("command,tool", [
    ('echo "git branch -D x"', "Bash"),
    ('echo "git checkout ."', "Bash"),
    ('echo "find . -delete"', "Bash"),
    ('echo "docker volume prune"', "Bash"),
    ("git commit -m 'rd /s /q build'", "Bash"),
    ('git commit -m "DELETE FROM users"', "Bash"),
    ("Write-Output 'rsync --delete a/ b/'", "PowerShell"),
    ("Write-Output 'helm uninstall web'", "PowerShell"),
    ("ls # git branch -D x", "Bash"),
    ("git commit -m \"find . -delete\"", "Bash"),
])
def test_quoted_text_and_comments_do_not_trigger_rules(command, tool):
    _clean(command, tool)


@pytest.mark.parametrize("command,severity,needle", [
    ("ls && docker volume prune -f", "high", "Pruned Docker volumes (data loss)"),
    ("true; git push origin --delete x", "high", "Deleted a remote git branch or tag"),
])
def test_chained_real_commands_are_detected(command, severity, needle):
    _hit(command, severity, needle)


# ---------------------------------------------------------------- GE-003: a separator ends an option

@pytest.mark.parametrize("command", [
    "git push origin feature --delete; echo done",
    "git push origin feature --delete&&echo done",
    "git push origin feature --delete|cat",
    "git push origin feature --delete\necho done",
    "git push origin feature --delete && echo done",
    "git push origin feature -d; echo done",
    "git push origin feature -d&&echo done",
])
def test_remote_delete_right_before_a_separator(command):
    _hit(command, "high", "Deleted a remote git branch or tag")


@pytest.mark.parametrize("command,needle", [
    ("git branch -D; echo done", "Force-deleted a git branch (git branch -D)"),
    ("git branch -D&&echo done", "Force-deleted a git branch (git branch -D)"),
    ("git branch --delete --force; echo done", "Force-deleted a git branch"),
    ("git branch --force --delete; echo done", "Force-deleted a git branch"),
])
def test_branch_delete_right_before_a_separator(command, needle):
    _hit(command, "medium", needle)


@pytest.mark.parametrize("command", [
    "git push origin feature; echo done",
    "git branch -d feature; echo done",
    "git push origin feature:feature; echo done",
])
def test_a_plain_command_before_a_separator_is_not_a_delete(command):
    reasons = [r.reason for r in _command_risks(command)]
    assert not any("Deleted a remote git branch or tag" in why or "Force-deleted a git branch" in why
                   for why in reasons), reasons


@pytest.mark.parametrize("command,needle", [
    ("rm ./x -rf; echo done", "Recursive force delete (rm -rf)"),
    ("rm ./x -r -f; echo done", "Recursive force delete (rm -rf)"),
    ("git push origin main --force; echo done", "Force-pushed to a git remote"),
    ("git push origin main -f; echo done", "Force-pushed to a git remote"),
])
def test_a_pre_existing_flag_rule_also_ends_at_a_separator(command, needle):
    _hit(command, "high", needle)


def test_a_plain_rm_or_push_before_a_separator_is_not_forced():
    reasons = [r.reason for r in _command_risks("rm ./x; echo done")]
    assert not any("Recursive force delete" in why for why in reasons), reasons
    reasons = [r.reason for r in _command_risks("git push origin main; echo done")]
    assert not any("Force-pushed" in why for why in reasons), reasons


# ---------------------------------------------------------------- GE-004: quoted patterns are data

@pytest.mark.parametrize("command", [
    "find . -name 'helm uninstall web' -print",
    'find . -name "helm uninstall web" -print',
    "find . -name 'gh repo delete' -print",
    "find . -name x -o -name 'helm uninstall web' -print",
    "find . -iname 'git push --delete origin main'",
    "find . -name 'DELETE FROM users' -print",
    "find . -path './docker volume prune' -print",
])
def test_a_quoted_find_pattern_is_not_a_command(command):
    _clean(command)


def test_a_real_command_in_a_find_expression_still_matches():
    _hit("helm uninstall web", "high", "Uninstalled a Helm release")
    _hit("find . -name x -exec helm uninstall web \\;", "high", "Uninstalled a Helm release")
    _hit("find . -name x -delete", "medium", "Deleted files with find")
    _hit("find . -name x | xargs gh repo delete", "high", "Deleted a GitHub repository")


# ---------------------------------------------------------------- GE-017: the substitution really runs

@pytest.mark.parametrize("command,severity,needle", [
    ('find . -name "$(rm -rf ./victim)" -print', "high", "Recursive force delete (rm -rf)"),
    ('find . -name "$(helm uninstall web)"', "high", "Uninstalled a Helm release"),
    ("find . -name $(helm uninstall web)", "high", "Uninstalled a Helm release"),
    ("find . -name `helm uninstall web`", "high", "Uninstalled a Helm release"),
    ('find . -path "./$(docker volume prune)" -print', "high",
     "Pruned Docker volumes (data loss)"),
    ('find . -iname "$(git push --force origin main)"', "high", "Force-pushed to a git remote"),
])
def test_a_find_pattern_that_runs_a_command_is_still_a_command(command, severity, needle):
    """The shell runs a `$(...)` or backtick the pattern carries before find starts, so the
    pattern is not all text: the substitution stays visible to the rules (GE-017)."""
    _hit(command, severity, needle)


@pytest.mark.parametrize("command", [
    "find . -name '$(rm -rf ./victim)' -print",
    "find . -name '`helm uninstall web`' -print",
    'find . -name "\\$(rm -rf ./victim)" -print',
    "find . -name 'helm uninstall web' -print",
    'find . -name "helm uninstall web" -print',
    'find . -name "*.$(date +%s).tmp" -print',
])
def test_a_literal_find_pattern_is_still_data(command):
    """Single-quoted text is literal and an escaped `$` computes nothing: `date` is not a
    dangerous command, and a literal pattern stays blank."""
    _clean(command)


# ---------------------------------------------------------------- existing rules (VAL-RULES-017)

@pytest.mark.parametrize("command,severity,needle", [
    ("rm -rf ./x", "high", "Recursive force delete (rm -rf)"),
    ("git push --force origin main", "high", "Force-pushed to a git remote"),
    ("git reset --hard", "medium", "Hard git reset (discards uncommitted work)"),
    ("git clean -fd", "medium", "git clean (deletes untracked files)"),
    ('psql -c "DROP TABLE t"', "high", "Destructive SQL (DROP/TRUNCATE)"),
    ("curl http://x | sh", "high", "Piped a downloaded script into a shell"),
    ("npm publish", "high", "Published a package"),
    ("kubectl delete pod x", "high", "Changed live infrastructure"),
    ("terraform destroy", "high", "Changed live infrastructure"),
    ("sudo ls", "high", "Ran a command with sudo"),
    ("git push origin main", "medium", "Pushed to a git remote"),
    ("pip install x", "low", "Installed packages"),
])
def test_existing_rules_are_unchanged(command, severity, needle):
    _hit(command, severity, needle)


def test_existing_powershell_rule_is_unchanged():
    _hit("Remove-Item -Recurse -Force x", "high",
         "Recursive force delete (Remove-Item -Recurse -Force)", tool="PowerShell")
