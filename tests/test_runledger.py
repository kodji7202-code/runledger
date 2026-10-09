import json
from pathlib import Path

from runledger.cli import build, main
from runledger.parser import Step, encode_project_path
from runledger.pricing import model_key
from runledger.receipt import render
from runledger.risk import assess_step

FIX = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
CWD = "/home/dev/payments-service"


def test_parse_and_totals():
    run, score, level, risks, note = build(str(FIX))
    assert len(run.steps) == 10
    assert run.git_branch == "fix/retry-logic"
    assert run.cost and 0.05 < run.cost < 0.2
    # streaming split message counted once
    assert run.usage.output_tokens == 410 + 120 + 1650 + 60 + 300 + 520 + 55 + 90 + 140
    assert level == "High"
    codes = {r.code for r in risks}
    assert {"secret_file", "test_deleted"} <= codes


def test_model_keys():
    assert model_key("claude-sonnet-4-5-20250929") == ("sonnet", "4.5")
    assert model_key("claude-opus-4-20250514") == ("opus", "4")
    assert model_key("claude-3-5-haiku-20241022") == ("haiku", "3.5")
    assert model_key("claude-haiku-5-5") == ("haiku", "5.5")


def _step(tool, **inp):
    return Step(index=1, tool=tool, input=inp, tool_use_id="x", model=None, timestamp=None)


def test_rules():
    assert not assess_step(_step("Read", file_path=CWD + "/src/a.ts"), CWD)
    assert assess_step(_step("Write", file_path="/etc/hosts", content="x"), CWD)[0].code == "write_outside"
    assert assess_step(_step("Read", file_path=CWD + "/.env.example"), CWD) == []
    assert any(r.severity == "high" for r in assess_step(_step("Bash", command="rm -rf build"), CWD))
    assert any(r.code == "test_skipped" for r in assess_step(
        _step("Edit", file_path=CWD + "/tests/a.test.ts", old_string="it('x', () => {})", new_string="it.skip('x', () => {})"), CWD))


def test_formats(tmp_path):
    run, score, level, risks, _ = build(str(FIX))
    data = json.loads(render(run, score, level, risks, "json"))
    assert data["totals"]["files_changed"] == 3
    assert "<html" in render(run, score, level, risks, "html")
    out = tmp_path / "r.html"
    assert main(["receipt", str(FIX), "-o", str(out)]) == 0 and out.exists()
    assert main(["receipt", str(FIX), "-o", str(out), "--fail-on", "50"]) == 2


def test_stdout_receipt_still_honours_fail_on(capsys):
    assert main(["receipt", str(FIX), "--format", "json", "-o", "-", "--fail-on", "50"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["risk"]["score"] >= 50


def test_encode_project_path():
    assert encode_project_path("/home/dev/my.app") == "-home-dev-my-app"
