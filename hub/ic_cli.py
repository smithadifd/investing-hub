"""``hub ic`` write verbs: argparse wiring and one handler per advisor action.

The logic (planning, applying, logging, revert) is in :mod:`hub.ic_writes`; this module only
declares flags and hands the parsed arguments to the matching planner.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from hub import ic_writes as w
from hub import store

Handler = Callable[[argparse.Namespace], int]
Adder = Callable[[argparse.ArgumentParser], None]

IMPORTANCE = ("low", "medium", "high")
# IC's EventType enum (backend/app/schemas/economic_event.py)
EVENT_TYPES = (
    "earnings",
    "ex_dividend",
    "dividend_pay",
    "stock_split",
    "fomc",
    "cpi",
    "ppi",
    "nfp",
    "gdp",
    "pce",
    "retail_sales",
    "unemployment",
    "ism_manufacturing",
    "ism_services",
    "housing_starts",
    "consumer_confidence",
    "custom",
    "ipo",
)
CATEGORIES = ("commodity", "equity", "macro", "crypto")
PERIODS = ("1d", "1w", "1m", "3m", "6m", "1y")


def _common(p: argparse.ArgumentParser, *, yes_help: str | None = None) -> None:
    p.add_argument(
        "--db",
        type=Path,
        default=store.DEFAULT_DB_PATH,
        help="database holding the ic_writes log (default: %(default)s)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the resolved request; send and log nothing"
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help=yes_help or "accepted for uniformity; this write needs no confirmation",
    )
    p.add_argument("--source-ref", metavar="REF", help="provenance recorded in ic_writes")
    p.add_argument("--summary", metavar="TEXT", help="receipt summary (default: action + target)")


def _active(p: argparse.ArgumentParser, *, allow_active: bool = True) -> None:
    group = p.add_mutually_exclusive_group()
    if allow_active:
        group.add_argument("--active", dest="is_active", action="store_const", const=True)
    group.add_argument("--inactive", dest="is_active", action="store_const", const=False)
    p.set_defaults(is_active=None)


def _alert_fields(p: argparse.ArgumentParser, *, condition_required: bool = False) -> None:
    p.add_argument("--condition", choices=w.ALERT_CONDITIONS, required=condition_required)
    p.add_argument("--threshold", metavar="N")
    p.add_argument("--period", choices=PERIODS, help="comparison period for percent conditions")
    p.add_argument("--cooldown", type=int, metavar="MINUTES")
    p.add_argument("--confirm-checks", type=int, metavar="N", help="crossing conditions only")
    p.add_argument("--notes")


YES_ALERT = (
    "required when this sets or changes a threshold or condition, or deactivates (--inactive)"
    " the alert (confirm with the operator in chat first)"
)
YES_LEVEL = (
    "required when this sets, changes or clears a target price or entry zones (confirm with the"
    " operator in chat first)"
)


def _alert_add_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help="required: every new alert sets a level (confirm with the operator first)")
    p.add_argument("--symbol", help="equity target")
    p.add_argument("--ratio-id", type=int, help="ratio target (id from `ratio add`)")
    p.add_argument("--entry-zone-item", metavar="SYMBOL", help="watchlist item, for entry_zone")
    p.add_argument("--watchlist", help="watchlist name, to pick among several holding the item")
    p.add_argument("--name", help="alert name (default: derived)")
    _alert_fields(p, condition_required=True)
    _active(p, allow_active=False)


def _alert_modify_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help=YES_ALERT)
    p.add_argument("name", help="alert name: exact, else a unique prefix")
    p.add_argument("--name", dest="new_name", help="rename the alert")
    _alert_fields(p)
    _active(p)


def _alert_remove_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help="required: removing an alert (confirm with the operator in chat first)")
    p.add_argument("name", help="alert name: exact, else a unique prefix")


def _item_fields(p: argparse.ArgumentParser) -> None:
    p.add_argument("--notes")
    p.add_argument("--target-price", metavar="N")
    p.add_argument("--track-calendar", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument(
        "--entry-zone",
        action="append",
        metavar="TIER:LOW:HIGH",
        help="repeatable; an empty bound is null (aggressive::46); replaces all zones on update",
    )
    p.add_argument(
        "--catalyst-tag", action="append", metavar="TAG", help="repeatable; replaces all tags"
    )


def _watchlist_add_item_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help=YES_LEVEL)
    p.add_argument("symbol")
    p.add_argument("--watchlist", required=True, help="watchlist name: exact, else unique prefix")
    p.add_argument("--thesis")
    _item_fields(p)


def _watchlist_update_item_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help=YES_LEVEL)
    p.add_argument("symbol")
    p.add_argument("--watchlist", help="watchlist name; needed only if the symbol is on several")
    p.add_argument("--thesis", help="replace the thesis")
    p.add_argument("--append-thesis", metavar="TEXT", help="append to the current thesis")
    p.add_argument(
        "--clear",
        action="append",
        choices=w.CLEARABLE_ITEM_FIELDS,
        help="set a field to null; repeatable",
    )
    _item_fields(p)


def _watchlist_create_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("name")
    p.add_argument("--description")


def _event_add_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("--type", default="custom", choices=EVENT_TYPES, help="default: custom")
    p.add_argument("--title", required=True)
    p.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--description")
    p.add_argument("--importance", choices=IMPORTANCE)
    p.add_argument("--symbol", help="equity the event belongs to")


def _event_update_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("--id", required=True, help="IC event id (printed by `event add`)")
    p.add_argument("--title", dest="new_title")
    p.add_argument("--date", metavar="YYYY-MM-DD")
    p.add_argument("--description")
    p.add_argument("--importance", choices=IMPORTANCE)


def _event_remove_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("--id", required=True, help="IC event id (printed by `event add`)")


def _trade_log_args(p: argparse.ArgumentParser) -> None:
    _common(p, yes_help="required: logging a trade (confirm with the operator in chat first)")
    p.add_argument("symbol")
    p.add_argument("--type", required=True, choices=w.TRADE_TYPES)
    p.add_argument("--quantity", required=True, metavar="N")
    p.add_argument("--price", metavar="N", help="per share; 0 or omitted for a split")
    p.add_argument("--fees", metavar="N")
    p.add_argument("--account", help="account name (required for a dividend)")
    p.add_argument("--executed-at", metavar="ISO", help="date or datetime (default: now)")
    p.add_argument("--notes")


def _trigger_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", nargs="?", help="trigger name, looked up in the cached pack")
    p.add_argument("--id", help="IC trigger id (instead of NAME)")


def _trigger_add_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("name")
    p.add_argument("--rule", required=True, help='the condition: "if X"')
    p.add_argument("--action", required=True, help='the response: "then I do Y"')
    p.add_argument("--tier")
    p.add_argument("--order", type=int, help="display_order")
    p.add_argument("--alert", action="append", metavar="NAME", help="linked alert; repeatable")


def _trigger_update_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    _trigger_target(p)
    p.add_argument("--rename", dest="new_name")
    p.add_argument("--rule")
    p.add_argument("--action")
    p.add_argument("--tier")
    p.add_argument("--order", type=int, help="display_order")
    p.add_argument(
        "--alert", action="append", metavar="NAME", help="linked alert; repeatable; replaces links"
    )
    p.add_argument("--clear-alerts", action="store_true", help="unlink every alert")


def _trigger_retire_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    _trigger_target(p)


def _lesson_add_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("symbol")
    p.add_argument("--outcome", required=True, choices=w.OUTCOMES)
    p.add_argument("--lesson", required=True)
    p.add_argument("--tag", action="append", help="repeatable")
    p.add_argument("--trade-id", type=int)


def _ratio_add_args(p: argparse.ArgumentParser) -> None:
    _common(p)
    p.add_argument("name")
    p.add_argument("--numerator", required=True, metavar="SYMBOL")
    p.add_argument("--denominator", required=True, metavar="SYMBOL")
    p.add_argument("--description")
    p.add_argument("--category", choices=CATEGORIES)


def _revert_args(p: argparse.ArgumentParser) -> None:
    _common(
        p,
        yes_help="required when the revert removes or deactivates an alert",
    )
    p.add_argument("id", type=int, help="ic_writes id (see `hub ic writes`)")


def _writes_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=store.DEFAULT_DB_PATH)
    p.add_argument("--limit", type=int, default=20, metavar="N")


def cmd_ic_alert_add(args: argparse.Namespace) -> int:
    return w.run("alert add", args, w.plan_alert_add)


def cmd_ic_alert_modify(args: argparse.Namespace) -> int:
    return w.run("alert modify", args, w.plan_alert_modify)


def cmd_ic_alert_remove(args: argparse.Namespace) -> int:
    return w.run("alert remove", args, w.plan_alert_remove)


def cmd_ic_watchlist_add_item(args: argparse.Namespace) -> int:
    return w.run("watchlist add-item", args, w.plan_watchlist_add_item)


def cmd_ic_watchlist_update_item(args: argparse.Namespace) -> int:
    return w.run("watchlist update-item", args, w.plan_watchlist_update_item)


def cmd_ic_watchlist_create(args: argparse.Namespace) -> int:
    return w.run("watchlist create", args, w.plan_watchlist_create)


def cmd_ic_event_add(args: argparse.Namespace) -> int:
    return w.run("event add", args, w.plan_event_add)


def cmd_ic_event_update(args: argparse.Namespace) -> int:
    return w.run("event update", args, w.plan_event_update)


def cmd_ic_event_remove(args: argparse.Namespace) -> int:
    return w.run("event remove", args, w.plan_event_remove)


def cmd_ic_trade_log(args: argparse.Namespace) -> int:
    return w.run("trade log", args, w.plan_trade_log)


def cmd_ic_trigger_add(args: argparse.Namespace) -> int:
    return w.run("trigger add", args, w.plan_trigger_add)


def cmd_ic_trigger_update(args: argparse.Namespace) -> int:
    return w.run("trigger update", args, w.plan_trigger_update)


def cmd_ic_trigger_retire(args: argparse.Namespace) -> int:
    return w.run("trigger retire", args, w.plan_trigger_retire)


def cmd_ic_lesson_add(args: argparse.Namespace) -> int:
    return w.run("lesson add", args, w.plan_lesson_add)


def cmd_ic_ratio_add(args: argparse.Namespace) -> int:
    return w.run("ratio add", args, w.plan_ratio_add)


def cmd_ic_revert(args: argparse.Namespace) -> int:
    return w.run_revert(args)


def cmd_ic_writes(args: argparse.Namespace) -> int:
    return w.run_writes(args)


# Names inside the "ic" group; a two-word name becomes a nested subcommand (`hub ic alert add`).
COMMANDS: dict[str, tuple[str, Handler]] = {
    "alert add": ("create an IC alert", cmd_ic_alert_add),
    "alert modify": ("change an IC alert (deactivating needs --yes)", cmd_ic_alert_modify),
    "alert remove": ("delete an IC alert (needs --yes)", cmd_ic_alert_remove),
    "watchlist add-item": ("add a symbol to an IC watchlist", cmd_ic_watchlist_add_item),
    "watchlist update-item": ("change a watchlist item", cmd_ic_watchlist_update_item),
    "watchlist create": ("create an IC watchlist", cmd_ic_watchlist_create),
    "event add": ("add a calendar event", cmd_ic_event_add),
    "event update": ("change a calendar event", cmd_ic_event_update),
    "event remove": ("delete a calendar event", cmd_ic_event_remove),
    "trade log": ("log a trade in IC (needs --yes)", cmd_ic_trade_log),
    "trigger add": ("add a trigger-playbook standing order", cmd_ic_trigger_add),
    "trigger update": ("change a standing order", cmd_ic_trigger_update),
    "trigger retire": ("retire a standing order", cmd_ic_trigger_retire),
    "lesson add": ("record a trade lesson", cmd_ic_lesson_add),
    "ratio add": ("add a ratio", cmd_ic_ratio_add),
    "writes": ("list recent writes made to IC", cmd_ic_writes),
    "revert": ("undo one logged IC write", cmd_ic_revert),
}

ARGUMENTS: dict[str, Adder] = {
    "ic alert add": _alert_add_args,
    "ic alert modify": _alert_modify_args,
    "ic alert remove": _alert_remove_args,
    "ic watchlist add-item": _watchlist_add_item_args,
    "ic watchlist update-item": _watchlist_update_item_args,
    "ic watchlist create": _watchlist_create_args,
    "ic event add": _event_add_args,
    "ic event update": _event_update_args,
    "ic event remove": _event_remove_args,
    "ic trade log": _trade_log_args,
    "ic trigger add": _trigger_add_args,
    "ic trigger update": _trigger_update_args,
    "ic trigger retire": _trigger_retire_args,
    "ic lesson add": _lesson_add_args,
    "ic ratio add": _ratio_add_args,
    "ic writes": _writes_args,
    "ic revert": _revert_args,
}
