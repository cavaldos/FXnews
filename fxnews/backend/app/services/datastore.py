"""Export / import of the SQLite database (the "Data" page, ``/api/data`` and the ``datastore`` CLI).

Formats
  sqlite   a consistent snapshot of the whole DB file (``sqlite3.Connection.backup``; safe with WAL).
  json     ``fxnews-*.json.gz``: ``{"format": "fxnews-export", "version": 1, "exported_at", "schema", "tables"}``;
           optional date range, table selection; streamed (never fully in memory).

Import (``merge`` | ``replace``) reads either format (detected by magic bytes), always snapshots the live DB into
the backups folder first, and runs in ONE transaction (any error rolls back).
  merge    upsert by natural keys, ids of foreign keys are remapped; the newer row wins.
  replace  delete the selected tables, insert the imported rows keeping their ids.

CLI:  python -m app.services.datastore export|import|backup|backups|restore ...
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, Table, and_, func, insert, select, update
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import Base, engine, init_db

log = logging.getLogger(__name__)

FORMAT_NAME = "fxnews-export"
FORMAT_VERSION = 1
SQLITE_MAGIC = b"SQLite format 3\x00"
GZIP_MAGIC = b"\x1f\x8b"
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.db$")
CHUNK = 1024 * 1024
INSERT_BATCH = 500

# One import / restore / backup at a time (they all take SQLite's write lock for a while).
_LOCK = threading.RLock()


class DataError(Exception):
    """A problem with the request or the file; ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ── table catalogue ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableSpec:
    name: str
    label: str
    time_col: str | None = None  # column the date range filters on (children follow their parent's range)
    refs: dict[str, str] = field(default_factory=dict)  # foreign key column -> parent table (remapped on merge)
    soft_refs: dict[str, str] = field(default_factory=dict)  # id columns without a DB foreign key, also remapped
    key_cols: tuple[str, ...] = ()  # natural key (merge)
    ts_col: str | None = None  # "newer wins" column (merge); None = existing rows are never updated
    parent_filter: tuple[str, str, str] | None = None  # (fk column, parent table, parent time col) for date range


SPECS: tuple[TableSpec, ...] = (  # parents first
    TableSpec("news_items", "News & calendar items", time_col="event_time", soft_refs={"analysis_id": "analyses"},
              key_cols=("dedupe_key",), ts_col="updated_at"),
    TableSpec("analyses", "LLM analyses", time_col="created_at", refs={"news_id": "news_items"},
              key_cols=("kind", "news_id", "model", "created_at")),
    TableSpec("signals", "Trade signals", time_col="created_at", refs={"analysis_id": "analyses"},
              soft_refs={"news_id": "news_items"}, key_cols=("analysis_id", "symbol", "created_at")),
    TableSpec("bt_events", "Backtest events", time_col="time", key_cols=("dedupe_key",), ts_col="updated_at"),
    TableSpec("bt_outcomes", "Backtest outcomes", refs={"event_id": "bt_events"}, key_cols=("event_id", "symbol"),
              ts_col="computed_at", parent_filter=("event_id", "bt_events", "time")),
    TableSpec("bt_predictions", "Backtest predictions", refs={"event_id": "bt_events"},
              key_cols=("event_id", "symbol", "model"), ts_col="created_at",
              parent_filter=("event_id", "bt_events", "time")),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}
ORDER = [s.name for s in SPECS]
# Deleting a table's rows also removes / unlinks these (FK cascade / SET NULL): replace them together.
DEPENDENTS: dict[str, list[str]] = {
    "news_items": ["analyses", "signals"],
    "analyses": ["signals"],
    "bt_events": ["bt_outcomes", "bt_predictions"],
}


def _table(name: str) -> Table:
    from app import models  # noqa: F401  (register tables)
    from app.backtest import models as _bt  # noqa: F401

    return Base.metadata.tables[name]


def parse_tables(value: str | list[str] | None) -> list[str] | None:
    """``"a,b"`` -> validated, ordered list; None / empty -> None (= all)."""
    if value is None:
        return None
    names = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if v and v.strip()]
    if not names:
        return None
    bad = [n for n in names if n not in SPEC_BY_NAME]
    if bad:
        raise DataError(422, f"unknown table(s): {', '.join(bad)}. Known: {', '.join(ORDER)}")
    return [n for n in ORDER if n in set(names)]


