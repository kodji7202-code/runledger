"""Release clean-install checker contract."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_clean_install  # noqa: E402


def test_artifact_discovery_requires_one_wheel_and_one_sdist(tmp_path):
    wheel = tmp_path / "runledger_ai-1.0.0rc1-py3-none-any.whl"
    sdist = tmp_path / "runledger_ai-1.0.0rc1.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    assert check_clean_install._artifacts(tmp_path) == (wheel.resolve(), sdist.resolve())
    assert check_clean_install._version_from_wheel(wheel) == "1.0.0rc1"


def test_release_workflow_uses_the_clean_install_gate():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "python tools/check_clean_install.py --dist-dir dist" in release
    assert release.index("python -m build") < release.index("tools/check_clean_install.py")


def test_executable_names_follow_the_platform():
    env = Path("env")
    assert check_clean_install._exe(env, "python").name.startswith("python")
    assert check_clean_install._exe(env, "runledger").name.startswith("runledger")


def test_health_smoke_bypasses_environment_proxies():
    script = (ROOT / "tools" / "check_clean_install.py").read_text(encoding="utf-8")
    assert "urllib.request.ProxyHandler({})" in script
    assert "urlopen(" not in script
