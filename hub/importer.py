"""Import a claude.ai export directory as revision-1 documents."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hub import store

# Files IC serves as contract documents; the importer leaves them alone.
SKIPPED = ("knowledge/advisor-actions.md", "knowledge/handoff-schema.md")

GENERIC_KIND = "document"
_TOP_LEVEL_KINDS = {
    "instructions.md": "instructions",
    "memory.md": "memory",
    "PROJECT.md": "project",
    "conversations.md": "conversations",
}


class ImportError_(Exception):
    """The export directory cannot be read."""


@dataclass(frozen=True)
class Candidate:
    source_ref: str
    slug: str
    kind: str
    body: str


def kind_for(source_ref: str) -> str:
    """Document kind from the path relative to the export root; unknown names are generic."""
    if source_ref in _TOP_LEVEL_KINDS:
        return _TOP_LEVEL_KINDS[source_ref]
    if source_ref.startswith("knowledge/"):
        return "knowledge"
    return GENERIC_KIND


def slug_for(source_ref: str) -> str:
    """Document identity: the relative path without its `.md` suffix."""
    return source_ref.removesuffix(".md")


def scan(root: Path) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Return the files to import and the skipped (relative path, reason) pairs, in path order."""
    if not root.is_dir():
        raise ImportError_(f"not a directory: {root}")
    real_root = root.resolve()
    files = sorted(p for p in root.glob("*.md") if p.is_file())
    skipped: list[tuple[str, str]] = []
    knowledge = root / "knowledge"
    if knowledge.is_dir():
        for entry in sorted(knowledge.iterdir()):
            if entry.suffix == ".md" and entry.is_file():
                files.append(entry)
            else:
                skipped.append(
                    (entry.relative_to(root).as_posix(), "not a top-level .md file in knowledge/")
                )
    found: list[Candidate] = []
    for path in files:
        ref = path.relative_to(root).as_posix()
        if ref in SKIPPED:
            skipped.append((ref, "served by Investing Companion"))
            continue
        if not path.resolve().is_relative_to(real_root):
            skipped.append((ref, "symlink resolves outside the export directory"))
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ImportError_(f"cannot read {ref}: {exc}") from exc
        found.append(Candidate(ref, slug_for(ref), kind_for(ref), body))
    return found, skipped


def _latest_body(conn: sqlite3.Connection, slug: str) -> str | None:
    row = conn.execute(
        "SELECT r.body FROM documents d JOIN document_revisions r ON r.document_id = d.id"
        " WHERE d.slug = ? ORDER BY r.revision DESC LIMIT 1",
        (slug,),
    ).fetchone()
    return None if row is None else row[0]


def plan(
    conn: sqlite3.Connection | None, candidates: list[Candidate]
) -> list[tuple[str, Candidate]]:
    """Label each candidate `new`, `unchanged` or `changed` against the store (None = empty)."""
    out = []
    for cand in candidates:
        existing = None if conn is None else _latest_body(conn, cand.slug)
        if existing is None:
            state = "new"
        elif existing == cand.body:
            state = "unchanged"
        else:
            state = "changed"
        out.append((state, cand))
    return out


def write_new(conn: sqlite3.Connection, planned: list[tuple[str, Candidate]]) -> int:
    """Insert revision 1 for every `new` candidate; return how many were written."""
    written = 0
    for state, cand in planned:
        if state != "new":
            continue
        store.insert_document_revision(
            conn,
            slug=cand.slug,
            kind=cand.kind,
            body=cand.body,
            source_kind="import",
            source_ref=cand.source_ref,
        )
        written += 1
    return written
