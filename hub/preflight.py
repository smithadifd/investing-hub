"""Read-only readiness checks against the deployed IC export endpoints."""

from __future__ import annotations

from hub import ic
from hub.session_open import DOC_VERSION_KEY


def _fetch(base_url: str, path: str, token: ic._Token) -> tuple[dict | None, str]:
    try:
        body = ic.get_json(base_url, path, token)
    except ic.IcError as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return None, "response is not a JSON object"
    return body, "JSON object received"


def build_report() -> tuple[str, bool]:
    """Fetch both endpoints and return a source-attributed report and readiness flag."""
    try:
        token = ic.read_token()
        base_url = ic.load_base_url()
        config_error = None
    except ic.IcError as exc:
        token = None
        base_url = None
        config_error = str(exc)

    def fetch(path: str) -> tuple[dict | None, str]:
        if token is None or base_url is None:
            return None, config_error or "IC configuration unavailable"
        return _fetch(base_url, path, token)

    pack, pack_reason = fetch(ic.CONTEXT_PACK_PATH)
    docs, docs_reason = fetch(ic.CONTRACT_DOCS_PATH)
    checks: list[tuple[str, str, bool, str]] = []

    def add(label: str, source: str, passed: bool, detail: str) -> None:
        checks.append((label, source, passed, detail.replace("\r", " ").replace("\n", " ")))

    pack_source = f"GET {ic.CONTEXT_PACK_PATH}"
    docs_source = f"GET {ic.CONTRACT_DOCS_PATH}"
    add("pack reachable", pack_source, pack is not None, pack_reason)
    for key in ("generated_at", "schema_version", "advisor_actions_version"):
        present = pack is not None and isinstance(pack.get(key), str) and bool(pack[key])
        add(
            f"{key} present",
            f"{pack_source}#/{key}",
            present,
            "present" if present else "missing or empty",
        )
    add("contract docs reachable", docs_source, docs is not None, docs_reason)

    for doc_key, pack_key in DOC_VERSION_KEY.items():
        doc = docs.get(doc_key) if docs is not None else None
        doc = doc if isinstance(doc, dict) else None
        source = f"{docs_source}#/{doc_key}"
        stamp = doc.get("stamp") if doc is not None else None
        expected = doc.get("expected_stamp") if doc is not None else None
        present = (
            isinstance(stamp, str)
            and bool(stamp)
            and isinstance(expected, str)
            and bool(expected)
        )
        add(
            f"{doc_key} stamps present",
            source,
            present,
            "present" if present else "missing or empty",
        )
        pack_stamp = pack.get(pack_key) if pack is not None else None
        matching = (
            present
            and stamp == expected == pack_stamp
            and doc is not None
            and doc.get("stamp_matches") is True
        )
        add(
            f"{doc_key} stamps match",
            f"{source} and {pack_source}#/{pack_key}",
            matching,
            "stamp, expected stamp and pack version agree"
            if matching
            else "stamp, expected stamp or pack version disagree",
        )

    lines = ["== hub ic preflight =="]
    for label, source, passed, detail in checks:
        lines.append(f"{'PASS' if passed else 'FAIL'} {label} [{source}]: {detail}")
    failures = sum(not passed for _, _, passed, _ in checks)
    lines.append(
        f"{'PASS' if not failures else 'FAIL'}: {len(checks) - failures} passed, {failures} failed"
    )
    return ic.redact("\n".join(lines) + "\n", token), failures == 0
