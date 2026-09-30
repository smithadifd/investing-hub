import json
import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".mcp.json"
ENV_REF = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
KEY_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _servers() -> dict:
    return json.loads(CONFIG.read_text())["mcpServers"]


def test_mcp_config_parses_and_declares_massive():
    assert "massive" in _servers(), "massive server not declared in .mcp.json"


def test_massive_reads_key_from_environment_reference():
    env = _servers()["massive"].get("env", {})
    assert env.get("MASSIVE_API_KEY") == "${MASSIVE_API_KEY}", "key must be an env-var reference"


def test_mcp_config_has_no_literal_secret():
    for name, server in _servers().items():
        for var, value in server.get("env", {}).items():
            assert ENV_REF.match(value), f"{name}.{var} is not a ${{VAR}} reference"
        for arg in server.get("args", []):
            assert not KEY_LIKE.fullmatch(arg), f"{name} arg looks like a key"
