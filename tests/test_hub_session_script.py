"""The session launcher fails fast and clearly; it never execs op-resolve or claude here."""

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hub-session.sh"
MSG = (
    "hub-session: OP_CONNECT_HOST is not set; export the 1Password Connect URL first "
    "(see README § Massive)"
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "OP_CONNECT_HOST"}
    env.update(extra)
    return env


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )


def test_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unset_connect_host_exits_2_with_one_line() -> None:
    result = _run(SCRIPT, _env())
    assert result.returncode == 2
    assert result.stderr.strip() == MSG


def test_empty_connect_host_is_rejected() -> None:
    result = _run(SCRIPT, _env(OP_CONNECT_HOST=""))
    assert result.returncode == 2
    assert MSG in result.stderr


def test_missing_env_local_exits_2_before_exec(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "hub-session.sh"
    shutil.copy(SCRIPT, copy)
    result = _run(copy, _env(OP_CONNECT_HOST="http://<connect-host>:8090", HOME=str(tmp_path)))
    assert result.returncode == 2
    assert ".env.local is missing" in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
