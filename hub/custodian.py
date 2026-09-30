"""Custodian CSV reader: header synonym matching, preamble skip and date discovery.

Column headers are matched against synonym sets rather than a fixed schema, so a typical
custodian export is recognised without configuration and anything else can be mapped by hand
(`--map header=field`). Lines above the header row (a title line or two) are skipped by
scanning the first few rows for the one that matches the most known headers. The transactions
synonym sets are transcribed from Investing Companion's broker-CSV importer.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

KINDS = ("positions", "transactions")

# How far into a file to look for the header row; further down is far more likely to be data.
MAX_HEADER_SCAN_ROWS = 10

# Field -> normalised header synonyms (see `normalize_header`).
TRANSACTION_HEADERS: dict[str, set[str]] = {
    "date": {
        "date",
        "tradedate",
        "transactiondate",
        "activitydate",
        "rundate",
        "executiondate",
        "occurredat",
    },
    "action": {
        "action",
        "type",
        "transactiontype",
        "side",
        "buysell",
        "buysellindicator",
        "activity",
        "activitytype",
    },
    "symbol": {"symbol", "ticker", "security", "securitysymbol", "instrument"},
    "quantity": {"quantity", "qty", "shares", "numberofshares", "sharequantity"},
    "price": {"price", "priceusd", "executionprice", "shareprice", "unitprice"},
    "fees": {"feescomm", "fees", "commission", "commissionfees", "feesandcomm"},
    "amount": {"amount", "netamount", "netcash", "total", "proceeds", "value"},
    "external_id": {
        "transactionid",
        "activityid",
        "referencenumber",
        "reference",
        "id",
        "confirmationnumber",
    },
}

POSITION_HEADERS: dict[str, set[str]] = {
    "symbol": {"symbol", "ticker", "security", "securitysymbol", "instrument"},
    "description": {"description", "name", "securityname", "securitydescription"},
    "quantity": {"quantity", "qty", "shares", "numberofshares", "sharequantity"},
    "price": {"price", "lastprice", "currentprice", "marketprice", "closeprice"},
    "market_value": {"marketvalue", "mktval", "mktvalue", "currentvalue", "value"},
    "cost_basis": {"costbasis", "costbasistotal", "totalcost", "cost"},
    "account_percent": {
        "percentofaccount",
        "ofaccount",
        "ofacct",
        "accountpercent",
        "percentofacct",
        "portfolioweight",
        "weight",
    },
    # No built-in synonyms: a positions file carries no date column unless one is mapped by hand.
    "date": set(),
}

SYNONYMS = {"transactions": TRANSACTION_HEADERS, "positions": POSITION_HEADERS}

# Fields a file must provide. Price, fees, amount and ids are optional.
REQUIRED = {
    "transactions": ("date", "action", "symbol", "quantity"),
    "positions": ("symbol", "quantity"),
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%d %b %Y",
)
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CustodianError(Exception):
    """The file or options cannot be turned into a snapshot; the message is for the operator."""


def normalize_header(value: str) -> str:
    """Lowercase and keep only letters and digits ("Fees & Comm" -> feesandcomm)."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def parse_as_of(text: str) -> str:
    """Validate a `YYYY-MM-DD` date and return it."""
    if not _ISO_DATE.match(text):
        raise CustodianError(f"invalid as-of date {text!r}; expected YYYY-MM-DD")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise CustodianError(f"invalid as-of date {text!r}; expected YYYY-MM-DD") from None
    return text


def parse_map_option(text: str) -> tuple[str, str]:
    """Split one `--map header=field` value."""
    header, sep, field_name = text.rpartition("=")
    if not sep or not header.strip() or not field_name.strip():
        raise CustodianError(f"invalid --map {text!r}; expected header=field")
    return header.strip(), field_name.strip()


def parse_date_cell(value: str) -> str | None:
    """A broker date cell as `YYYY-MM-DD`, or None.

    "08/11/2026 as of 08/10/2026" yields the first date, the way Investing Companion reads it.
    """
    text = value.strip()
    if " as of " in text.lower():
        text = text[: text.lower().index(" as of ")].strip()
    if not text:
        return None
    for fmt in (*_DATE_FORMATS, *_DATETIME_FORMATS):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def repo_root() -> Path:
    """The checkout this package lives in (`source_ref` is relative to it)."""
    return Path(__file__).resolve().parents[1]


