"""Render a synthetic Droid ask decision after an empty successful tool result."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as folder:
        project = Path(folder)
        session = project / "empty-result.jsonl"
        rows = [
            {"type": "session_start", "id": "empty-result", "cwd": folder, "version": 1},
            {"type": "message", "id": "call", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "execute", "name": "Execute",
                 "input": {"command": "true"}}]}},
            {"type": "message", "id": "result", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "execute", "content": "", "is_error": False}]}},
        ]
        session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        log = project / ".runledger" / "guard.log"
        log.parent.mkdir()
        log.write_text(json.dumps({"session_id": "empty-result", "tool": "Execute",
                                   "agent": "droid", "canonical_tool": "Bash",
                                   "summary": "true", "decision": "ask"}) + "\n",
                       encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "runledger", "receipt", str(session), "--format", "json", "-o", "-"],
            check=True, capture_output=True, text=True)
        receipt = json.loads(result.stdout)
        assert receipt["guard"][0]["step"] == 1
        print("Agent:", receipt["agent"])
        print("Guard outcome:", receipt["guard"][0]["outcome"])


if __name__ == "__main__":
    main()
