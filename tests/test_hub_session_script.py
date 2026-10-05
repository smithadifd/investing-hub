"""The session launcher picks one of three credential paths and execs it with argv intact.

Every executable it can reach (claude, op, op-resolve.py) is a stub on a temp PATH/HOME that
prints its argv; nothing real is called and no reference is resolved.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hub-session.sh"
BOTH = {"MASSIVE_API_KEY": "placeholder-a", "IC_API_TOKEN": "placeholder-b"}
CONNECT = {"OP_CONNECT_HOST": "http://<connect-host>:8090"}
NO_PATH_MSG = (
    "hub-session: no credential path found; export MASSIVE_API_KEY and IC_API_TOKEN (env), "
    "set OP_CONNECT_HOST (connect), or install the 1Password CLI op (op); see README § Massive"
)
MODE_VARS = ("OP_CONNECT_HOST", "MASSIVE_API_KEY", "IC_API_TOKEN", "HUB_SESSION_MODE")


class Sandbox:
    """A temp repo copy, HOME and stub bin dir; `run` launches the copied script."""

    def __init__(self, root: Path, *, with_op: bool, with_env_local: bool) -> None:
        self.root = root
        (root / "scripts").mkdir()
        self.script = root / "scripts" / "hub-session.sh"
        shutil.copy(SCRIPT, self.script)
        if with_env_local:
            (root / ".env.local").write_text("PLACEHOLDER=op://<vault>/<item>/<field>\n")
        self.home = root / "home"
        self.bin = root / "bin"
        (self.home / ".claude" / "scripts").mkdir(parents=True)
        self.bin.mkdir()
        self._stub(self.bin / "claude", "claude")
        self._stub(self.home / ".claude" / "scripts" / "op-resolve.py", "op-resolve")
        if with_op:
            self._stub(self.bin / "op", "op")

    @staticmethod
    def _stub(path: Path, name: str) -> None:
        path.write_text(
            f"#!/bin/sh\nprintf '{name}'\nfor a in \"$@\"; do printf ' [%s]' \"$a\"; done\necho\n"
        )
        path.chmod(0o755)

    def run(
        self, *args: str, cwd: Path | None = None, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in MODE_VARS}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:/usr/bin:/bin")
        env.update(extra)
        return subprocess.run(
            ["bash", str(self.script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd or self.root,
        )


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path, with_op=True, with_env_local=True)


def test_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_env_mode_execs_claude_directly_with_passthrough(box: Sandbox) -> None:
    result = box.run("-p", "hello world", **BOTH)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "claude [-p] [hello world]\n"
    assert result.stderr == "hub-session: using env\n"


def test_env_wins_over_connect_and_op(box: Sandbox) -> None:
    result = box.run(**BOTH, **CONNECT)
    assert result.stdout == "claude\n"
    assert result.stderr == "hub-session: using env\n"


def test_one_variable_alone_is_not_env(box: Sandbox) -> None:
    result = box.run(MASSIVE_API_KEY="placeholder-a", **CONNECT)
    assert result.stderr == "hub-session: using connect\n"
    result = box.run(MASSIVE_API_KEY="placeholder-a", IC_API_TOKEN="")
    assert result.stderr == "hub-session: using op\n"


def test_connect_mode_execs_resolver_with_env_file(box: Sandbox) -> None:
    result = box.run("-p", "hi", **CONNECT)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "op-resolve [--env-file] [.env.local] [--] [claude] [-p] [hi]\n"
    assert result.stderr == "hub-session: using connect\n"


def test_connect_wins_over_op(box: Sandbox) -> None:
    result = box.run(**CONNECT)
    assert result.stdout.startswith("op-resolve ")
    assert result.stderr == "hub-session: using connect\n"


def test_op_mode_execs_op_run_with_env_file(box: Sandbox) -> None:
    result = box.run("-p", "hi")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "op [run] [--env-file] [.env.local] [--] [claude] [-p] [hi]\n"
    assert result.stderr == "hub-session: using op\n"


def test_no_path_exits_2_with_one_line(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, with_op=False, with_env_local=True)
    result = box.run()
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == NO_PATH_MSG + "\n"


def test_override_beats_detection(box: Sandbox) -> None:
    result = box.run(HUB_SESSION_MODE="op", **BOTH, **CONNECT)
    assert result.stdout.startswith("op [run]")
    assert result.stderr == "hub-session: using op\n"
    result = box.run(HUB_SESSION_MODE="connect", **BOTH)
    assert result.stdout.startswith("op-resolve ")
    result = box.run(HUB_SESSION_MODE="env", **BOTH, **CONNECT)
    assert result.stdout == "claude\n"


def test_forced_op_without_op_on_path_exits_2_before_exec(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, with_op=False, with_env_local=True)
    result = box.run(HUB_SESSION_MODE="op")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "hub-session: HUB_SESSION_MODE=op but the 1Password CLI op is not on PATH\n"
    )


def test_forced_connect_without_resolver_exits_2_before_exec(box: Sandbox) -> None:
    (box.home / ".claude" / "scripts" / "op-resolve.py").unlink()
    result = box.run(HUB_SESSION_MODE="connect", **CONNECT)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"hub-session: HUB_SESSION_MODE=connect but {box.home}/.claude/scripts/op-resolve.py "
        "is missing\n"
    )


def test_forced_env_with_one_variable_exits_2_before_exec(box: Sandbox) -> None:
    for present in BOTH:
        result = box.run(HUB_SESSION_MODE="env", **{present: BOTH[present]})
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == (
            "hub-session: HUB_SESSION_MODE=env needs MASSIVE_API_KEY and IC_API_TOKEN "
            "both non-empty\n"
        )


def test_unknown_mode_exits_2_with_one_line(box: Sandbox) -> None:
    result = box.run(HUB_SESSION_MODE="bogus", **BOTH)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "hub-session: HUB_SESSION_MODE must be connect, op or env (see README § Massive)\n"
    )


@pytest.mark.parametrize(
    ("extra", "mode"), [(CONNECT, "connect"), ({}, "op")], ids=["connect", "op"]
)
def test_missing_env_local_exits_2_before_exec(
    tmp_path: Path, extra: dict[str, str], mode: str
) -> None:
    box = Sandbox(tmp_path, with_op=True, with_env_local=False)
    result = box.run(**extra)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"hub-session: .env.local is missing (looked in {tmp_path.resolve()}); copy "
        ".env.example to .env.local there, run from the instance directory, or set "
        "HUB_INSTANCE_DIR (see README § Massive)\n"
    )


def test_env_mode_skips_env_local_check(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, with_op=False, with_env_local=False)
    result = box.run(**BOTH)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "claude\n"
    assert result.stderr == "hub-session: using env\n"


def test_root_is_the_working_directory_not_the_script_location(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    box = Sandbox(tmp_path / "code", with_op=True, with_env_local=True)
    elsewhere = tmp_path / "instance"
    elsewhere.mkdir()
    # .env.local sits beside the script, but the working directory has none.
    result = box.run(cwd=elsewhere, **CONNECT)
    assert result.returncode == 2
    assert result.stdout == ""
    assert ".env.local is missing" in result.stderr
    assert str(elsewhere.resolve()) in result.stderr


def test_instance_dir_override_supplies_env_local(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    box = Sandbox(tmp_path / "code", with_op=True, with_env_local=False)
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / ".env.local").write_text("")
    result = box.run(cwd=tmp_path, HUB_INSTANCE_DIR=str(instance), **CONNECT)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "hub-session: using connect\n"
    assert result.stdout == "op-resolve [--env-file] [.env.local] [--] [claude]\n"


def test_instance_dir_override_must_exist(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, with_op=True, with_env_local=True)
    result = box.run(HUB_INSTANCE_DIR=str(tmp_path / "absent"), **CONNECT)
    assert result.returncode == 2
    assert result.stderr.startswith("hub-session: cannot enter HUB_INSTANCE_DIR=")
