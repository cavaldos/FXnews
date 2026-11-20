"""Data import / export API (the "Data" page). Logic lives in app/services/datastore.py."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.config import get_settings
from app.services import datastore
from app.services.datastore import DataError

router = APIRouter(prefix="/api/data", tags=["data"])


def _http(exc: DataError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/tables")
def tables():
    try:
        return datastore.overview()
    except DataError as exc:
        raise _http(exc) from exc


@router.get("/export")
def export(
    format: Literal["sqlite", "json"] = "sqlite",
    tables: str | None = Query(None, description="comma separated table names (json only); default all"),
    frm: str | None = Query(None, alias="from", description="YYYY-MM-DD or ISO datetime (json only)"),
    to: str | None = Query(None, description="YYYY-MM-DD or ISO datetime, a date-only value includes that day (json only)"),
):
    try:
        if format == "sqlite":
            fd, name = tempfile.mkstemp(prefix="fxnews-export-", suffix=".db")
            os.close(fd)
            tmp = Path(name)
            try:
                datastore.export_sqlite(tmp)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return FileResponse(tmp, media_type="application/vnd.sqlite3", filename=datastore.export_filename("db"),
                                headers={"Cache-Control": "no-store"}, background=BackgroundTask(tmp.unlink, missing_ok=True))
        names = datastore.parse_tables(tables)
        gen = datastore.iter_json_export(names, datastore.parse_bound(frm), datastore.parse_bound(to, end=True))
        filename = datastore.export_filename("json.gz")
        return StreamingResponse(gen, media_type="application/gzip",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})
    except DataError as exc:
        raise _http(exc) from exc


@router.post("/import")
async def import_data(
    request: Request,
    mode: Literal["merge", "replace"] = "merge",
    tables: str | None = Query(None, description="comma separated table names; default all tables of the file"),
    filename: str | None = Query(None, max_length=255),
):
    """The file is the RAW request body (SQLite DB, JSON or JSON.gz; type detected from the content)."""
    limit = max(1, get_settings().data_import_max_mb) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"file is larger than the {get_settings().data_import_max_mb} MB limit (DATA_IMPORT_MAX_MB)")
    try:
        names = datastore.parse_tables(tables)
    except DataError as exc:
        raise _http(exc) from exc
    fd, name = tempfile.mkstemp(prefix="fxnews-import-")
    tmp = Path(name)
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"file is larger than the {get_settings().data_import_max_mb} MB limit (DATA_IMPORT_MAX_MB)")
                await run_in_threadpool(f.write, chunk)
        if size == 0:
            raise HTTPException(422, "empty request body: send the file as the raw body")
        try:
            return await run_in_threadpool(lambda: datastore.import_file(tmp, mode=mode, tables=names, filename=filename))
        except DataError as exc:
            raise _http(exc) from exc
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/backups")
def backups():
    try:
        return datastore.list_backups()
    except DataError as exc:
        raise _http(exc) from exc


@router.post("/backups")
async def make_backup():
    try:
        return await run_in_threadpool(datastore.create_backup, "manual")
    except DataError as exc:
        raise _http(exc) from exc


@router.get("/backups/{name}")
def download_backup(name: str):
    try:
        path = datastore.backup_path(name)
    except DataError as exc:
        raise _http(exc) from exc
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name, headers={"Cache-Control": "no-store"})


@router.post("/backups/{name}/restore")
async def restore_backup(name: str):
    try:
        return await run_in_threadpool(datastore.restore_backup, name)
    except DataError as exc:
        raise _http(exc) from exc
