"""Google Drive behind the chart pipe. Same paths as Proton: /Clients/Last, First/Chart/…"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from app.infra.chart_store import ChartError, segments

_FOLDER = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_local = threading.local()
_cache: dict[tuple[str, str], str] = {}


def clear_cache() -> None:
    _cache.clear()
    if hasattr(_local, "svc"):
        del _local.svc


def _google_failure(exc: BaseException) -> ChartError | None:
    if isinstance(exc, ChartError):
        return None
    name = type(exc).__name__
    mod = type(exc).__module__
    if name in {
        "HttpError",
        "GoogleAuthError",
        "RefreshError",
        "TransportError",
        "MalformedError",
        "DefaultCredentialsError",
        "ServerNotFoundError",
    }:
        return ChartError("Google Drive is not reachable")
    if mod.startswith("google.auth") or mod.startswith("googleapiclient.errors") or mod.startswith("httplib2"):
        return ChartError("Google Drive is not reachable")
    if isinstance(exc, (OSError, json.JSONDecodeError)):
        return ChartError("Google Drive is not reachable")
    return None


def _gone(exc: BaseException) -> bool:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    try:
        return int(status) == 404
    except (TypeError, ValueError):
        return False


def _exec(req: Any) -> Any:
    try:
        return req.execute()
    except ChartError:
        raise
    except Exception as exc:
        if _gone(exc):
            raise ChartError("not found", 404) from exc
        mapped = _google_failure(exc)
        if mapped is not None:
            raise mapped from exc
        raise


def _root() -> str:
    root = (os.environ.get("GOOGLE_DRIVE_ROOT_ID") or "").strip()
    if not root:
        raise ChartError("GOOGLE_DRIVE_ROOT_ID is not set")
    return root


def drive_service():
    svc = getattr(_local, "svc", None)
    if svc is not None:
        return svc
    path = (os.environ.get("GOOGLE_DRIVE_SA_FILE") or "").strip()
    if not path:
        raise ChartError("GOOGLE_DRIVE_SA_FILE is not set")
    if not os.path.isfile(path):
        raise ChartError("GOOGLE_DRIVE_SA_FILE is not a file")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    try:
        creds = service_account.Credentials.from_service_account_file(path, scopes=_SCOPES)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        raise ChartError("Google account file is not valid") from exc
    except Exception as exc:
        mapped = _google_failure(exc)
        if mapped is not None:
            raise ChartError("Google account file is not valid") from exc
        raise
    subject = (os.environ.get("GOOGLE_DRIVE_IMPERSONATE") or "").strip()
    try:
        if subject:
            creds = creds.with_subject(subject)
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        mapped = _google_failure(exc)
        if mapped is not None:
            raise mapped from exc
        raise
    _local.svc = svc
    return svc


def _epoch(raw: str) -> int:
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _children(svc, parent: str) -> list[dict[str, Any]]:
    safe = parent.replace("'", "\\'")
    out: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "q": f"'{safe}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime)",
            "pageSize": 200,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if token:
            kwargs["pageToken"] = token
        resp = _exec(svc.files().list(**kwargs))
        out.extend(resp.get("files") or [])
        token = resp.get("nextPageToken")
        if not token:
            return out


def _one(children: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    hits = [row for row in children if row.get("name") == name]
    if len(hits) > 1:
        raise ChartError(f"duplicate name: {name}")
    return hits[0] if hits else None


def _still_there(svc, folder_id: str, parent: str, name: str) -> bool:
    """A cached id is only a folder we may write under when Drive still says so."""
    try:
        meta = _exec(svc.files().get(
            fileId=folder_id,
            fields="id, name, mimeType, trashed, parents",
            supportsAllDrives=True,
        ))
    except ChartError as exc:
        if exc.status == 404:
            return False
        raise
    if not isinstance(meta, dict) or meta.get("trashed"):
        return False
    if meta.get("name") != name or meta.get("mimeType") != _FOLDER:
        return False
    return parent in (meta.get("parents") or [])


def _resolve(svc, path: str, *, create: bool) -> str | None:
    parent = _root()
    for name in segments(path):
        cached = _cache.get((parent, name))
        if cached and _still_there(svc, cached, parent, name):
            parent = cached
            continue
        if cached:
            _cache.pop((parent, name), None)
        found = _one(_children(svc, parent), name)
        if found:
            if found.get("mimeType") != _FOLDER:
                raise ChartError(f"not a folder: {name}", 409)
            new_id = str(found["id"])
            _cache[(parent, name)] = new_id
            parent = new_id
            continue
        if not create:
            return None
        made = _exec(svc.files().create(
            body={"name": name, "mimeType": _FOLDER, "parents": [parent]},
            fields="id, name, mimeType",
            supportsAllDrives=True,
        ))
        new_id = str(made["id"])
        _cache[(parent, name)] = new_id
        parent = new_id
    return parent


def _walk(svc, path: str) -> tuple[str, str] | None:
    """(parent id, file id) of an existing file or folder. None if any segment is missing."""
    parent = _root()
    parts = segments(path)
    if not parts:
        return parent, parent
    for name in parts[:-1]:
        found = _one(_children(svc, parent), name)
        if not found or found.get("mimeType") != _FOLDER:
            return None
        parent = str(found["id"])
    found = _one(_children(svc, parent), parts[-1])
    if not found:
        return None
    return parent, str(found["id"])


def mkdir(path: str) -> None:
    if _resolve(drive_service(), path, create=True) is None:
        raise ChartError("mkdir failed")


def list_path(path: str) -> dict[str, Any]:
    svc = drive_service()
    found = _walk(svc, path)
    if found is None:
        raise ChartError("not found", 404)
    _parent, folder_id = found
    if segments(path):
        meta = _exec(svc.files().get(fileId=folder_id, fields="mimeType", supportsAllDrives=True))
        if meta.get("mimeType") != _FOLDER:
            raise ChartError("not a folder", 400)
    items = []
    for row in _children(svc, folder_id):
        kind = "folder" if row.get("mimeType") == _FOLDER else "file"
        items.append({
            "name": row.get("name") or "",
            "type": kind,
            "modify_time": _epoch(str(row.get("modifiedTime") or "")),
        })
    return {"ok": True, "items": items}


def read_file(path: str) -> tuple[bytes, str]:
    svc = drive_service()
    found = _walk(svc, path)
    if found is None:
        raise ChartError("not found", 404)
    _parent, file_id = found
    meta = _exec(svc.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True))
    if meta.get("mimeType") == _FOLDER:
        raise ChartError("not a file", 400)
    blob = _exec(svc.files().get_media(fileId=file_id, supportsAllDrives=True))
    if not isinstance(blob, (bytes, bytearray)):
        raise ChartError("drive file read failed")
    return bytes(blob), str(meta.get("mimeType") or "application/octet-stream")


def _bytes(payload: dict) -> tuple[bytes, str]:
    import base64

    raw_b64 = str(payload.get("b64") or "")
    if raw_b64:
        try:
            return base64.b64decode(raw_b64, validate=True), _mime(str(payload.get("path") or ""), fallback="application/octet-stream")
        except Exception as exc:
            raise ChartError("bad file body", 400) from exc
    if payload.get("text") is not None and str(payload.get("text")) != "":
        return str(payload.get("text")).encode(), "text/plain; charset=utf-8"
    raise ChartError("empty file", 400)


def _mime(path: str, fallback: str) -> str:
    low = path.lower()
    if low.endswith(".pdf"):
        return "application/pdf"
    if low.endswith(".txt"):
        return "text/plain; charset=utf-8"
    if low.endswith(".md"):
        return "text/markdown; charset=utf-8"
    return fallback


def save(payload: dict) -> dict[str, Any]:
    path = str(payload.get("path") or "")
    parts = segments(path)
    if not parts:
        raise ChartError("bad path", 400)
    data, mime = _bytes(payload)
    svc = drive_service()
    parent_path = "/" + "/".join(parts[:-1])
    parent = _resolve(svc, parent_path, create=False)
    if parent is None:
        raise ChartError("parent not found", 404)
    name = parts[-1]
    existing = _one(_children(svc, parent), name)
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(data, mimetype=mime.split(";")[0], resumable=False)
    if existing:
        if existing.get("mimeType") == _FOLDER:
            raise ChartError("not a file", 409)
        _exec(svc.files().update(fileId=existing["id"], media_body=media, supportsAllDrives=True))
    else:
        _exec(svc.files().create(
            body={"name": name, "parents": [parent]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ))
    return {"ok": True, "path": path}


def share(path: str, title: str = "") -> dict[str, Any]:
    del title
    svc = drive_service()
    found = _walk(svc, path)
    if found is None:
        raise ChartError("not found", 404)
    _parent, file_id = found
    _exec(svc.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
        supportsAllDrives=True,
    ))
    meta = _exec(svc.files().get(fileId=file_id, fields="webViewLink", supportsAllDrives=True))
    url = str(meta.get("webViewLink") or "")
    if not url.startswith("https://"):
        raise ChartError("share failed")
    return {"ok": True, "url": url, "cloaked": False}
