"""Client for the Investing Companion (IC) API: the pack read path and the advisor write path.

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
import socket
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
REDACTED = "ict_...<redacted>"
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
    """A failure talking to IC. The message is one line and already redacted.

    ``status`` is the HTTP status when IC answered, else None (no answer: timeout, refused).
    """

    status: int | None = None
    # True when the request provably never reached IC (connection refused, DNS failure)
    not_sent: bool = False


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
        raise IcError(f"{TOKEN_ENV} is not set; resolve the IC API token into the env")
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
    return _request(base_url, "GET", path, token, timeout)


def _request(
    base_url: str,
    method: str,
    path: str,
    token: _Token,
    timeout: float,
    payload: Any = None,
    *,
    for_write: bool = False,
) -> Any:
    """One HTTP exchange. ``for_write`` allows an empty reply (204) and adds IC's error detail."""
    headers = {"Authorization": f"Bearer {token.reveal()}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with _build_opener().open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        line = _http_error_line(exc.code, path)
        detail = _error_detail(exc) if for_write else ""
        error = IcError(f"{line}: {detail}" if detail else line)
        error.status = exc.code
        raise error from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise IcError(_timeout_line(base_url, timeout)) from None
        never_sent = isinstance(exc.reason, ConnectionRefusedError | socket.gaierror)
        if isinstance(exc.reason, ConnectionRefusedError):
            error = IcError(f"cannot reach IC at {base_url}: connection refused")
        else:
            error = IcError(f"cannot reach IC at {base_url}: {exc.reason}")
        error.not_sent = never_sent
        raise error from None
    except TimeoutError:
        raise IcError(_timeout_line(base_url, timeout)) from None
    except ConnectionRefusedError:
        error = IcError(f"cannot reach IC at {base_url}: connection refused")
        error.not_sent = True
        raise error from None
    if for_write and not body.strip():
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IcError(f"IC returned a non-JSON response for {path}") from None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """IC's one-line validation message (FastAPI ``detail``), truncated; empty if unreadable."""
    try:
        payload = json.loads(exc.read())
    except Exception:
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                where = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                parts.append(f"{where}: {item.get('msg')}" if where else str(item.get("msg")))
            else:
                parts.append(str(item))
        detail = "; ".join(parts)
    if not isinstance(detail, str):
        return ""
    detail = " ".join(detail.split())
    return detail if len(detail) <= 300 else detail[:297] + "..."


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


def request_json(
    base_url: str,
    method: str,
    path: str,
    token: _Token,
    payload: Any = None,
    timeout: float | None = None,
) -> Any:
    """Send ``method path`` (optional JSON body) and return the decoded JSON, or None for an
    empty reply. Errors leave as one redacted line carrying IC's validation detail."""
    timeout = TIMEOUT_SECONDS if timeout is None else timeout
    status, not_sent = None, False
    try:
        return _request(base_url, method.upper(), path, token, timeout, payload, for_write=True)
    except IcError as exc:
        message, status, not_sent = redact(str(exc), token), exc.status, exc.not_sent
    except Exception as exc:
        message = redact(f"IC request failed: {type(exc).__name__}: {exc}", token)
    error = IcError(message)
    error.status = status
    error.not_sent = not_sent
    raise error from None


# --- advisor write client -------------------------------------------------------------------

API = "/api/v1"
MAX_LISTED = 20


class ResolutionError(IcError):
    """A name matched zero or several things; the message lists the candidates."""


class IcClient:
    """The advisor write client: ``call`` plus name resolution over IC's list endpoints.

    ``call`` returns IC's payload unwrapped from ``{"data": ...}`` (None for an empty reply).
    The token must carry the ``advisor:write`` scope for anything but pack reads.
    """

    def __init__(self, base_url: str, token: _Token, timeout: float | None = None) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> IcClient:
        return cls(load_base_url(), read_token())

    def call(self, method: str, path: str, body: Any = None) -> Any:
        reply = request_json(self.base_url, method, path, self.token, body, self.timeout)
        if isinstance(reply, dict) and "data" in reply:
            return reply["data"]
        return reply

    # reads used to resolve names and capture state

    def alerts(self) -> list[dict]:
        return _as_list(self.call("GET", f"{API}/alerts"), "alerts")

    def watchlists(self) -> list[dict]:
        return _as_list(self.call("GET", f"{API}/watchlists"), "watchlists")

    def watchlist(self, watchlist_id: int | str) -> dict:
        data = self.call("GET", f"{API}/watchlists/{watchlist_id}")
        if not isinstance(data, dict):
            raise IcError("IC returned an unexpected watchlist payload")
        return data

    def triggers(self, include_retired: bool = False) -> list[dict]:
        query = "?include_retired=true" if include_retired else ""
        return _as_list(self.call("GET", f"{API}/triggers{query}"), "triggers")

    def trigger(self, trigger_id: int | str) -> dict | None:
        """One trigger by id (``GET /triggers/{id}``, retired ones included); None if absent."""
        try:
            data = self.call("GET", f"{API}/triggers/{trigger_id}")
        except IcError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise IcError("IC returned an unexpected trigger payload")
        return data

    def accounts(self) -> list[dict]:
        return _as_list(self.call("GET", f"{API}/accounts"), "accounts")

    # resolution: exact name, else unique prefix; zero or several matches raise

    def find_alert(self, name: str) -> dict:
        return resolve_name(self.alerts(), name, "alert")

    def find_watchlist(self, name: str) -> dict:
        return resolve_name(self.watchlists(), name, "watchlist")

    def find_account(self, name: str) -> dict:
        return resolve_name(self.accounts(), name, "account")

    def find_item(self, symbol: str, watchlist_name: str | None = None) -> tuple[dict, dict]:
        """The watchlist item for ``symbol`` as ``(watchlist, item)``.

        With ``watchlist_name`` only that watchlist is searched; without it every watchlist is,
        and a symbol on several watchlists is an error naming each one.
        """
        symbol = symbol.strip().upper()
        if watchlist_name is not None:
            picked = [self.watchlist(self.find_watchlist(watchlist_name)["id"])]
        else:
            picked = [self.watchlist(w["id"]) for w in self.watchlists()]
        hits = [
            (w, item)
            for w in picked
            for item in w.get("items") or []
            if str((item.get("equity") or {}).get("symbol", "")).upper() == symbol
        ]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            where = f" on watchlist {picked[0].get('name')!r}" if watchlist_name else ""
            raise ResolutionError(f"no watchlist item for {symbol}{where}")
        names = ", ".join(f"{symbol} ({w.get('name')})" for w, _ in hits)
        raise ResolutionError(
            f"{symbol} is on {len(hits)} watchlists: {names}; pass --watchlist NAME to choose"
        )


def _as_list(data: Any, what: str) -> list[dict]:
    if not isinstance(data, list):
        raise IcError(f"IC returned an unexpected {what} payload")
    return [row for row in data if isinstance(row, dict)]


def resolve_name(rows: list[dict], query: str, label: str, key: str = "name") -> dict:
    """The one row whose ``key`` equals ``query`` (case-insensitive), else the one whose ``key``
    starts with it. Zero or several matches raise :class:`ResolutionError` listing candidates."""
    wanted = query.strip().casefold()
    names = [str(row.get(key, "")) for row in rows]
    exact = [r for r, n in zip(rows, names, strict=True) if n.casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ResolutionError(_ambiguous(label, query, exact, key))
    prefix = [
        r for r, n in zip(rows, names, strict=True) if wanted and n.casefold().startswith(wanted)
    ]
    if len(prefix) == 1:
        return prefix[0]
    if prefix:
        raise ResolutionError(_ambiguous(label, query, prefix, key))
    shown = ", ".join(repr(n) for n in names[:MAX_LISTED])
    if len(names) > MAX_LISTED:
        shown += f", ... ({len(names)} total)"
    raise ResolutionError(
        f"no {label} matches {query!r}" + (f"; known {label}s: {shown}" if shown else "")
    )


def _ambiguous(label: str, query: str, rows: list[dict], key: str) -> str:
    cands = ", ".join(f"{r.get(key)!r} (id {r.get('id')})" for r in rows[:MAX_LISTED])
    return f"{len(rows)} {label}s match {query!r}: {cands}; use a longer or exact name"
