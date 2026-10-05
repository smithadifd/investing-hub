#!/usr/bin/env python3
"""Stub of week-ahead's reference reader — synthetic, for adapter tests only.

Stands in for the real ``scripts/read_beats.py`` (never invoked from tests)
inside a week-ahead fixture root: the same ``--json`` consumer surface the
beats-schema contract defines — ``beats`` carrying ``name`` and
``weight_raw``, ``warnings``, exit 0 on a read, 3 when the file is absent and
4 when no beat parsed. A deliberately small parser: it exists to prove the hub
consumes the reader's output across the repo boundary, not to be the reader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_ABSENT, EXIT_MALFORMED = 0, 3, 4
WEIGHTS = ("lead", "standing", "watch", "skip")
_KNOWN_LABELS = ("Weight", "What I care about", "What would make it lead")
_FIELD = re.compile(r"^([A-Za-z][A-Za-z' ]{0,40}):\s*(.*)$")


def main() -> int:
    ap = argparse.ArgumentParser(description="stub read_beats.py for hub adapter tests")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--path", default=None)
    args = ap.parse_args()

    path = Path(args.path) if args.path else Path(__file__).resolve().parent.parent / "beats.md"
    if not path.is_file():
        print(f"beats: ABSENT — looked in: {path}", file=sys.stderr)
        return EXIT_ABSENT
    text = path.read_text(encoding="utf-8")
    beats, warnings = _parse(text)
    if not beats:
        print(f"beats: MALFORMED — no beat parsed from {path}", file=sys.stderr)
        return EXIT_MALFORMED
    if args.json:
        print(
            json.dumps(
                {
                    "schema": "beats/1",
                    "source": str(path),
                    "revision": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
                    "weights": list(WEIGHTS),
                    "beats": beats,
                    "duplicate_beat_names": _duplicates(beats),
                    "warnings": warnings,
                }
            )
        )
    elif not args.quiet:
        print(f"{path} ({len(beats)} beats)")
    return EXIT_OK


def _parse(text: str) -> tuple[list[dict], list[str]]:
    """``### <name>`` blocks under ``## Beats``, each carrying its fields."""
    beats: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    in_beats = False
    name: str | None = None
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            _emit(beats, warnings, seen, name, fields)
            in_beats = line.strip().lower() == "## beats"
            name, fields = None, {}
            continue
        if not in_beats:
            continue
        if line.startswith("### ") and not line.startswith("####"):
            _emit(beats, warnings, seen, name, fields)
            name, fields = line[4:].strip(), {}
            continue
        if name is None:
            continue
        match = _FIELD.match(line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    _emit(beats, warnings, seen, name, fields)
    return beats, warnings


def _emit(
    beats: list[dict], warnings: list[str], seen: set[str], name: str | None, fields: dict[str, str]
) -> None:
    """Close one block: a beat when anything recognisable sat under it, else a warning."""
    if name is None:
        return
    if not any(fields.get(label) for label in _KNOWN_LABELS):
        warnings.append(f"beat '{name}' — no recognisable field under it; block skipped")
        return
    key = name.strip().lower()
    if key in seen:
        warnings.append(
            f"beat '{name}' — duplicate name: an earlier beat already answers to it."
            " Both kept, neither wins."
        )
    seen.add(key)
    raw_weight = fields.get("Weight", "")
    weight = raw_weight.lower() if raw_weight.lower() in WEIGHTS else None
    if raw_weight and weight is None:
        warnings.append(f"beat '{name}' — weight '{raw_weight}' is out of vocabulary")
    beats.append(
        {
            "name": name,
            "order": len(beats),
            "weight": weight,
            "weight_raw": raw_weight,
            "cares_about": fields.get("What I care about", ""),
            "promotes": fields.get("What would make it lead", ""),
        }
    )


def _duplicates(beats: list[dict]) -> list[str]:
    """First-seen duplicate beat names — a collision is published, not resolved."""
    seen: set[str] = set()
    out: list[str] = []
    for beat in beats:
        key = str(beat.get("name", "")).strip().lower()
        if key in seen and key not in out:
            out.append(key)
        seen.add(key)
    return out


if __name__ == "__main__":
    sys.exit(main())
