import pytest

from runledger.parser import Run, Step, Usage
from runledger.receipt import render
from runledger.summarize import apply_templates, shorten, template_summary


def _run(steps, cwd="/p", prompts=None):
    return Run("session-text", "/x/s.jsonl", cwd, None, None, None, prompts or [], steps, "",
               Usage(), {})


def _bash(command, **extra):
    return Step(1, "Bash", {"command": command, **extra}, "t", None, None)


@pytest.mark.parametrize("text,limit,expected", [
    ("short", 10, "short"),
    ("alpha beta gamma delta", 12, "alpha beta…"),
    ("x" * 30, 10, "x" * 9 + "…"),
    ("a  b\n c", 10, "a b c"),
])
def test_shorten_cuts_at_word_boundary_with_ellipsis(text, limit, expected):
    assert shorten(text, limit) == expected
    assert len(shorten(text, limit)) <= limit


def test_long_command_is_cut_on_a_word_with_ellipsis():
    cmd = "pytest " + " ".join(f"tests/test_module_{i}.py" for i in range(20))
    summary = template_summary(_bash(cmd), "/p")
    shown = summary.split("`")[1]
    assert shown.endswith("…") and len(shown) <= 120
    assert shown[:-1].rstrip() + " " in cmd + " "  # cut falls on a whole word


def test_extra_command_lines_are_counted_not_dropped_silently():
    assert template_summary(_bash("echo a\necho b\necho c"), "/p") == "Ran `echo a (+2 lines)`"
    assert template_summary(_bash("echo a\necho b"), "/p") == "Ran `echo a (+1 line)`"


def test_backticks_in_commands_cannot_break_code_spans():
    run = _run([_bash("echo `date` && ls")])
    apply_templates(run)
    assert run.steps[0].summary == "Ran `echo 'date' && ls`"
    md = render(run, 0, "Low", [], "md")
    assert "1. Ran `echo 'date' && ls` —" in md
    page = render(run, 0, "Low", [], "html")
    assert "<code>echo &#x27;date&#x27; &amp;&amp; ls</code>" in page


def test_overview_goal_is_cut_on_a_word_with_ellipsis():
    run = _run([], prompts=["internationalization " * 15])
    apply_templates(run)
    goal = run.overall_summary.split("“")[1].split("”")[0]
    assert goal.endswith("internationalization…") and len(goal) <= 200


def test_missing_cwd_shows_a_dash_not_none():
    md = render(_run([], cwd=None), 0, "Low", [], "md")
    assert "**Folder:** -" in md and "None" not in md


def test_markdown_free_text_cannot_change_the_layout():
    hostile = "fix it\n# Heading\n<details><summary>x</summary> | pipe"
    run = _run([Step(1, "Read", {"file_path": "/p/a`b.py"}, "t", None, None)], prompts=[hostile])
    apply_templates(run)
    run.steps[0].summary = "# not a heading <b>bold</b>"
    from runledger.risk import Risk
    risks = [Risk("medium", "x", "reason\n## injected <img src=x>", 1)]
    md = render(run, 40, "Medium", risks, "md")
    lines = md.splitlines()
    assert not any(line.startswith(("# Heading", "## injected")) for line in lines)
    assert "<details>" not in md and "<img" not in md and "<b>" not in md
    assert "1. \\# not a heading &lt;b&gt;bold&lt;/b&gt;" in md
    assert "step 1: reason ## injected &lt;img src=x&gt;" in md


def test_file_paths_with_backticks_stay_in_one_code_span():
    run = _run([Step(1, "Write", {"file_path": "/p/we`ird.py", "content": "x\n"}, "t", None, None)])
    apply_templates(run)
    md = render(run, 0, "Low", [], "md")
    assert "- `we'ird.py` (new) +1 −0" in md


def test_markdown_guard_table_escapes_tags_but_not_ampersands():
    run = _run([])
    run.guard = [{"step": None, "tool": "Bash", "summary": "a && b <x>", "decision": "deny",
                  "outcome": "blocked", "reason": "r", "codes": ["c"], "ts": None}]
    md = render(run, 0, "Low", [], "md")
    assert "a && b &lt;x&gt;" in md and "&amp;" not in md
