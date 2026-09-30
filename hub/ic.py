"""Read-only client for the Investing Companion (IC) export API.

The token comes from the ``IC_API_TOKEN`` environment variable at call time. It is never
logged, never written to disk and never placed in an exception message: every error that
leaves this module passes through :func:`redact` first and carries no chained exception.
Requests go straight to IC: environment and system proxies are ignored and redirects are
refused, so the bearer token is only ever sent to the configured base URL.
"""

from __future__ import annotations

import functools
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

TOKEN_ENV = "IC_API_TOKEN"
BASE_URL_ENV = "HUB_IC_BASE_URL"
CONFIG_FILE = Path("config.yaml")
PACK_DIR = Path("data") / "ic"
PACK_FILE = "pack-latest.json"
META_FILE = "pack-meta.json"
CONTEXT_PACK_PATH = "/api/v1/export/context-pack"
CONTRACT_DOCS_PATH = "/api/v1/export/contract-docs"
TIMEOUT_SECONDS = 15.0
REDACTED = "ict_…<redacted>"
# Belt and braces: anything shaped like an IC token is redacted even if it is not ours.
# The alphabet matches what read_token accepts: printable ASCII without whitespace.
_TOKEN_SHAPE = re.compile(r"ict_[!-~]+")
# scheme://host[:port][/path]; the path is printable ASCII, so no whitespace or control chars.
_BASE_URL = re.compile(
    r"https?://(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:/[!-~]*)?"
)
_DOC_KEYS = ("handoff_schema", "advisor_actions")
_META_KEYS = ("generated_at", "schema_version", "advisor_actions_version")


class IcError(Exception):
    """A failure talking to IC. The message is one line and already redacted."""


def _clean_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Re-raise any ``IcError`` redacted and with no ``__context__`` or ``__cause__``."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except IcError as exc:
            message = redact(str(exc))
        # Raised outside the handler, so the original exception is not reachable.
        raise IcError(message) from None

    return wrapper


