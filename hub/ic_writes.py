"""Direct writes to Investing Companion (IC): the advisor-action verbs, the ``ic_writes`` log
and revert.

Every verb is one planner function that resolves names, reads the before-state and returns a
:class:`Plan` (method, path, body). :func:`run` then prints it (``--dry-run``) or applies it:
it sends the request, reads the after-state, records an ``ic_writes`` row and posts an IC
receipt. A receipt failure never undoes the write; it is recorded on the row.

Confirmation policy (the session confirms with the operator in chat first, then passes
``--yes``): ``trade log`` and any write that deactivates or removes an alert require ``--yes``.
Everything else applies directly. See ``AGENTS.md``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hub import ic, store

API = ic.API
RECEIPT_PATH = f"{API}/export/handoff-receipts"
RECEIPT_SOURCE = "investing_hub"
RECENT_HOURS = 24

ALERT_CONDITIONS = (
    "above",
    "below",
    "crosses_above",
    "crosses_below",
    "percent_up",
    "percent_down",
    "percent_from_high",
    "entry_zone",
)
TRADE_TYPES = ("buy", "sell", "short", "cover", "dividend", "split")
OUTCOMES = ("played_out", "partial", "wrong", "unclear")
CLEARABLE_ITEM_FIELDS = ("thesis", "notes", "target_price", "entry_zones", "catalyst_tags")

NEEDS_YES_TRADE = "logs a trade"
NEEDS_YES_DEACTIVATE = "deactivates an alert"
NEEDS_YES_REMOVE = "removes an alert"


class WriteError(Exception):
    """A refusal or bad input; the message is one user-facing line."""


@dataclass
class Plan:
    action: str  # advisor-action vocabulary name, e.g. ADD_ALERT; also the receipt action
    target: str
    method: str
    path: str
    body: dict | None = None
    before: Any = None
    ic_id: str | None = None
    needs_yes: str | None = None
    notes: list[str] = field(default_factory=list)
    reverts: int | None = None  # ic_writes id this plan undoes


# --- small helpers --------------------------------------------------------------------------


def iso_ms(moment: datetime) -> str:
    """``2026-10-06T20:04:05.123Z``, the format the store's timestamp default produces."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _num(text: str, flag: str) -> int | str:
    """An exact number for the request body: an int, else the decimal as a string.

    IC's money and price fields are Decimal and accept strings, so ``18.1`` is never
    rounded through a binary float on its way out.
    """
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise WriteError(f"{flag} must be a number (got {text!r})") from None
    if not value.is_finite():
        raise WriteError(f"{flag} must be a finite number (got {text!r})")
    if value == value.to_integral_value() and "." not in text and "e" not in text.lower():
        return int(value)
    return format(value, "f")


def _is_zero(text: str | None, flag: str) -> bool:
    return text is not None and Decimal(str(_num(text, flag))) == 0


def parse_zone(text: str) -> dict:
    """``tier:low:high``; an empty bound is null (``aggressive::46`` means sub-46)."""
    parts = text.split(":")
    if len(parts) != 3 or not parts[0].strip():
        raise WriteError(f"--entry-zone must look like tier:low:high (got {text!r})")
    low = _num(parts[1], "--entry-zone low") if parts[1].strip() else None
    high = _num(parts[2], "--entry-zone high") if parts[2].strip() else None
    if low is None and high is None:
        raise WriteError(f"--entry-zone {text!r} needs at least one bound")
    if low is not None and high is not None and Decimal(str(low)) >= Decimal(str(high)):
        raise WriteError(f"--entry-zone {text!r}: low must be less than high")
    return {"tier": parts[0].strip(), "low": low, "high": high}


def _date(text: str, flag: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise WriteError(f"{flag} must be YYYY-MM-DD (got {text!r})") from None


def _set(body: dict, key: str, value: Any) -> None:
    if value is not None:
        body[key] = value


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str, ensure_ascii=False)


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


NUMERIC_KEYS = frozenset(
    {"threshold_value", "target_price", "low", "high", "quantity", "price", "fees"}
)


def _norm(value: Any, key: str | None = None) -> Any:
    """Compare-friendly form. Numbers are compared by value only under ``NUMERIC_KEYS``
    ("5.00" equals 5); every other string, number and boolean is compared exactly."""
    if isinstance(value, list):
        return [_norm(v, key) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v, k) for k, v in value.items()}
    if key in NUMERIC_KEYS and isinstance(value, int | float | Decimal | str):
        if isinstance(value, bool):
            return value
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return value
    return value


def differing(live: dict, expected: dict, keys: list[str]) -> list[str]:
    """Keys (present on both sides) whose values differ between ``live`` and ``expected``."""
    return [
        k
        for k in keys
        if k in live and k in expected and _norm(live[k], k) != _norm(expected[k], k)
    ]