def parse_bound(value: str | None, *, end: bool = False) -> datetime | None:
    """``2026-11-01`` or an ISO datetime (UTC when no offset). A date-only END includes that whole day."""
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime.combine(d + timedelta(days=1) if end else d, time.min, tzinfo=UTC)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataError(422, f"invalid date {value!r}: use YYYY-MM-DD or an ISO datetime") from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ── paths / backups ────────────────────────────────────────────────────────────────────────────────────


def db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise DataError(501, "data import/export only supports a SQLite database")
    return Path(url.removeprefix("sqlite:///")).resolve()


def backup_dir() -> Path:
    configured = get_settings().data_backup_dir.strip()
    base = Path(configured).expanduser() if configured else db_path().parent / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def backup_path(name: str) -> Path:
    """Resolve a backup file name; rejects anything that is not a plain ``*.db`` name inside the backups folder."""
    if not BACKUP_NAME_RE.fullmatch(name or "") or ".." in name:
        raise DataError(400, "invalid backup name")
    root = backup_dir()
    path = (root / name).resolve()
    if path.parent != root:
        raise DataError(400, "invalid backup name")
    if not path.is_file():
        raise DataError(404, f"backup {name!r} not found")
    return path


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def snapshot_to(dest: Path) -> None:
    """Consistent copy of the live DB into ``dest`` (works while the app writes; handles WAL)."""
    src = sqlite3.connect(str(db_path()), timeout=30)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_backup(label: str = "") -> dict[str, Any]:
    """Snapshot the live DB into the backups folder and prune old ones (``data_backup_keep``)."""
    with _LOCK:
        root = backup_dir()
        safe = re.sub(r"[^A-Za-z0-9_-]", "", label)[:30]
        base = f"fxnews-{_stamp()}" + (f"-{safe}" if safe else "")
        path = root / f"{base}.db"
        n = 1
        while path.exists():
            n += 1
            path = root / f"{base}-{n}.db"
        tmp = path.with_suffix(".db.part")
        try:
            snapshot_to(tmp)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        prune_backups()
        return _backup_info(path)