class _Token:
    """Holds the token so a stray ``repr`` or ``str`` never shows its value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__


@dataclass(frozen=True)
class ContractDoc:
    key: str
    name: str
    stamp: str | None
    expected_stamp: str
    stamp_matches: bool
    content: str


def redact(text: str, token: _Token | str | None = None) -> str:
    """Return ``text`` with the token value (and anything token-shaped) replaced."""
    value = token.reveal() if isinstance(token, _Token) else token
    if value:
        text = text.replace(value, REDACTED)
    return _TOKEN_SHAPE.sub(REDACTED, text)


@_clean_errors
def read_token(environ: Mapping[str, str] | None = None) -> _Token:
    env = os.environ if environ is None else environ
    value = env.get(TOKEN_ENV, "").strip()
    if not value:
        raise IcError(f"{TOKEN_ENV} is not set; resolve the IC read-only token into the env")
    if not value.isascii() or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise IcError(f"{TOKEN_ENV} contains whitespace, control or non-ASCII characters")
    return _Token(value)


@_clean_errors
def load_base_url(config_path: Path = CONFIG_FILE, environ: Mapping[str, str] | None = None) -> str:
    """``HUB_IC_BASE_URL`` wins; otherwise ``ic.base_url`` from ``config.yaml``."""
    env = os.environ if environ is None else environ
    url = env.get(BASE_URL_ENV, "").strip()
    if not url:
        url = _base_url_from_config(config_path)
    if not _BASE_URL.fullmatch(url):
        shown = redact(repr(url), env.get(TOKEN_ENV))
        raise IcError(
            f"IC base URL must start with http:// or https:// and be scheme://host[:port][/path]"
            f" with no whitespace or control characters (got {shown})"
        )
    return url.rstrip("/")


def _base_url_from_config(config_path: Path) -> str:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise IcError(
            f"no IC base URL: set {BASE_URL_ENV} or ic.base_url in {config_path}"
        ) from None
    except OSError as exc:
        raise IcError(f"cannot read {config_path}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise IcError(f"{config_path} is not valid YAML") from None
    section = data.get("ic") if isinstance(data, dict) else None
    url = section.get("base_url") if isinstance(section, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise IcError(f"no IC base URL: set {BASE_URL_ENV} or ic.base_url in {config_path}")
    return url.strip()


def get_json(base_url: str, path: str, token: _Token, timeout: float | None = None) -> Any:
    """GET ``base_url + path`` with the bearer token and return the decoded JSON body."""
    timeout = TIMEOUT_SECONDS if timeout is None else timeout
    try:
        return _get_json(base_url, path, token, timeout)
    except IcError as exc:
        message = redact(str(exc), token)
    except Exception as exc:  # anything unforeseen still leaves as one redacted line
        message = redact(f"IC request failed: {type(exc).__name__}: {exc}", token)
    # Raised outside the handlers so the unredacted original is not reachable via __context__.
    raise IcError(message) from None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token must not travel to a ``Location`` we did not choose."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


def _build_opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) disables env and system proxies: the call goes direct to IC.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def _get_json(base_url: str, path: str, token: _Token, timeout: float) -> Any:
    url = base_url + path
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token.reveal()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with _build_opener().open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise IcError(_http_error_line(exc.code, path)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise IcError(_timeout_line(base_url, timeout)) from None
        if isinstance(exc.reason, ConnectionRefusedError):
            raise IcError(f"cannot reach IC at {base_url}: connection refused") from None
        raise IcError(f"cannot reach IC at {base_url}: {exc.reason}") from None
    except TimeoutError:
        raise IcError(_timeout_line(base_url, timeout)) from None
    except ConnectionRefusedError:
        raise IcError(f"cannot reach IC at {base_url}: connection refused") from None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IcError(f"IC returned a non-JSON response for {path}") from None


def _timeout_line(base_url: str, timeout: float) -> str:
    return f"IC at {base_url} did not respond within {timeout:g}s (timeout)"


def _http_error_line(code: int, path: str) -> str:
    if 300 <= code < 400:
        return f"IC redirected ({code}) — refusing to forward credentials"
    if code == 401:
        return f"IC rejected the token (401) for {path}: check {TOKEN_ENV} is current"
    if code == 403:
        return f"IC refused {path} (403): the token lacks the scope for this route"
    if code == 503:
        return f"IC is unavailable (503) for {path}: try again later"
    return f"IC returned HTTP {code} for {path}"


@_clean_errors
def fetch_context_pack(base_url: str, token: _Token, timeout: float | None = None) -> dict:
    pack = get_json(base_url, CONTEXT_PACK_PATH, token, timeout)
    if not isinstance(pack, dict):
        raise IcError("IC context pack is not a JSON object")
    for key in _META_KEYS:
        if not isinstance(pack.get(key), str) or not pack[key]:
            raise IcError(f"IC context pack is missing {key}")
    return pack


@_clean_errors
def fetch_contract_docs(
    base_url: str, token: _Token, timeout: float | None = None
) -> list[ContractDoc]:
    """Fetch IC's contract docs. Nothing is written to disk."""
    body = get_json(base_url, CONTRACT_DOCS_PATH, token, timeout)
    if not isinstance(body, dict):
        raise IcError("IC contract docs response is not a JSON object")
    docs = []
    for key in _DOC_KEYS:
        doc = body.get(key)
        if not isinstance(doc, dict):
            raise IcError(f"IC contract docs response is missing {key}")
        try:
            docs.append(
                ContractDoc(
                    key=key,
                    name=str(doc["filename"]),
                    stamp=None if doc.get("stamp") is None else str(doc["stamp"]),
                    expected_stamp=str(doc["expected_stamp"]),
                    stamp_matches=doc["stamp_matches"] is True,
                    content=str(doc["content"]),
                )
            )
        except KeyError as exc:
            raise IcError(f"IC contract doc {key} is missing {exc.args[0]}") from None
    return docs


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write ``obj`` as JSON via a temp file in the same directory, then rename it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@_clean_errors
def pull_pack(
    base_url: str,
    token: _Token,
    pack_dir: Path = PACK_DIR,
    timeout: float | None = None,
    now: datetime | None = None,
) -> dict:
    """Fetch the pack and store it plus its meta. Returns the meta."""
    pack = fetch_context_pack(base_url, token, timeout)
    fetched_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    meta = {"fetched_at": fetched_at, **{key: pack[key] for key in _META_KEYS}}
    try:
        # Pack first, meta second: the meta never describes a pack that is not on disk.
        write_json_atomic(pack_dir / PACK_FILE, pack)
        write_json_atomic(pack_dir / META_FILE, meta)
    except OSError as exc:
        raise IcError(f"cannot write the pack under {pack_dir}: {exc.strerror}") from None
    return meta