# --- reading state --------------------------------------------------------------------------


def resource_of(path: str) -> str:
    rest = path.removeprefix(API).strip("/").split("/")
    if rest[0] == "watchlists" and len(rest) >= 3 and rest[2] == "items":
        return "item"
    return {
        "alerts": "alert",
        "watchlists": "watchlist",
        "events": "event",
        "triggers": "trigger",
    }.get(rest[0], rest[0])


READABLE = ("alert", "item", "trigger")  # resources the advisor token can GET one of


def read_state(client: ic.IcClient, resource: str, ic_id: str | None) -> dict | None:
    """The live resource, or None if it no longer exists. Only for ``READABLE`` resources."""
    if ic_id is None:
        return None
    if resource == "alert":
        return next((a for a in client.alerts() if str(a.get("id")) == ic_id), None)
    if resource == "trigger":
        return client.trigger(ic_id)
    if resource == "item":
        wid, _, iid = ic_id.partition(":")
        return next(
            (i for i in client.watchlist(wid).get("items") or [] if str(i.get("id")) == iid),
            None,
        )
    raise WriteError(f"no read route for {resource}")


def _derive_ic_id(plan: Plan, reply: Any) -> str | None:
    if plan.ic_id is not None:
        return plan.ic_id
    if isinstance(reply, dict) and reply.get("id") is not None:
        if resource_of(plan.path) == "item" and reply.get("watchlist_id") is not None:
            return f"{reply['watchlist_id']}:{reply['id']}"
        return str(reply["id"])
    return None


# --- the log --------------------------------------------------------------------------------


