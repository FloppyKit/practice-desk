"""One chart pipe. Proton stays on the notify bridge. Google Drive and Nextcloud answer the same calls."""
from __future__ import annotations

import json
import os
import urllib.error
from urllib.parse import parse_qs, unquote, urlparse

_ALIASES = {
    "google": "gdrive",
    "google-drive": "gdrive",
    "google_drive": "gdrive",
    "gdrive": "gdrive",
    "nextcloud": "nextcloud",
    "proton": "proton",
}


class ChartError(RuntimeError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def segments(path: str) -> list[str]:
    """Absolute chart path as folder names. Rejects traversal."""
    raw = (path or "").strip()
    if "\\" in raw or "\x00" in raw:
        raise ChartError("bad path", 400)
    if raw in ("", "/"):
        return []
    parts = raw.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(part in ("", ".", "..") for part in parts):
        raise ChartError("bad path", 400)
    return parts


def backend_name() -> str:
    raw = (os.environ.get("CHART_BACKEND") or "proton").strip().lower()
    name = _ALIASES.get(raw)
    if not name:
        raise ChartError("CHART_BACKEND must be proton, gdrive, or nextcloud", 502)
    return name


def _store():
    name = backend_name()
    if name == "gdrive":
        from app.infra import gdrive_store
        return gdrive_store
    if name == "nextcloud":
        from app.infra import nextcloud_store
        return nextcloud_store
    return None


def _query_path(url_path: str) -> str:
    query = urlparse(url_path).query
    if not query and "path=" in url_path:
        query = url_path.split("?", 1)[-1]
    got = parse_qs(query).get("path") or [""]
    return unquote(got[0])


def _err(exc: ChartError) -> tuple[int, bytes, str]:
    body = json.dumps({"ok": False, "error": str(exc)}).encode()
    status = exc.status if exc.status >= 400 else 502
    return status, body, "application/json"


def _post_err(exc: ChartError) -> tuple[int, dict]:
    status = exc.status if exc.status >= 400 else 502
    return status, {"ok": False, "error": str(exc)}


def _transport(exc: BaseException) -> ChartError | None:
    """A dead server or a bad account file is a chart failure, not an uncaught 500."""
    if isinstance(exc, ChartError):
        return None
    name = type(exc).__name__
    mod = type(exc).__module__
    if name in {
        "URLError",
        "HttpError",
        "GoogleAuthError",
        "RefreshError",
        "TransportError",
        "MalformedError",
        "DefaultCredentialsError",
    }:
        return ChartError("chart store is not reachable", 502)
    if mod.startswith("google.auth") or mod.startswith("googleapiclient.errors"):
        return ChartError("chart store is not reachable", 502)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError)):
        return ChartError("chart store is not reachable", 502)
    return None


def local_get(path: str) -> tuple[int, bytes, str] | None:
    """Handle a /drive GET for gdrive or nextcloud. None means Proton's notify bridge."""
    if not path.startswith("/drive/"):
        return None
    try:
        store = _store()
        if store is None:
            return None
        target = _query_path(path)
        if path.startswith("/drive/list"):
            data = store.list_path(target)
            return 200, json.dumps(data).encode(), "application/json"
        if path.startswith("/drive/file"):
            blob, ctype = store.read_file(target)
            return 200, blob, ctype or "application/octet-stream"
        raise ChartError("no such drive call", 404)
    except ChartError as exc:
        return _err(exc)
    except Exception as exc:
        mapped = _transport(exc)
        if mapped is not None:
            return _err(mapped)
        raise


def local_post(path: str, payload: dict) -> tuple[int, dict] | None:
    """Handle a /drive POST for gdrive or nextcloud. None means Proton's notify bridge."""
    if not path.startswith("/drive/"):
        return None
    try:
        store = _store()
        if store is None:
            return None
        if path.startswith("/drive/save"):
            return 200, store.save(payload)
        if path.startswith("/drive/share"):
            return 200, store.share(str(payload.get("path") or ""), str(payload.get("title") or ""))
        raise ChartError("no such drive call", 404)
    except ChartError as exc:
        return _post_err(exc)
    except Exception as exc:
        mapped = _transport(exc)
        if mapped is not None:
            return _post_err(mapped)
        raise


def local_mkdir(path: str) -> bool:
    """True when gdrive or nextcloud created the folder. False means use the Proton pipe."""
    try:
        store = _store()
        if store is None:
            return False
        store.mkdir(path)
    except ChartError:
        raise
    except Exception as exc:
        mapped = _transport(exc)
        if mapped is not None:
            raise mapped from exc
        raise
    return True
