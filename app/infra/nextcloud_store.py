"""Nextcloud WebDAV behind the chart pipe. Same paths as Proton: /Clients/Last, First/Chart/…"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from base64 import b64encode
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlencode, urlparse

from app.infra.chart_store import ChartError, segments

_DAV = "{DAV:}"
_PROPFIND = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<d:propfind xmlns:d='DAV:'><d:prop>"
    "<d:displayname/><d:getcontenttype/><d:getlastmodified/><d:resourcetype/>"
    "</d:prop></d:propfind>"
).encode()


def _settings() -> tuple[str, str, str]:
    base = (os.environ.get("NEXTCLOUD_URL") or "").strip().rstrip("/")
    user = (os.environ.get("NEXTCLOUD_USER") or "").strip()
    password = os.environ.get("NEXTCLOUD_APP_PASSWORD") or ""
    if not base or not user or not password:
        raise ChartError("Nextcloud needs NEXTCLOUD_URL, NEXTCLOUD_USER, and NEXTCLOUD_APP_PASSWORD")
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    local = parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}
    if parsed.scheme != "https" and not local:
        raise ChartError("NEXTCLOUD_URL must be https")
    return base, user, password


def _auth_header(user: str, password: str) -> str:
    token = b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _dav_path(user: str, path: str) -> str:
    parts = [quote(user, safe="")] + [quote(part, safe="") for part in segments(path)]
    return "/remote.php/dav/files/" + "/".join(parts)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=90) as resp:
            ctype = resp.headers.get("Content-Type") or ""
            return resp.status, resp.read(), ctype
    except urllib.error.HTTPError as exc:
        ctype = exc.headers.get("Content-Type") if exc.headers else ""
        return exc.code, exc.read(), ctype or ""
    except urllib.error.URLError as exc:
        raise ChartError("Nextcloud is not reachable") from exc


def _call(method: str, path: str, data: bytes | None = None, extra: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    base, user, password = _settings()
    headers = {"Authorization": _auth_header(user, password)}
    if extra:
        headers.update(extra)
    try:
        return _request(method, base + _dav_path(user, path), data, headers)
    except ChartError:
        raise
    except urllib.error.URLError as exc:
        raise ChartError("Nextcloud is not reachable") from exc


def _epoch(raw: str) -> int:
    if not raw:
        return 0
    try:
        return int(parsedate_to_datetime(raw).timestamp())
    except (TypeError, ValueError, OSError):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0


def _href_path(href: str) -> str:
    raw = href or ""
    if "://" in raw:
        return urlparse(raw).path
    return raw.split("?", 1)[0]


def _items(xml_bytes: bytes, user: str, folder: str) -> list[dict]:
    base = unquote(_dav_path(user, folder)).rstrip("/")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ChartError("drive list failed") from exc
    items = []
    for resp in root.findall(f"{_DAV}response"):
        href = unquote(_href_path(resp.findtext(f"{_DAV}href") or "")).rstrip("/")
        if href == base:
            continue
        if not href.startswith(base + "/"):
            continue
        rest = href[len(base) + 1:]
        if not rest or "/" in rest:
            continue
        prop = resp.find(f"{_DAV}propstat/{_DAV}prop")
        if prop is None:
            continue
        resource = prop.find(f"{_DAV}resourcetype")
        is_folder = resource is not None and resource.find(f"{_DAV}collection") is not None
        modified = prop.findtext(f"{_DAV}getlastmodified") or ""
        items.append({
            "name": rest,
            "type": "folder" if is_folder else "file",
            "modify_time": _epoch(modified),
        })
    return items


def _kind(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ChartError("mkdir failed") from exc
    resp = root.find(f"{_DAV}response")
    prop = None if resp is None else resp.find(f"{_DAV}propstat/{_DAV}prop")
    if prop is None:
        raise ChartError("mkdir failed")
    resource = prop.find(f"{_DAV}resourcetype")
    is_folder = resource is not None and resource.find(f"{_DAV}collection") is not None
    return "folder" if is_folder else "file"


def mkdir(path: str) -> None:
    parts = segments(path)
    if not parts:
        return
    built = ""
    for part in parts:
        built += "/" + part
        code, _body, _ctype = _call("MKCOL", built)
        if code in {201, 204}:
            continue
        if code == 405:
            # 405 means the name is taken. It is "already exists" only when that name is a folder.
            pcode, pbody, _pctype = _call(
                "PROPFIND", built, _PROPFIND, {"Depth": "0", "Content-Type": "application/xml"}
            )
            if pcode in {200, 207} and _kind(pbody) == "folder":
                continue
            raise ChartError("not a folder", 409)
        if code in {404, 409}:
            raise ChartError("parent not found", 404)
        raise ChartError(f"mkdir failed ({code})")


def list_path(path: str) -> dict:
    _base, user, _password = _settings()
    code, body, _ctype = _call("PROPFIND", path, _PROPFIND, {"Depth": "1", "Content-Type": "application/xml"})
    if code == 404:
        raise ChartError("not found", 404)
    if code not in {207, 200}:
        raise ChartError(f"drive list failed ({code})")
    return {"ok": True, "items": _items(body, user, path)}


def read_file(path: str) -> tuple[bytes, str]:
    if not segments(path):
        raise ChartError("not a file", 400)
    code, body, ctype = _call("GET", path)
    if code == 404:
        raise ChartError("not found", 404)
    if code != 200:
        raise ChartError(f"drive file read failed ({code})")
    return body, ctype or "application/octet-stream"


def _bytes(payload: dict) -> tuple[bytes, str]:
    import base64

    path = str(payload.get("path") or "")
    raw_b64 = str(payload.get("b64") or "")
    if raw_b64:
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except Exception as exc:
            raise ChartError("bad file body", 400) from exc
        low = path.lower()
        if low.endswith(".pdf"):
            return data, "application/pdf"
        return data, "application/octet-stream"
    if str(payload.get("text") or "") != "":
        return str(payload.get("text")).encode(), "text/plain; charset=utf-8"
    raise ChartError("empty file", 400)


def save(payload: dict) -> dict:
    path = str(payload.get("path") or "")
    if not segments(path):
        raise ChartError("bad path", 400)
    data, mime = _bytes(payload)
    code, _body, _ctype = _call("PUT", path, data, {"Content-Type": mime})
    if code in {404, 409}:
        raise ChartError("parent not found", 404)
    if code not in {200, 201, 204}:
        raise ChartError(f"drive save failed ({code})")
    return {"ok": True, "path": path}


def share(path: str, title: str = "") -> dict:
    del title
    if not segments(path):
        raise ChartError("bad path", 400)
    base, user, password = _settings()
    body = urlencode({"path": "/" + "/".join(segments(path)), "shareType": "3"}).encode()
    headers = {
        "Authorization": _auth_header(user, password),
        "OCS-APIRequest": "true",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        code, raw, _ctype = _request("POST", base + "/ocs/v2.php/apps/files_sharing/api/v1/shares", body, headers)
    except urllib.error.URLError as exc:
        raise ChartError("Nextcloud is not reachable") from exc
    if code >= 400:
        raise ChartError("share failed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ChartError("share failed") from exc
    status = (root.findtext(".//statuscode") or "").strip()
    url = (root.findtext(".//url") or "").strip()
    if status not in {"100", "200"} or not url.startswith("https://"):
        raise ChartError("share failed")
    return {"ok": True, "url": url, "cloaked": False}