def source_ref(path: Path, root: Path | None = None) -> str:
    """`path` relative to the repo root when inside it, else as given."""
    root = root or repo_root()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


@dataclass
class Parsed:
    kind: str
    header_row: int  # index of the header row among the file's rows
    mapping: dict[str, int]  # field -> column index
    headers: list[str]  # the header row as written
    rows: list[list[str]]  # non-blank data rows
    missing: list[str] = field(default_factory=list)  # required fields with no column
    dates: list[str] = field(default_factory=list)  # ISO dates found in the date column

    @property
    def newest_date(self) -> str | None:
        return max(self.dates) if self.dates else None

    def mapping_report(self) -> list[tuple[str, str]]:
        """(header as written, field) pairs in column order."""
        by_col = {col: name for name, col in self.mapping.items()}
        return [(self.headers[col], by_col[col]) for col in sorted(by_col)]


def _read_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def _map_row(
    kind: str, row: list[str], overrides: dict[str, str]
) -> tuple[dict[str, int], list[str]]:
    """Field -> column for one candidate header row, plus override headers it lacks."""
    norm = [normalize_header(cell) for cell in row]
    synonyms = SYNONYMS[kind]
    mapping: dict[str, int] = {}
    absent: list[str] = []
    for header, field_name in overrides.items():
        wanted = normalize_header(header)
        cols = [i for i, n in enumerate(norm) if n and n == wanted]
        if cols:
            mapping[field_name] = cols[0]
        else:
            absent.append(header)
    used = set(mapping.values())
    for index, n in enumerate(norm):
        if not n or index in used:
            continue
        for field_name, names in synonyms.items():
            if field_name not in mapping and n in names:
                mapping[field_name] = index
                break
    return mapping, absent


def parse(text: str, kind: str, overrides: dict[str, str] | None = None) -> Parsed:
    """Find the header row, map its columns and collect the data rows.

    `overrides` maps a header (as written, matched case/punctuation-insensitively) to a field
    name. Raises CustodianError when no row looks like a header, or an override names an unknown
    field or a header the file lacks. Missing required fields are reported in `Parsed.missing`.
    """
    if kind not in KINDS:
        raise CustodianError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    overrides = overrides or {}
    bad = sorted(set(overrides.values()) - set(SYNONYMS[kind]))
    if bad:
        raise CustodianError(
            f"unknown field(s) in --map: {', '.join(bad)}; "
            f"{kind} fields are {', '.join(SYNONYMS[kind])}"
        )
    rows = _read_rows(text)
    best: tuple[int, dict[str, int], list[str]] | None = None
    best_score = 0
    for index, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        mapping, _ = _map_row(kind, row, overrides)
        score = len(mapping)
        if score > best_score:
            best, best_score = (index, mapping, row), score
    if best is None:
        raise CustodianError(
            f"no header row found in the first {MAX_HEADER_SCAN_ROWS} rows"
            f" ({kind} need {', '.join(REQUIRED[kind])} columns)"
        )
    header_row, mapping, headers = best
    _, absent = _map_row(kind, headers, overrides)
    if absent:
        raise CustodianError(f"--map header not found in the file: {', '.join(absent)}")
    data = [r for r in rows[header_row + 1 :] if any(cell.strip() for cell in r)]
    missing = [f for f in REQUIRED[kind] if f not in mapping]
    dates: list[str] = []
    if "date" in mapping:
        col = mapping["date"]
        for r in data:
            found = parse_date_cell(r[col]) if col < len(r) else None
            if found:
                dates.append(found)
    return Parsed(kind, header_row, mapping, headers, data, missing, dates)


def count_rows(text: str, kind: str) -> int:
    """Data rows in a stored file, or 0 when no header can be found."""
    try:
        return len(parse(text, kind).rows)
    except CustodianError:
        return 0
