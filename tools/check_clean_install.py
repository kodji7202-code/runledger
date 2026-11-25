#!/usr/bin/env python3
"""Install release artifacts into clean virtual environments and smoke-test them.

This is the last release gate before PyPI: it uses the built wheel exactly as a user receives
it, rather than the source checkout. The base environment proves the CLI stays zero-dependency;
the server environment proves the optional FastAPI extra installs and starts. The check needs the
repository checkout only for its small public test fixture.

    python tools/check_clean_install.py --dist-dir dist

Exits nonzero with the failing command/context on stderr. It creates no files outside the
temporary directory it removes before exit.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _exe(env: Path, name: str) -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return env / folder / (name + suffix)


def _run(argv, *, cwd=None, env=None, timeout=120):
    command = [str(part) for part in argv]
    result = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed (%s): %s\nstdout:\n%s\nstderr:\n%s"
            % (result.returncode, " ".join(command), result.stdout, result.stderr)
        )
    return result.stdout.strip()


def _make_env(path: Path):
    builder = venv.EnvBuilder(with_pip=True)
    builder.create(str(path))


def _artifacts(dist: Path):
    dist = dist.resolve()
    wheels = sorted(dist.glob("runledger_ai-*-py3-none-any.whl"))
    sdists = sorted(dist.glob("runledger_ai-*.tar.gz"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one pure-Python wheel in {dist}, found {len(wheels)}")
    if len(sdists) != 1:
        raise RuntimeError(f"expected exactly one source distribution in {dist}, found {len(sdists)}")
    return wheels[0], sdists[0]


def _version_from_wheel(wheel: Path) -> str:
    # runledger_ai-<version>-py3-none-any.whl; versions do not contain hyphens in PEP 440 files.
    return wheel.name.split("-")[1]


def _base_smoke(env_path: Path, wheel: Path, expected: str, work: Path):
    python = _exe(env_path, "python")
    cli = _exe(env_path, "runledger")
    _run([python, "-m", "pip", "install", "--no-cache-dir", wheel])
    version = _run([cli, "--version"])
    if version != f"runledger {expected}":
        raise RuntimeError(f"CLI reports {version!r}, expected 'runledger {expected}'")

    receipt = work / "receipt.json"
    fixture = ROOT / "tests" / "fixtures" / "sample_session.jsonl"
    _run([cli, "receipt", fixture, "--format", "json", "-o", receipt], cwd=work)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not data.get("session_id"):
        raise RuntimeError("the installed CLI wrote an invalid receipt smoke artifact")
    _run([cli, "validate", receipt], cwd=work)

    missing_fastapi = (
        "import importlib.util, sys; "
        "sys.exit(0 if importlib.util.find_spec('fastapi') is None else 1)"
    )
    try:
        _run([python, "-c", missing_fastapi])
    except RuntimeError as exc:
        raise RuntimeError("the zero-dependency CLI install unexpectedly includes fastapi") from exc


def _server_smoke(env_path: Path, wheel: Path, work: Path):
    python = _exe(env_path, "python")
    requirement = f"runledger-ai[server] @ {wheel.as_uri()}"
    _run([python, "-m", "pip", "install", "--no-cache-dir", requirement], timeout=240)
    smoke = work / "server_smoke.py"
    smoke.write_text(
        """import json
import tempfile
import threading
import urllib.request
from pathlib import Path

from runledger.server.app import make_server

db = Path(tempfile.mkdtemp()) / "smoke.db"
server = make_server(str(db), host="127.0.0.1", port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        f"http://127.0.0.1:{server.server_address[1]}/health", timeout=10
    ) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert response.status == 200 and body["ok"] is True
finally:
    server.shutdown()
    server.server_close()
    server.db.close()
    thread.join(5)
print("server health smoke passed")
""",
        encoding="utf-8",
    )
    _run([python, smoke])


def check_clean_install(dist_dir: Path, *, server: bool = True) -> str:
    wheel, sdist = _artifacts(dist_dir)
    expected = _version_from_wheel(wheel)
    with tempfile.TemporaryDirectory(prefix="runledger-install-") as temp:
        work = Path(temp)
        base = work / "base-env"
        _make_env(base)
        _base_smoke(base, wheel, expected, work)
        if server:
            server_env = work / "server-env"
            _make_env(server_env)
            _server_smoke(server_env, wheel, work)
    return f"clean install passed for {wheel.name} and {sdist.name}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist",
                        help="directory containing one built wheel and sdist (default: dist/)")
    parser.add_argument("--skip-server", action="store_true",
                        help="check only the zero-dependency CLI install")
    args = parser.parse_args(argv)
    try:
        message = check_clean_install(args.dist_dir, server=not args.skip_server)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"clean install check failed: {exc}\n")
        return 1
    sys.stdout.write(message + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