def open_log(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise WriteError(f"database not found: {db} (run `hub db init`)")
    conn = store.connect(db)
    try:
        conn.execute("SELECT 1 FROM ic_writes LIMIT 1")
    except sqlite3.OperationalError:
        conn.close()
        raise WriteError(f"{db} has no ic_writes table (run `hub db migrate`)") from None
    return conn


def _insert_pending(conn: sqlite3.Connection, plan: Plan, source_ref: str | None) -> int:
    """Record the intent before anything is sent, so a crash mid-send leaves a trace."""
    cur = conn.execute(
        "INSERT INTO ic_writes (at, action, target, method, path, request, before, ic_id,"
        " source_ref, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            iso_ms(datetime.now(UTC)),
            plan.action,
            plan.target,
            plan.method,
            plan.path,
            _json(plan.body),
            _json(plan.before),
            plan.ic_id,
            source_ref,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def list_writes(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ic_writes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def recent_writes(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """Writes from the last day (not failed ones), plus any pending or unknown row of any age:
    those mean "check IC" and must not age out of sight."""
    cutoff = iso_ms(now - timedelta(hours=RECENT_HOURS))
    return conn.execute(
        "SELECT id, at, action, target, status FROM ic_writes"
        " WHERE (at >= ? AND status != 'failed') OR status IN ('pending', 'unknown')"
        " ORDER BY id",
        (cutoff,),
    ).fetchall()


def format_write(row: sqlite3.Row) -> str:
    receipt = (
        f"receipt {row['receipt_id']}"
        if row["receipt_id"]
        else "receipt FAILED"
        if row["receipt_error"]
        else "no receipt"
    )
    tail = f" (reverted by #{row['reverted_by']})" if row["reverted_by"] else ""
    ic_id = f" ic_id={row['ic_id']}" if row["ic_id"] else ""
    return (
        f"#{row['id']} {row['at']} {row['status'].upper()} {row['action']} {row['target']}"
        f"{ic_id} [{receipt}]{tail}"
    )


# --- applying -------------------------------------------------------------------------------


def _fail(name: str, message: str) -> int:
    print(f"hub ic {name}: {ic.redact(message)}", file=sys.stderr)
    return 1


def print_plan(plan: Plan, *, yes: bool, out: Callable[[str], None] = print) -> None:
    out(f"dry-run: {plan.action} {plan.target}")
    out(f"  {plan.method} {plan.path}")
    if plan.body is not None:
        out("  " + _dump(plan.body).replace("\n", "\n  "))
    for note in plan.notes:
        out(f"  note: {note}")
    if plan.needs_yes:
        state = "given" if yes else "NOT given"
        out(f"  requires --yes ({plan.needs_yes}); --yes {state}")
    out("nothing sent")


def run(
    name: str,
    args: Any,
    planner: Callable[[ic.IcClient, Any], Plan],
    *,
    client: ic.IcClient | None = None,
) -> int:
    """Plan, then print (``--dry-run``) or apply. Returns the exit code."""
    try:
        client = client or ic.IcClient.from_env()
        plan = planner(client, args)
        return execute(name, args, client, plan)
    except (ic.IcError, WriteError) as exc:
        return _fail(name, str(exc))
    except KeyError as exc:
        return _fail(name, f"unexpected IC response shape: missing {exc}")


def execute(name: str, args: Any, client: ic.IcClient, plan: Plan) -> int:
    if args.dry_run:
        print_plan(plan, yes=args.yes)
        return 0
    if plan.needs_yes and not args.yes:
        raise WriteError(
            f"{plan.action} {plan.needs_yes} and needs --yes; confirm with the operator in chat"
            " first, then re-run with --yes (use --dry-run to see the request)"
        )
    conn = open_log(args.db)  # a pending row is written before the send; no log, no write
    try:
        return _apply(name, conn, client, plan, args.source_ref, args.summary)
    finally:
        conn.close()


def _apply(
    name: str,
    conn: sqlite3.Connection,
    client: ic.IcClient,
    plan: Plan,
    source_ref: str | None,
    summary: str | None,
) -> int:
    write_id = _insert_pending(conn, plan, source_ref)  # intent first, outcome after
    try:
        reply = client.call(plan.method, plan.path, plan.body)
    except ic.IcError as exc:
        # Nothing was applied when IC refused the request (HTTP below 500) or the request
        # never left (connection refused, DNS failure): status failed. A timeout, a reset
        # after sending or a 5xx may still have been applied: status unknown.
        refused = exc.not_sent or (exc.status is not None and exc.status < 500)
        status = "failed" if refused else "unknown"
        message = ic.redact(str(exc))
        conn.execute(
            "UPDATE ic_writes SET status = ?, error = ? WHERE id = ?", (status, message, write_id)
        )
        if status == "unknown":
            print(
                f"hub ic {name}: WARN {message}; IC may or may not have applied this change:"
                f" check IC before retrying (ic_writes #{write_id} is marked unknown)",
                file=sys.stderr,
            )
            return 1
        raise
    ic_id = _derive_ic_id(plan, reply)
    after = None if plan.method == "DELETE" else reply
    try:
        # The send succeeded: record that at once, with IC's own reply as the after-state.
        conn.execute(
            "UPDATE ic_writes SET status = 'applied', after = ?, ic_id = ? WHERE id = ?",
            (_json(after), ic_id, write_id),
        )
        if plan.reverts is not None:
            conn.execute(
                "UPDATE ic_writes SET reverted_by = ? WHERE id = ?", (write_id, plan.reverts)
            )
        # Best effort: a fresh GET gives a fuller after-state.
        fuller = _read_after(client, plan, ic_id, reply)
        if fuller is not reply:
            conn.execute("UPDATE ic_writes SET after = ? WHERE id = ?", (_json(fuller), write_id))
    except BaseException as exc:  # KeyboardInterrupt included: tell the operator, re-raise
        if isinstance(exc, sqlite3.Error):
            detail = f"ic_writes #{write_id} could not be updated ({exc})"
        else:
            detail = f"{type(exc).__name__} interrupted the follow-up; see ic_writes #{write_id}"
        print(
            f"hub ic {name}: WARN the write WAS applied in IC but {detail}. Check the row and"
            f" IC; record by hand if needed: {plan.method} {plan.path} {_json(plan.body)}"
            f" -> {_json(reply)}",
            file=sys.stderr,
        )
        if isinstance(exc, sqlite3.Error):
            return 1
        raise
    receipt_id, receipt_error = post_receipt(client, plan, ic_id, summary)
    try:
        conn.execute(
            "UPDATE ic_writes SET receipt_id = ?, receipt_error = ? WHERE id = ?",
            (receipt_id, receipt_error, write_id),
        )
    except sqlite3.Error as exc:
        print(
            f"hub ic {name}: WARN applied, but the receipt result could not be recorded in"
            f" ic_writes #{write_id} ({exc}); receipt {receipt_id or receipt_error}",
            file=sys.stderr,
        )
    line = f"hub ic {name}: applied {plan.action} {plan.target} (write #{write_id}"
    line += f", IC id {ic_id}" if ic_id else ""
    print(line + (f", receipt {receipt_id})" if receipt_id else ")"))
    for note in plan.notes:
        print(f"  note: {note}")
    if receipt_error:
        print(
            f"hub ic {name}: WARN receipt failed ({receipt_error}); the write stands and is"
            f" recorded in ic_writes #{write_id}",
            file=sys.stderr,
        )
    return 0


def _read_after(client: ic.IcClient, plan: Plan, ic_id: str | None, reply: Any) -> Any:
    """The state after the write: a fresh GET where IC allows one, else IC's own reply."""
    if plan.method == "DELETE":
        return None
    resource = resource_of(plan.path)
    if resource in READABLE and ic_id:
        try:
            state = read_state(client, resource, ic_id)
        except (ic.IcError, WriteError):
            state = None
        if state is not None:
            return state
    return reply


def post_receipt(
    client: ic.IcClient, plan: Plan, ic_id: str | None, summary: str | None
) -> tuple[str | None, str | None]:
    """POST the audit receipt. Returns ``(receipt_id, error)``; never raises."""
    detail = f"{plan.method} {plan.path}" + (f" (id {ic_id})" if ic_id else "")
    if plan.body:
        detail += f" fields: {', '.join(sorted(plan.body))}"
    payload = {
        "summary": (summary or f"{plan.action} {plan.target}")[:2000],
        "actions": [
            {
                "action": plan.action,
                "target": plan.target[:200],
                "result": "applied",
                "detail": detail[:500],
            }
        ],
        "source": RECEIPT_SOURCE,
    }
    try:
        reply = client.call("POST", RECEIPT_PATH, payload)
    except Exception as exc:  # a receipt must never undo or mask the write
        return None, ic.redact(str(exc))[:300]
    receipt_id = reply.get("id") if isinstance(reply, dict) else None
    return (str(receipt_id) if receipt_id is not None else "unknown"), None


# --- planners: alerts -----------------------------------------------------------------------


def plan_alert_add(client: ic.IcClient, a: Any) -> Plan:
    chosen = [x for x in (a.symbol, a.ratio_id, a.entry_zone_item) if x is not None]
    if len(chosen) != 1:
        raise WriteError("give exactly one of --symbol, --ratio-id, --entry-zone-item")
    body: dict[str, Any] = {"condition_type": a.condition}
    if a.symbol:
        label = a.symbol.upper()
        body["equity_symbol"] = label
    elif a.ratio_id is not None:
        label = f"ratio {a.ratio_id}"
        body["ratio_id"] = a.ratio_id
    else:
        if a.condition != "entry_zone":
            raise WriteError("--entry-zone-item is only for --condition entry_zone")
        watchlist, item = client.find_item(a.entry_zone_item, a.watchlist)
        label = a.entry_zone_item.upper()
        body["watchlist_item_id"] = item["id"]
    if a.condition == "entry_zone":
        if a.threshold is not None:
            raise WriteError("entry_zone alerts take no --threshold")
    elif a.threshold is None:
        raise WriteError(f"--threshold is required for condition {a.condition}")
    else:
        body["threshold_value"] = _num(a.threshold, "--threshold")
    body["name"] = a.name or f"{label} {a.condition}" + (f" {a.threshold}" if a.threshold else "")
    _set(body, "comparison_period", a.period)
    _set(body, "cooldown_minutes", a.cooldown)
    _set(body, "confirm_checks", a.confirm_checks)
    _set(body, "notes", a.notes)
    if a.is_active is False:
        body["is_active"] = False
    return Plan("ADD_ALERT", label, "POST", f"{API}/alerts", body)


def _alert_changes(a: Any) -> dict[str, Any]:
    body: dict[str, Any] = {}
    _set(body, "name", a.new_name)
    _set(body, "notes", a.notes)
    _set(body, "condition_type", a.condition)
    if a.threshold is not None:
        body["threshold_value"] = _num(a.threshold, "--threshold")
    _set(body, "comparison_period", a.period)
    _set(body, "cooldown_minutes", a.cooldown)
    _set(body, "confirm_checks", a.confirm_checks)
    _set(body, "is_active", a.is_active)
    return body


def plan_alert_modify(client: ic.IcClient, a: Any) -> Plan:
    body = _alert_changes(a)
    if not body:
        raise WriteError("nothing to change: pass at least one field flag")
    alert = client.find_alert(a.name)
    return Plan(
        "MODIFY_ALERT",
        str(alert["name"]),
        "PUT",
        f"{API}/alerts/{alert['id']}",
        body,
        before=alert,
        ic_id=str(alert["id"]),
        needs_yes=NEEDS_YES_DEACTIVATE if body.get("is_active") is False else None,
    )


def plan_alert_remove(client: ic.IcClient, a: Any) -> Plan:
    alert = client.find_alert(a.name)
    return Plan(
        "REMOVE_ALERT",
        str(alert["name"]),
        "DELETE",
        f"{API}/alerts/{alert['id']}",
        before=alert,
        ic_id=str(alert["id"]),
        needs_yes=NEEDS_YES_REMOVE,
        notes=["linked triggers keep running but lose this alert; retire or re-point them"],
    )


# --- planners: watchlists -------------------------------------------------------------------


def _item_fields(a: Any) -> dict[str, Any]:
    body: dict[str, Any] = {}
    _set(body, "notes", a.notes)
    if a.target_price is not None:
        body["target_price"] = _num(a.target_price, "--target-price")
    _set(body, "track_calendar", a.track_calendar)
    if a.entry_zone:
        body["entry_zones"] = [parse_zone(z) for z in a.entry_zone]
    if a.catalyst_tag:
        body["catalyst_tags"] = [t.strip().lower() for t in a.catalyst_tag if t.strip()]
    return body


def plan_watchlist_add_item(client: ic.IcClient, a: Any) -> Plan:
    watchlist = client.find_watchlist(a.watchlist)
    symbol = a.symbol.strip().upper()
    body = {"symbol": symbol, **_item_fields(a)}
    _set(body, "thesis", a.thesis)
    return Plan(
        "ADD_TO_WATCHLIST",
        f"{symbol} ({watchlist['name']})",
        "POST",
        f"{API}/watchlists/{watchlist['id']}/items",
        body,
    )


def plan_watchlist_update_item(client: ic.IcClient, a: Any) -> Plan:
    watchlist, item = client.find_item(a.symbol, a.watchlist)
    body = _item_fields(a)
    if a.thesis is not None and a.append_thesis is not None:
        raise WriteError("use --thesis or --append-thesis, not both")
    if a.thesis is not None:
        body["thesis"] = a.thesis
    elif a.append_thesis is not None:
        current = (item.get("thesis") or "").rstrip()
        body["thesis"] = f"{current}\n\n{a.append_thesis}" if current else a.append_thesis
    for name in a.clear or []:
        if name in body:
            raise WriteError(f"--clear {name} conflicts with a value given for it")
        body[name] = None
    if not body:
        raise WriteError("nothing to change: pass at least one field flag")
    symbol = a.symbol.strip().upper()
    notes = []
    if "entry_zones" in body and body["entry_zones"] is not None:
        notes.append("entry zones replace the item's whole zone list")
    return Plan(
        "UPDATE_WATCHLIST_ITEM",
        f"{symbol} ({watchlist['name']})",
        "PUT",
        f"{API}/watchlists/{watchlist['id']}/items/{item['id']}",
        body,
        before=item,
        ic_id=f"{watchlist['id']}:{item['id']}",
        notes=notes,
    )


def plan_watchlist_create(client: ic.IcClient, a: Any) -> Plan:
    name = a.name.strip()
    if any(str(w.get("name", "")).casefold() == name.casefold() for w in client.watchlists()):
        raise WriteError(f"a watchlist named {name!r} already exists")
    body: dict[str, Any] = {"name": name}
    _set(body, "description", a.description)
    return Plan("CREATE_WATCHLIST", name, "POST", f"{API}/watchlists", body)


# --- planners: events -----------------------------------------------------------------------

_ID = re.compile(r"[0-9A-Za-z-]+")
NO_EVENT_READ = "IC's advisor token has no event read route: no before-state is captured"


def _id(text: str) -> str:
    if not _ID.fullmatch(text):
        raise WriteError(f"--id must be an IC id (got {text!r})")
    return text


def plan_event_add(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {
        "event_type": a.type,
        "title": a.title,
        "event_date": _date(a.date, "--date"),
    }
    _set(body, "description", a.description)
    _set(body, "importance", a.importance)
    if a.symbol:
        body["equity_symbol"] = a.symbol.upper()
    return Plan("ADD_CALENDAR_EVENT", a.title, "POST", f"{API}/events", body)


def plan_event_update(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {}
    _set(body, "title", a.new_title)
    if a.date is not None:
        body["event_date"] = _date(a.date, "--date")
    _set(body, "description", a.description)
    _set(body, "importance", a.importance)
    if not body:
        raise WriteError("nothing to change: pass at least one field flag")
    event_id = _id(a.id)
    return Plan(
        "UPDATE_CALENDAR_EVENT",
        a.new_title or f"event {event_id}",
        "PUT",
        f"{API}/events/{event_id}",
        body,
        ic_id=event_id,
        notes=[NO_EVENT_READ],
    )


def plan_event_remove(client: ic.IcClient, a: Any) -> Plan:
    event_id = _id(a.id)
    return Plan(
        "REMOVE_CALENDAR_EVENT",
        f"event {event_id}",
        "DELETE",
        f"{API}/events/{event_id}",
        ic_id=event_id,
        notes=[NO_EVENT_READ + "; a removed event cannot be re-created by revert"],
    )


# --- planners: trades, ratios, lessons ------------------------------------------------------


def plan_trade_log(client: ic.IcClient, a: Any) -> Plan:
    symbol = a.symbol.strip().upper()
    body: dict[str, Any] = {
        "symbol": symbol,
        "trade_type": a.type,
        "quantity": _num(a.quantity, "--quantity"),
    }
    if a.type == "split":
        if a.account or (a.fees is not None and not _is_zero(a.fees, "--fees")):
            raise WriteError("a split carries no account and no fees")
        if a.price is not None and not _is_zero(a.price, "--price"):
            raise WriteError("a split's price must be 0 (quantity is the ratio)")
        body["price"] = 0
    else:
        if a.price is None:
            raise WriteError("--price is required")
        body["price"] = _num(a.price, "--price")
    if a.type == "dividend" and not a.account:
        raise WriteError("a dividend needs --account (its cash is folded per account)")
    body["fees"] = _num(a.fees, "--fees") if a.fees is not None else 0
    body["executed_at"] = _executed_at(a.executed_at)
    _set(body, "notes", a.notes)
    label = f"{symbol} {a.type}"
    if a.account:
        account = client.find_account(a.account)
        body["account_id"] = account["id"]
        label += f" ({account['name']})"
    return Plan("LOG_TRADE", label, "POST", f"{API}/trades", body, needs_yes=NEEDS_YES_TRADE)


def _executed_at(text: str | None) -> str:
    """UTC timestamp for IC. A bare date means noon UTC that day (so it shows as that date in
    any US zone); a datetime without an offset is read as UTC."""
    if text is None:
        return iso_ms(datetime.now(UTC))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise WriteError(f"--executed-at must be an ISO date or datetime (got {text!r})") from None
    if "T" not in text and " " not in text:
        parsed = parsed.replace(hour=12)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return iso_ms(parsed)


def plan_ratio_add(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {
        "name": a.name,
        "numerator_symbol": a.numerator.upper(),
        "denominator_symbol": a.denominator.upper(),
    }
    _set(body, "description", a.description)
    _set(body, "category", a.category)
    return Plan("ADD_RATIO", a.name, "POST", f"{API}/ratios", body)


def plan_lesson_add(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {
        "symbol": a.symbol.strip().upper(),
        "thesis_outcome": a.outcome,
        "lesson": a.lesson,
    }
    if a.tag:
        body["tags"] = a.tag
    _set(body, "trade_id", a.trade_id)
    return Plan("ADD_LESSON", body["symbol"], "POST", f"{API}/lessons", body)


# --- planners: triggers ---------------------------------------------------------------------
# Triggers are found by name through GET /triggers; before and after state come from
# GET /triggers/{id} (both are on the advisor:write allow-list).


def _find_trigger(client: ic.IcClient, a: Any) -> tuple[str, str, dict]:
    """``(id, label, live trigger row)`` for ``a.name`` or ``a.id``."""
    if a.id is not None:
        trigger_id = _id(a.id)
        row = read_state(client, "trigger", trigger_id)
        if row is None:
            raise WriteError(f"no trigger with id {trigger_id}")
        return trigger_id, str(row.get("name", f"trigger {trigger_id}")), row
    if not a.name:
        raise WriteError("give a trigger NAME or --id")
    active = client.triggers()
    try:
        row = ic.resolve_name(active, a.name, "trigger")
    except ic.ResolutionError as exc:
        # No active match: look among retired ones too, so a retired trigger is named, not lost
        everything = client.triggers(include_retired=True)
        if len(everything) == len(active):
            raise
        try:
            row = ic.resolve_name(everything, a.name, "trigger")
        except ic.ResolutionError:
            raise exc from None
    return str(row["id"]), str(row["name"]), row


def plan_trigger_add(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {"name": a.name, "rule": a.rule, "action": a.action}
    _set(body, "tier", a.tier)
    _set(body, "display_order", a.order)
    if a.alert:
        body["alert_ids"] = _alert_ids(client, a.alert)
    return Plan("ADD_TRIGGER", a.name, "POST", f"{API}/triggers", body)


def _alert_ids(client: ic.IcClient, names: list[str]) -> list[int]:
    alerts = client.alerts()
    return [ic.resolve_name(alerts, n, "alert")["id"] for n in names]


def plan_trigger_update(client: ic.IcClient, a: Any) -> Plan:
    body: dict[str, Any] = {}
    _set(body, "name", a.new_name)
    _set(body, "rule", a.rule)
    _set(body, "action", a.action)
    _set(body, "tier", a.tier)
    _set(body, "display_order", a.order)
    if a.alert and a.clear_alerts:
        raise WriteError("use --alert or --clear-alerts, not both")
    if a.alert:
        body["alert_ids"] = _alert_ids(client, a.alert)
    elif a.clear_alerts:
        body["alert_ids"] = []
    if not body:
        raise WriteError("nothing to change: pass at least one field flag")
    trigger_id, label, row = _find_trigger(client, a)
    return Plan(
        "UPDATE_TRIGGER",
        label,
        "PUT",
        f"{API}/triggers/{trigger_id}",
        body,
        before=row,
        ic_id=trigger_id,
    )


def plan_trigger_retire(client: ic.IcClient, a: Any) -> Plan:
    trigger_id, label, row = _find_trigger(client, a)
    return Plan(
        "RETIRE_TRIGGER",
        label,
        "POST",
        f"{API}/triggers/{trigger_id}/retire",
        before=row,
        ic_id=trigger_id,
        notes=["retiring is terminal and does not silence the trigger's linked alerts"],
    )


# --- revert ---------------------------------------------------------------------------------

_SEG = re.compile(rf"^{re.escape(API)}/(alerts|events|triggers|watchlists)(?:/([0-9A-Za-z-]+))?")


def _alert_create_body(before: dict) -> dict:
    body: dict[str, Any] = {}
    for key in (
        "name",
        "notes",
        "condition_type",
        "threshold_value",
        "comparison_period",
        "cooldown_minutes",
        "confirm_checks",
        "is_active",
    ):
        if before.get(key) is not None:
            body[key] = before[key]
    target = before.get("target") or {}
    if before.get("condition_type") == "entry_zone":
        body["watchlist_item_id"] = before.get("watchlist_item_id")
        body.pop("threshold_value", None)
    elif target.get("type") == "ratio" or (before.get("ratio_id") and not target):
        body["ratio_id"] = target.get("id") or before.get("ratio_id")
    else:
        symbol = target.get("symbol")
        if not symbol:
            raise WriteError("the recorded alert has no target symbol; cannot re-create it")
        body["equity_symbol"] = symbol
    return body


def _later_writes(conn: sqlite3.Connection, row: sqlite3.Row) -> list[int]:
    """Later, unreverted hub writes to the same IC resource (so the recorded state is stale)."""
    resource = resource_of(row["path"])
    rows = conn.execute(
        "SELECT id, path FROM ic_writes WHERE id > ? AND ic_id = ? AND reverted_by IS NULL"
        " AND status != 'failed'"
        " AND action NOT LIKE 'REVERT\\_%' ESCAPE '\\'",
        (row["id"], row["ic_id"]),
    ).fetchall()
    return [r["id"] for r in rows if resource_of(r["path"]) == resource]


def _with_alert_ids(state: dict) -> dict:
    """A trigger row carries ``alerts`` (summaries); requests carry ``alert_ids``."""
    if "alerts" in state and "alert_ids" not in state:
        ids = sorted(a.get("id") for a in state["alerts"] or [])
        return {**state, "alert_ids": ids}
    return state


def _check_live(
    client: ic.IcClient,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    keys: list[str],
    what: str,
) -> dict | None:
    """Refuse unless the resource still looks like the state this write left it in.

    Readable resources are compared field by field; the rest (no read route for the advisor
    token) are only checked against later hub writes. Returns the live state, or None if
    the resource cannot be read.
    """
    resource = resource_of(row["path"])
    if resource not in READABLE:
        later = _later_writes(conn, row)
        if later:
            ids = ", ".join(f"#{i}" for i in later)
            raise WriteError(f"{what} was changed by later hub write(s) {ids}; revert those first")
        return None
    live = read_state(client, resource, row["ic_id"])
    if live is None:
        raise WriteError(f"{what} no longer exists in IC; nothing to revert")
    expected = _load(row["after"]) or _load(row["request"]) or {}
    changed = differing(_with_alert_ids(live), _with_alert_ids(expected), keys)
    if changed:
        raise WriteError(
            f"{what} was changed since write #{row['id']} (differs on: {', '.join(changed)});"
            " refusing to overwrite. Inspect it in IC and fix by hand"
        )
    return live


def plan_revert(conn: sqlite3.Connection, client: ic.IcClient, write_id: int) -> Plan:
    row = conn.execute("SELECT * FROM ic_writes WHERE id = ?", (write_id,)).fetchone()
    if row is None:
        raise WriteError(f"no ic_writes row #{write_id}")
    if row["action"].startswith("REVERT_"):
        raise WriteError(f"write #{write_id} is itself a revert; make the change again instead")
    if row["status"] != "applied":
        raise WriteError(
            f"write #{write_id} has status {row['status']!r}; only applied writes can be reverted"
        )
    if row["reverted_by"]:
        raise WriteError(f"write #{write_id} was already reverted by #{row['reverted_by']}")
    match = _SEG.match(row["path"])
    if match is None:
        raise WriteError(_irreversible(row))
    kind, method = match.group(1), row["method"]
    request = _load(row["request"]) or {}
    before = _load(row["before"])
    keys = list(request)
    what = f"{row['target']} (write #{write_id})"
    action = f"REVERT_{row['action']}"
    target = f"write #{write_id}: {row['target']}"
    ic_id = row["ic_id"]

    def plan(
        verb: str, path: str, body: dict | None, needs_yes: str | None = None, **kw: Any
    ) -> Plan:
        return Plan(action, target, verb, path, body, needs_yes=needs_yes, reverts=write_id, **kw)

    if kind == "alerts" and method == "POST" and match.group(2) is None:
        live = _check_live(client, conn, row, keys, what)
        path = f"{API}/alerts/{ic_id}"
        return plan("DELETE", path, None, NEEDS_YES_REMOVE, before=live, ic_id=ic_id)
    if kind == "events" and method == "POST" and match.group(2) is None:
        _check_live(client, conn, row, keys, what)
        return plan("DELETE", f"{API}/events/{ic_id}", None, ic_id=ic_id)
    if method == "PUT" and kind in ("alerts", "watchlists", "triggers"):
        return _plan_revert_modify(client, conn, row, plan, keys, what, before, request)
    if kind == "alerts" and method == "DELETE":
        if not isinstance(before, dict):
            raise WriteError("no before-state was recorded for this removal; cannot re-create it")
        if read_state(client, "alert", ic_id) is not None:
            raise WriteError(f"alert id {ic_id} exists again; nothing to revert")
        body = _alert_create_body(before)
        return plan(
            "POST",
            f"{API}/alerts",
            body,
            notes=["the re-created alert gets a new IC id; trigger links it had are not restored"],
        )
    raise WriteError(_irreversible(row))


def _plan_revert_modify(client, conn, row, plan, keys, what, before, request) -> Plan:
    if not isinstance(before, dict):
        raise WriteError("no before-state was recorded for this change; cannot restore it")
    _check_live(client, conn, row, keys, what)
    restore: dict[str, Any] = {}
    for key in keys:
        if key == "alert_ids":
            restore[key] = [a.get("id") for a in before.get("alerts") or []]
        elif key in before:
            restore[key] = before[key]
    if not restore:
        raise WriteError("the recorded before-state has none of the changed fields")
    needs_yes = NEEDS_YES_DEACTIVATE if restore.get("is_active") is False else None
    live = _live_or_none(client, row)
    return plan("PUT", row["path"], restore, needs_yes, before=live, ic_id=row["ic_id"])


def _live_or_none(client: ic.IcClient, row: sqlite3.Row) -> dict | None:
    resource = resource_of(row["path"])
    return read_state(client, resource, row["ic_id"]) if resource in READABLE else None


def _irreversible(row: sqlite3.Row) -> str:
    reasons = {
        "ADD_TO_WATCHLIST": "IC has no delete route for watchlist items",
        "CREATE_WATCHLIST": "IC has no delete route for watchlists",
        "ADD_RATIO": "IC has no delete route for ratios",
        "LOG_TRADE": "IC does not let this token edit or delete trades",
        "ADD_LESSON": "IC has no delete route for lessons",
        "ADD_TRIGGER": "IC does not let this token delete triggers",
        "RETIRE_TRIGGER": "retiring a trigger is terminal",
        "UPDATE_CALENDAR_EVENT": "no before-state exists for events",
        "REMOVE_CALENDAR_EVENT": "a removed event cannot be re-created without its before-state",
    }
    why = reasons.get(row["action"], "this write has no safe inverse")
    return f"cannot revert {row['action']} (write #{row['id']}): {why}. Correct it in IC by hand"


def run_revert(args: Any, *, client: ic.IcClient | None = None) -> int:
    name = "revert"
    try:
        client = client or ic.IcClient.from_env()
        conn = open_log(args.db)
        try:
            plan = plan_revert(conn, client, args.id)
        finally:
            conn.close()
        return execute(name, args, client, plan)
    except (ic.IcError, WriteError) as exc:
        return _fail(name, str(exc))
    except KeyError as exc:
        return _fail(name, f"unexpected IC response shape: missing {exc}")


def run_writes(args: Any) -> int:
    try:
        conn = open_log(args.db)
    except WriteError as exc:
        return _fail("writes", str(exc))
    try:
        rows = list_writes(conn, args.limit)
    finally:
        conn.close()
    if not rows:
        print("no IC writes recorded")
    for row in rows:
        print(format_write(row))
    return 0
