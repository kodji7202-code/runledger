"""The per-tool-call entrypoints must not load receipt and AI features."""
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("entrypoint", ["module", "console"])
@pytest.mark.parametrize("payload,decision", [
    ({"tool_name": "Bash", "tool_input": {"command": "git status --short"}}, None),
    ({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "deny"),
    (None, None),
    ("not json", None),
])
def test_guard_imports(tmp_path, entrypoint, payload, decision):
    if isinstance(payload, dict):
        payload.update(session_id="import-test", cwd=str(tmp_path),
                       hook_event_name="PreToolUse")
        raw = json.dumps(payload)
    else:
        raw = payload or ""
    script = """
import json
import runpy
import sys

entrypoint = sys.argv.pop()
sys.argv = ['runledger', 'guard', '--agent', 'claude-code']
if entrypoint == 'module':
    try:
        runpy.run_module('runledger', run_name='__main__')
    except SystemExit as exc:
        assert exc.code == 0
else:
    from runledger.cli import main
    assert main() == 0
print(json.dumps(sorted(sys.modules)), file=sys.stderr)
"""
    result = subprocess.run([sys.executable, "-c", script, entrypoint], input=raw,
                            text=True, capture_output=True, check=True)
    modules = json.loads(result.stderr)
    for name in ("guard_receipt", "pricing", "quality", "receipt", "safe", "review",
                 "summarize", "llm", "server.app"):
        assert "runledger." + name not in modules
    if decision is None:
        assert result.stdout == ""
    else:
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == decision