def _backup_info(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"name": path.name, "size_bytes": st.st_size, "created_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat()}


def list_backups() -> list[dict[str, Any]]:
    files = [p for p in backup_dir().glob("*.db") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return [_backup_info(p) for p in files]


def prune_backups() -> None:
    keep = max(1, get_settings().data_backup_keep)
    for info in list_backups()[keep:]:
        with contextlib.suppress(OSError):
            (backup_dir() / info["name"]).unlink()


def restore_backup(name: str) -> dict[str, Any]:
    """Replace the whole live DB with a backup (the current state is snapshotted first). Works while the app runs."""
    with _LOCK:
        path = backup_path(name)
        _require_sqlite_file(path)
        safety = create_backup("pre-restore")
        engine.dispose()  # drop pooled connections so none of them holds a stale page cache
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(db_path()), timeout=30)
            try:
                src.backup(dst)  # page-level copy through the live DB connection; WAL stays consistent
            finally:
                dst.close()
        finally:
            src.close()
        engine.dispose()
        init_db()  # a backup from an older version may miss newer tables
        return {"restored": name, "backup": safety["name"]}


def _require_sqlite_file(path: Path) -> None:
    with path.open("rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise DataError(422, f"{path.name} is not a SQLite database")


# ── overview ───────────────────────────────────────────────────────────────────────────────────────────


def overview() -> dict[str, Any]:
    path = db_path()
    size = path.stat().st_size if path.exists() else 0
    wal = Path(f"{path}-wal")
    if wal.exists():
        size += wal.stat().st_size
    tables = []
    with engine.connect() as conn:
        for spec in SPECS:
            n = conn.execute(select(func.count()).select_from(_table(spec.name))).scalar_one()
            tables.append({"name": spec.name, "rows": n, "label": spec.label})
    return {"tables": tables, "db_path": str(path), "size_bytes": size, "backup_dir": str(backup_dir())}


# ── export ─────────────────────────────────────────────────────────────────────────────────────────────


def export_filename(ext: str) -> str:
    return f"fxnews-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.{ext}"


def _range_clause(spec: TableSpec, frm: datetime | None, to: datetime | None):
    """WHERE clause for the date range; children (outcomes, predictions) follow their event's time."""
    if frm is None and to is None:
        return None
    table = _table(spec.name)
    if spec.parent_filter:
        fk, parent, tcol = spec.parent_filter
        ptable = _table(parent)
        conds = []
        if frm is not None:
            conds.append(ptable.c[tcol] >= frm)
        if to is not None:
            conds.append(ptable.c[tcol] < to)
        return table.c[fk].in_(select(ptable.c.id).where(and_(*conds)))
    if spec.time_col is None:
        return None
    col = table.c[spec.time_col]
    conds = []
    if frm is not None:
        conds.append(col >= frm)
    if to is not None:
        conds.append(col < to)
    return and_(*conds)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return (o.replace(tzinfo=UTC) if o.tzinfo is None else o.astimezone(UTC)).isoformat()
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


def iter_json_export(tables: list[str] | None = None, frm: datetime | None = None,
                     to: datetime | None = None) -> Iterator[bytes]:
    """Yield the gzip-compressed JSON export in chunks; rows are read lazily, nothing is held in memory."""
    names = tables or list(ORDER)
    buf = _Sink()
    gz = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0)

    def write(s: str) -> Iterator[bytes]:
        gz.write(s.encode("utf-8"))
        if buf.size() >= 64 * 1024:
            yield buf.take()

    schema = {n: [c.name for c in _table(n).columns] for n in names}
    with engine.connect() as conn:
        counts = {}
        for n in names:
            q = select(func.count()).select_from(_table(n))
            cl = _range_clause(SPEC_BY_NAME[n], frm, to)
            counts[n] = conn.execute(q.where(cl) if cl is not None else q).scalar_one()
        head = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "exported_at": datetime.now(UTC).isoformat(),
                "range": {"from": frm.isoformat() if frm else None, "to": to.isoformat() if to else None},
                "counts": counts, "schema": schema}
        text = json.dumps(head, ensure_ascii=False)
        yield from write(text[:-1] + ', "tables": {')
        for i, n in enumerate(names):
            table = _table(n)
            q = select(table).order_by(table.c.id)
            cl = _range_clause(SPEC_BY_NAME[n], frm, to)
            if cl is not None:
                q = q.where(cl)
            yield from write(("," if i else "") + json.dumps(n) + ": [")
            first = True
            for row in conn.execute(q.execution_options(yield_per=1000)):
                piece = json.dumps(dict(row._mapping), ensure_ascii=False, default=_json_default)
                yield from write(piece if first else "," + piece)
                first = False
            yield from write("]")
        yield from write("}}")
    gz.close()
    tail = buf.take()
    if tail:
        yield tail


