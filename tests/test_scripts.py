"""scripts/ensure_model.sh — the keep-alive that must never restart a live server.

`lms server start` is not idempotent on LM Studio 0.4.x: run against a server that is already
listening it restarts that server, and every answer being generated at that moment dies. The
script therefore probes the port first. These tests stub `lms` and `curl` and check that order.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ensure_model.sh"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_tools(tmp_path):
    """A PATH whose `lms` and `curl` log what they were asked and behave as the test says.

    HOME is redirected so the script's own `$HOME/.lmstudio/bin` prefix finds no real lms.
    `curl` (the port probe) succeeds when FAKE_PORT_UP=1, or once `lms server start` was called.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_dir, "lms",
          f'echo "lms $*" >> "{log}"\n'
          'if [ "$1 $2" = "ps --json" ]; then echo "[{\\"modelKey\\":\\"$FAKE_MODEL\\"}]"; fi\n'
          'exit 0\n')
    _stub(bin_dir, "curl",
          f'echo "curl $*" >> "{log}"\n'
          f'[ "$FAKE_PORT_UP" = "1" ] || grep -q "lms server start" "{log}"\n')
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path),
               LLM_MODEL="fake/model", FAKE_MODEL="fake/model", OPENAI_BASE_URL="http://localhost:4321/v1")
    return env, log


def _run(env):
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_a_listening_server_is_never_started_again(fake_tools):
    env, log = fake_tools
    env["FAKE_PORT_UP"] = "1"
    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text(encoding="utf-8")
    assert "lms server start" not in calls, calls
    assert ":4321/v1/models" in calls                       # probed the port from OPENAI_BASE_URL
    assert r.stdout == ""                                   # nothing to do, nothing to log


def test_a_down_server_is_probed_first_then_started(fake_tools):
    env, log = fake_tools
    env["FAKE_PORT_UP"] = "0"
    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("curl"), calls              # probe before anything else
    assert "lms server start" in calls
    assert "server listening on port 4321" in r.stdout
