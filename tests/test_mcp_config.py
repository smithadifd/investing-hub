import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".mcp.json.example"
ENV_REF = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
KEY_LIKE = re.compile(
    r"[A-Za-z0-9_\-]{24,}|sk_[A-Za-z0-9_]+|Bearer\s+\S+|--api-key|api[_-]?key=",
    re.IGNORECASE,
)


def _config() -> dict:
    return json.loads(CONFIG.read_text())


def _servers() -> dict:
    return _config()["mcpServers"]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_example_parses_and_declares_massive():
    assert "massive" in _servers(), "massive server not declared in .mcp.json.example"


def test_massive_command_is_mcp_massive():
    assert _servers()["massive"]["command"] == "mcp_massive"


def test_env_values_are_exact_references():
    env = _servers()["massive"].get("env", {})
    assert env.get("MASSIVE_API_KEY") == "${MASSIVE_API_KEY}", "key must be an env-var reference"
    for server_name, server in _servers().items():
        for var, value in server.get("env", {}).items():
            assert ENV_REF.match(value), f"{server_name}.{var} is not a ${{VAR}} reference"


def test_no_key_shaped_string_anywhere():
    for text in _strings(_config()):
        if ENV_REF.match(text):
            continue
        assert not KEY_LIKE.search(text), f"key-shaped string in config: {text[:6]}..."


def test_local_config_is_gitignored():
    lines = [ln.strip() for ln in (ROOT / ".gitignore").read_text().splitlines()]
    assert ".mcp.json" in lines, ".mcp.json must be gitignored"