class _Sink:
    """Write-only buffer the gzip writer fills; ``take()`` drains it."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._size = 0

    def write(self, b: bytes) -> int:
        self._parts.append(bytes(b))
        self._size += len(b)
        return len(b)

    def flush(self) -> None:  # GzipFile calls it
        pass

    def size(self) -> int:
        return self._size

    def take(self) -> bytes:
        out = b"".join(self._parts)
        self._parts, self._size = [], 0
        return out


def export_sqlite(dest: Path) -> None:
    snapshot_to(dest)


# ── import: sources ────────────────────────────────────────────────────────────────────────────────────


class _Source:
    kind = ""
    text_json = False  # JSON columns arrive as text (SQLite) instead of decoded values

    def tables(self) -> set[str]:
        raise NotImplementedError

    def columns(self, name: str) -> list[str]:
        raise NotImplementedError

    def rows(self, name: str) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _SqliteSource(_Source):
    kind = "sqlite"
    text_json = True

    def __init__(self, path: Path) -> None:
        self.conn = self._open(path)
        self.conn.row_factory = sqlite3.Row
        self._tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        last: Exception | None = None
        for q in ("mode=ro", "immutable=1"):
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?{q}", uri=True)
            try:
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                return conn
            except sqlite3.DatabaseError as exc:
                last = exc
                conn.close()
        raise DataError(422, f"cannot read the SQLite file: {last}")

    def tables(self) -> set[str]:
        return self._tables

    def columns(self, name: str) -> list[str]:
        return [r[1] for r in self.conn.execute(f'PRAGMA table_info("{name}")')]

    def rows(self, name: str) -> Iterator[dict[str, Any]]:
        for r in self.conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            yield dict(r)

    def close(self) -> None:
        self.conn.close()


class _JsonSource(_Source):
    kind = "json"

    def __init__(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
            raise DataError(422, f"not an FXNews export (missing \"format\": \"{FORMAT_NAME}\")")
        if int(data.get("version", 0) or 0) > FORMAT_VERSION:
            raise DataError(422, f"export version {data.get('version')} is newer than this server supports ({FORMAT_VERSION})")
        tables = data.get("tables")
        if not isinstance(tables, dict):
            raise DataError(422, "export has no \"tables\" object")
        self.data = tables
        self.schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
        for name, rows in tables.items():
            if not isinstance(rows, list):
                raise DataError(422, f"table {name!r} must be a list of rows")

    def tables(self) -> set[str]:
        return set(self.data)

    def columns(self, name: str) -> list[str]:
        cols = self.schema.get(name)
        if isinstance(cols, list) and cols:
            return [str(c) for c in cols]
        keys: dict[str, None] = {}
        for row in self.data[name][:50]:
            if isinstance(row, dict):
                keys.update(dict.fromkeys(row))
        return list(keys)

    def rows(self, name: str) -> Iterator[dict[str, Any]]:
        for row in self.data[name]:
            if isinstance(row, dict):
                yield row


def detect_kind(path: Path) -> str:
    if not path.is_file():
        raise DataError(404, f"file not found: {path}")
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(SQLITE_MAGIC):
        return "sqlite"
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    if head.lstrip()[:1] in (b"{", b"\xef"):  # '{' or a UTF-8 BOM
        return "json"
    raise DataError(422, "unrecognised file: expected a SQLite database, a .json or a .json.gz export")


def _gunzip(path: Path, dest: Path, limit: int) -> None:
    total = 0
    try:
        with gzip.open(path, "rb") as src, dest.open("wb") as out:
            while chunk := src.read(CHUNK):
                total += len(chunk)
                if total > limit:
                    raise DataError(413, "decompressed file is too large")
                out.write(chunk)
    except (OSError, EOFError) as exc:
        raise DataError(422, f"corrupt gzip file: {exc}") from exc


def _open_source(path: Path, tmp_files: list[Path]) -> _Source:
    kind = detect_kind(path)
    if kind == "gzip":
        limit = max(1, get_settings().data_import_max_mb) * 1024 * 1024 * 10
        fd, name = tempfile.mkstemp(prefix="fxnews-gunzip-")
        os.close(fd)
        inner = Path(name)
        tmp_files.append(inner)
        _gunzip(path, inner, limit)
        path, kind = inner, detect_kind(inner)
        if kind == "gzip":
            raise DataError(422, "nested gzip is not supported")
    if kind == "sqlite":
        return _SqliteSource(path)
    try:
        with path.open("rb") as f:
            data = json.load(f)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DataError(422, f"invalid JSON: {exc}") from exc
    return _JsonSource(data)


# ── import: value handling ─────────────────────────────────────────────────────────────────────────────


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"not a datetime: {v!r}")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _coerce(col, v: Any, text_json: bool) -> Any:
    if v is None:
        return None
    t = col.type
    if isinstance(t, DateTime) or isinstance(getattr(t, "impl", None), DateTime):
        return _parse_dt(v)
    if isinstance(t, JSON):
        if text_json and isinstance(v, (str, bytes)):
            return json.loads(v)
        return v
    if isinstance(t, Boolean):
        return bool(v)
    if isinstance(t, Integer):
        return int(v)
    if isinstance(t, Float):
        return float(v)
    return v if isinstance(v, str) else str(v)


def _default_for(col) -> Any:
    d = col.default
    if d is None:
        return None
    if getattr(d, "is_scalar", False):
        return d.arg
    if getattr(d, "is_callable", False):
        return d.arg(None)
    return None


def _required_cols(table: Table) -> list[str]:
    return [c.name for c in table.columns
            if not c.primary_key and not c.nullable and c.default is None and c.server_default is None]


class _Report:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, int]] = {}
        self.warnings: list[str] = []
        self._counts: dict[str, int] = {}
        self._messages: dict[str, str] = {}

    def table(self, name: str) -> dict[str, int]:
        return self.tables.setdefault(name, {"inserted": 0, "updated": 0, "skipped": 0})

    def note(self, key: str, message: str) -> None:
        """Count a recurring problem; one warning line per ``key`` is emitted at the end."""
        self._counts[key] = self._counts.get(key, 0) + 1
        self._messages.setdefault(key, message)

    def finish(self) -> list[str]:
        out = list(self.warnings)
        for key, n in self._counts.items():
            out.append(f"{self._messages[key]} ({n} row{'s' if n != 1 else ''})")
        return out


def _prepare_row(spec: TableSpec, raw: dict[str, Any], cols: list[str], text_json: bool, report: _Report) -> dict[str, Any] | None:
    """Coerce an imported row to column types; None (and a warning) when it cannot be used."""
    table = _table(spec.name)
    out: dict[str, Any] = {}
    for name in cols:
        col = table.c[name]
        try:
            v = _coerce(col, raw.get(name), text_json)
        except (ValueError, TypeError) as exc:
            report.note(f"bad:{spec.name}", f"{spec.name}: unreadable value skipped the row ({exc})")
            return None
        if v is None and not col.nullable and not col.primary_key:
            v = _default_for(col)
            if v is None:
                report.note(f"null:{spec.name}.{name}", f"{spec.name}.{name} is required but empty, row skipped")
                return None
        out[name] = v
    return out


# ── import: execution ──────────────────────────────────────────────────────────────────────────────────


def _plan(source: _Source, mode: str, tables: list[str] | None, report: _Report) -> list[str]:
    """Validate the file against the models; return the tables that will be written (in FK order)."""
    available = [n for n in ORDER if n in source.tables()]
    unknown = sorted(source.tables() - set(ORDER) - {"sqlite_sequence"})
    if unknown:
        report.warnings.append(f"ignored unknown table(s): {', '.join(unknown)}")
    if not available:
        raise DataError(422, "the file contains none of the FXNews tables")
    selected = [n for n in (tables or available) if n in available]
    for n in tables or []:
        if n not in available:
            report.warnings.append(f"table {n} is not in the file, skipped")
    if not selected:
        raise DataError(422, "none of the selected tables is in the file")
    if mode == "replace":
        for n in list(selected):
            for dep in DEPENDENTS.get(n, []):
                if dep in available and dep not in selected:
                    selected.append(dep)
                    report.warnings.append(f"{dep} is replaced together with {n} (its rows reference {n})")
                elif dep not in available:
                    report.warnings.append(f"{dep} is not in the file: its rows referencing {n} are unlinked or deleted")
        selected = [n for n in ORDER if n in selected]
    for n in selected:
        table = _table(n)
        cols = source.columns(n)
        extra = [c for c in cols if c not in table.c]
        if extra:
            report.warnings.append(f"{n}: ignored unknown column(s) {', '.join(extra)}")
        missing = [c for c in _required_cols(table) if c not in cols]
        if mode == "replace" and "id" not in cols:
            missing.append("id")
        if missing:
            raise DataError(422, f"table {n}: required column(s) missing from the file: {', '.join(missing)}")
    return selected


def _known_cols(source: _Source, name: str) -> list[str]:
    table = _table(name)
    return [c for c in source.columns(name) if c in table.c]


def _merge(conn: Connection, source: _Source, selected: list[str], report: _Report) -> None:
    available = source.tables()
    need = set(selected)
    grew = True
    while grew:  # parents of selected tables are read (without writing) to translate ids
        grew = False
        for n in list(need):
            for parent in (*SPEC_BY_NAME[n].refs.values(), *SPEC_BY_NAME[n].soft_refs.values()):
                if parent not in need and parent in available:
                    need.add(parent)
                    grew = True
    maps: dict[str, dict[int, int]] = {}
    pending_soft: list[tuple[str, int, str, Any]] = []  # (table, new id, column, old parent id)
    for name in [n for n in ORDER if n in need]:
        spec = SPEC_BY_NAME[name]
        table = _table(name)
        write = name in selected
        cols = _known_cols(source, name)
        rep = report.table(name) if write else None
        existing = _load_existing(conn, spec)
        tmap = maps.setdefault(name, {})
        for raw in source.rows(name):
            row = _prepare_row(spec, raw, cols, source.text_json, report)
            if row is None:
                if rep:
                    rep["skipped"] += 1
                continue
            old_id = row.pop("id", None)
            unresolvable = False
            for col, parent in spec.refs.items():
                old = row.get(col)
                if old is None:
                    continue
                new = maps.get(parent, {}).get(old)
                if new is None:
                    if table.c[col].nullable:
                        row[col] = None
                        if write:
                            report.note(f"ref:{name}.{col}", f"{name}.{col}: parent {parent} not found, link cleared")
                    else:
                        unresolvable = True
                        if write:
                            report.note(f"orphan:{name}.{col}", f"{name}: parent {parent} not found, row skipped")
                        break
                else:
                    row[col] = new
            if unresolvable:
                if rep:
                    rep["skipped"] += 1
                continue
            soft: dict[str, Any] = {}  # references to tables processed later: translated after the loop
            for col, parent in spec.soft_refs.items():
                if col not in row:
                    continue
                old = row.pop(col)
                if ORDER.index(parent) < ORDER.index(name):
                    new = maps.get(parent, {}).get(old) if old is not None else None
                    if old is not None and new is None and write:
                        report.note(f"soft:{name}.{col}", f"{name}.{col}: referenced {parent} row not found, link left empty")
                    row[col] = new
                else:
                    soft[col] = old
            key = tuple(row.get(c) for c in spec.key_cols)
            hit = existing.get(key)
            if hit is not None:
                if old_id is not None:
                    tmap[old_id] = hit[0]
                newer = spec.ts_col is not None and row.get(spec.ts_col) is not None and (hit[1] is None or row[spec.ts_col] > hit[1])
                if write and newer:
                    conn.execute(update(table).where(table.c.id == hit[0]).values(**row))
                    existing[key] = (hit[0], row[spec.ts_col])
                    rep["updated"] += 1
                    new_id, touched = hit[0], True
                else:
                    if rep:
                        rep["skipped"] += 1
                    new_id, touched = hit[0], False
            elif write:
                new_id = conn.execute(insert(table).values(**row)).inserted_primary_key[0]
                existing[key] = (new_id, row.get(spec.ts_col) if spec.ts_col else None)
                if old_id is not None:
                    tmap[old_id] = new_id
                rep["inserted"] += 1
                touched = True
            else:
                continue
            if touched:
                for col, old in soft.items():
                    if old is not None:
                        pending_soft.append((name, new_id, col, old))
    # Soft references (news_items.analysis_id) point to a table that is processed after this one.
    for tname, rid, col, old in pending_soft:
        parent = SPEC_BY_NAME[tname].soft_refs[col]
        new = maps.get(parent, {}).get(old)
        if new is None:
            report.note(f"soft:{tname}.{col}", f"{tname}.{col}: referenced {parent} row not found, link left empty")
            continue
        t = _table(tname)
        conn.execute(update(t).where(t.c.id == rid).values(**{col: new}))


def _load_existing(conn: Connection, spec: TableSpec) -> dict[tuple, tuple[int, Any]]:
    table = _table(spec.name)
    cols = [table.c.id, *(table.c[c] for c in spec.key_cols)]
    if spec.ts_col:
        cols.append(table.c[spec.ts_col])
    n = len(spec.key_cols)
    out: dict[tuple, tuple[int, Any]] = {}
    for r in conn.execute(select(*cols)):
        out[tuple(r[1:1 + n])] = (r[0], r[1 + n] if spec.ts_col else None)
    return out


def _replace(conn: Connection, source: _Source, selected: list[str], report: _Report) -> None:
    for name in reversed(selected):  # children first
        table = _table(name)
        n = conn.execute(select(func.count()).select_from(table)).scalar_one()
        conn.execute(table.delete())
        report.table(name)["deleted"] = n  # type: ignore[assignment]
    ids: dict[str, set[int]] = {}

    def parent_ids(parent: str) -> set[int]:
        if parent not in ids:
            t = _table(parent)
            ids[parent] = {r[0] for r in conn.execute(select(t.c.id))}
        return ids[parent]

    for name in selected:
        spec = SPEC_BY_NAME[name]
        table = _table(name)
        cols = _known_cols(source, name)
        rep = report.table(name)
        batch: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in source.rows(name):
            row = _prepare_row(spec, raw, cols, source.text_json, report)
            if row is None or row.get("id") is None or row["id"] in seen:
                rep["skipped"] += 1
                continue
            bad = False
            for col, parent in spec.refs.items():
                v = row.get(col)
                if v is not None and v not in parent_ids(parent):
                    if table.c[col].nullable:
                        row[col] = None
                        report.note(f"ref:{name}.{col}", f"{name}.{col}: parent {parent} not found, link cleared")
                    else:
                        bad = True
                        report.note(f"orphan:{name}.{col}", f"{name}: parent {parent} not found, row skipped")
            if bad:
                rep["skipped"] += 1
                continue
            seen.add(row["id"])
            batch.append(row)
            if len(batch) >= INSERT_BATCH:
                conn.execute(insert(table), batch)
                rep["inserted"] += len(batch)
                batch = []
        if batch:
            conn.execute(insert(table), batch)
            rep["inserted"] += len(batch)
        ids[name] = seen


def import_file(path: Path, *, mode: str = "merge", tables: list[str] | None = None,
                filename: str | None = None) -> dict[str, Any]:
    """Import a SQLite / JSON / JSON.gz file. Takes a backup first; one transaction; raises DataError on bad input."""
    if mode not in ("merge", "replace"):
        raise DataError(422, "mode must be 'merge' or 'replace'")
    with _LOCK:
        db_path()  # sqlite only
        init_db()
        tmp_files: list[Path] = []
        source = _open_source(path, tmp_files)
        try:
            report = _Report()
            selected = _plan(source, mode, tables, report)
            backup = create_backup("pre-import")
            try:
                with engine.begin() as conn:
                    (_replace if mode == "replace" else _merge)(conn, source, selected, report)
            except DataError:
                raise
            except Exception as exc:
                log.exception("import failed, rolled back")
                raise DataError(500, f"import failed and was rolled back: {exc}") from exc
            for n in selected:
                report.table(n)
            return {"mode": mode, "backup": backup["name"], "format": source.kind, "filename": filename,
                    "tables": {n: report.tables[n] for n in ORDER if n in report.tables}, "warnings": report.finish()}
        finally:
            source.close()
            for f in tmp_files:
                f.unlink(missing_ok=True)


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.services.datastore", description="FXNews data export / import")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write the DB to a .db (sqlite) or .json.gz file")
    e.add_argument("-o", "--output", help="target file (default: ./fxnews-<timestamp>.<ext>)")
    e.add_argument("--format", choices=["sqlite", "json"], default="sqlite")
    e.add_argument("--tables", help="comma separated (json only); default all")
    e.add_argument("--from", dest="frm", help="YYYY-MM-DD or ISO datetime (json only)")
    e.add_argument("--to", help="YYYY-MM-DD or ISO datetime (json only)")
    i = sub.add_parser("import", help="import a .db / .json / .json.gz file (a backup is taken first)")
    i.add_argument("file")
    i.add_argument("--mode", choices=["merge", "replace"], default="merge")
    i.add_argument("--tables", help="comma separated; default all tables of the file")
    b = sub.add_parser("backup", help="snapshot the DB into the backups folder")
    b.add_argument("--label", default="manual")
    sub.add_parser("backups", help="list backups")
    r = sub.add_parser("restore", help="replace the DB with a backup (the current DB is backed up first)")
    r.add_argument("name")
    args = p.parse_args(argv)
    try:
        init_db()
        if args.cmd == "export":
            if args.format == "sqlite":
                out = Path(args.output or export_filename("db"))
                export_sqlite(out)
            else:
                out = Path(args.output or export_filename("json.gz"))
                with out.open("wb") as f:
                    for chunk in iter_json_export(parse_tables(args.tables), parse_bound(args.frm), parse_bound(args.to, end=True)):
                        f.write(chunk)
            print(f"wrote {out} ({out.stat().st_size} bytes)")
        elif args.cmd == "import":
            res = import_file(Path(args.file), mode=args.mode, tables=parse_tables(args.tables), filename=Path(args.file).name)
            print(json.dumps(res, indent=2))
        elif args.cmd == "backup":
            print(json.dumps(create_backup(args.label), indent=2))
        elif args.cmd == "backups":
            print(json.dumps(list_backups(), indent=2))
        else:
            print(json.dumps(restore_backup(args.name), indent=2))
    except DataError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
