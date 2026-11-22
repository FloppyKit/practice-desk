"""Chart pipe: Proton stays on notify. Google Drive and Nextcloud use the same calls."""
from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

from app.infra import gdrive_store, nextcloud_store
from app.infra.chart_store import ChartError, backend_name, local_get, local_mkdir, local_post


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value() if callable(self.value) else self.value


class FakeDrive:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.seq = 0
        self.shared: list[str] = []

    def files(self):
        return _Files(self)

    def permissions(self):
        return _Perms(self)

    def _parent_from_q(self, q: str) -> str:
        return q.split(" in parents", 1)[0].strip().strip("'").replace("\\'", "'")

    def list(self, **kw):
        parent = self._parent_from_q(kw["q"])
        rows = [n for n in self.nodes.values() if n["parent"] == parent]
        files = [{
            "id": n["id"], "name": n["name"], "mimeType": n["mime"],
            "modifiedTime": "2026-01-02T00:00:00Z",
        } for n in rows]
        return _Call({"files": files})

    def create(self, body, media_body=None, **_kw):
        self.seq += 1
        fid = f"id{self.seq}"
        data = b""
        mime = body.get("mimeType") or "application/octet-stream"
        if media_body is not None:
            data = media_body.getbytes(0, 10_000_000)
            mime = getattr(media_body, "_mimetype", None) or mime
        parent = (body.get("parents") or [""])[0]
        self.nodes[fid] = {"id": fid, "name": body["name"], "mime": mime, "parent": parent, "data": data}
        return _Call({"id": fid, "name": body["name"], "mimeType": mime})

    def update(self, fileId, media_body=None, **_kw):
        if media_body is not None:
            self.nodes[fileId]["data"] = media_body.getbytes(0, 10_000_000)
        return _Call({"id": fileId})

    def get_media(self, fileId, **_kw):
        return _Call(self.nodes[fileId]["data"])

    def get(self, fileId, **_kw):
        node = self.nodes[fileId]
        return _Call({
            "mimeType": node["mime"],
            "webViewLink": "https://drive.google.com/file/d/" + fileId + "/view",
        })


class _Files:
    def __init__(self, drive: FakeDrive) -> None:
        self.drive = drive

    def list(self, **kw):
        return self.drive.list(**kw)

    def create(self, body, media_body=None, **kw):
        return self.drive.create(body, media_body, **kw)

    def update(self, fileId, media_body=None, **kw):
        return self.drive.update(fileId, media_body, **kw)

    def get_media(self, fileId, **kw):
        return self.drive.get_media(fileId, **kw)

    def get(self, fileId, **kw):
        return self.drive.get(fileId, **kw)


class _Perms:
    def __init__(self, drive: FakeDrive) -> None:
        self.drive = drive

    def create(self, fileId, body, **_kw):
        assert body["type"] == "anyone" and body["role"] == "reader"
        self.drive.shared.append(fileId)
        return _Call({"id": "perm"})


class FakeCloud:
    def __init__(self) -> None:
        self.folders = {"/"}
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def __call__(self, method, url, data=None, headers=None):
        self.calls.append(method + " " + url.split("?", 1)[0])
        assert (headers or {}).get("Authorization", "").startswith("Basic ")
        path = unquote(urlparse(url).path)
        prefix = "/remote.php/dav/files/ada"
        if path.startswith("/ocs/"):
            body = (
                "<?xml version='1.0'?><ocs><meta><statuscode>200</statuscode></meta>"
                "<data><url>https://cloud.example.test/s/demo</url></data></ocs>"
            ).encode()
            return 200, body, "application/xml"
        if not path.startswith(prefix):
            return 404, b"", ""
        chart = path[len(prefix):] or "/"
        if method == "MKCOL":
            parent = chart.rsplit("/", 1)[0] or "/"
            if parent not in self.folders:
                return 409, b"parent", ""
            if chart in self.folders:
                return 405, b"exists", ""
            self.folders.add(chart)
            return 201, b"", ""
        if method == "PROPFIND":
            if chart != "/" and chart not in self.folders:
                return 404, b"", ""
            return 207, self._xml(chart), "application/xml"
        if method == "PUT":
            parent = chart.rsplit("/", 1)[0] or "/"
            if parent not in self.folders:
                return 409, b"parent", ""
            self.files[chart] = data or b""
            return 201, b"", ""
        if method == "GET":
            if chart not in self.files:
                return 404, b"", ""
            return 200, self.files[chart], "application/pdf"
        return 500, b"", ""

    def _xml(self, folder: str) -> bytes:
        base = "/remote.php/dav/files/ada" + ("" if folder == "/" else folder)
        rows = [
            f"<d:response><d:href>{escape(base)}/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/></d:resourcetype>"
            "<d:getlastmodified>Fri, 21 Nov 2026 15:00:00 GMT</d:getlastmodified>"
            "</d:prop></d:propstat></d:response>"
        ]
        pre = "" if folder == "/" else folder
        kids = []
        for name in self.folders:
            if name.rsplit("/", 1)[0] == (pre or "") or (folder == "/" and name.count("/") == 1):
                if name != folder:
                    kids.append((name.rsplit("/", 1)[-1], True, name))
        for name, blob in self.files.items():
            if name.rsplit("/", 1)[0] == folder or (folder == "/" and name.count("/") == 1 and name.rsplit("/", 1)[0] == ""):
                kids.append((name.rsplit("/", 1)[-1], False, name))
        for label, is_dir, _full in kids:
            href = base + "/" + quote(label)
            kind = "<d:collection/>" if is_dir else ""
            rows.append(
                f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
                f"<d:resourcetype>{kind}</d:resourcetype>"
                "<d:getlastmodified>Fri, 21 Nov 2026 15:00:00 GMT</d:getlastmodified>"
                "</d:prop></d:propstat></d:response>"
            )
        xml = "<?xml version='1.0'?><d:multistatus xmlns:d='DAV:'>" + "".join(rows) + "</d:multistatus>"
        return xml.encode()


@contextmanager
def _env(**values: str):
    old = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_proton_is_the_default_and_leaves_the_notify_pipe() -> None:
    with _env(CHART_BACKEND=None):
        assert backend_name() == "proton"
        assert local_get("/drive/list?path=/Clients") is None
        assert local_post("/drive/save", {"path": "/Clients/a.txt", "text": "a"}) is None
        assert local_mkdir("/Clients") is False


def test_unknown_backend_fails() -> None:
    with _env(CHART_BACKEND="dropbox"):
        try:
            backend_name()
        except ChartError as exc:
            assert "proton" in str(exc)
        else:
            raise AssertionError("unknown backend was accepted")


def test_email_pipe_does_not_switch_with_the_chart() -> None:
    with _env(CHART_BACKEND="gdrive"):
        assert local_get("/notify/send") is None
        assert local_post("/notify/send", {}) is None


def test_gdrive_round_trip_and_a_missing_parent_fails() -> None:
    fake = FakeDrive()
    gdrive_store.clear_cache()
    old = gdrive_store.drive_service
    gdrive_store.drive_service = lambda: fake
    try:
        with _env(CHART_BACKEND="gdrive", GOOGLE_DRIVE_ROOT_ID="root", GOOGLE_DRIVE_SA_FILE="/not/used"):
            assert local_mkdir("/Clients/Patient, Unit") is True
            assert local_mkdir("/Clients/Patient, Unit") is True
            assert len(fake.nodes) == 2
            code, body, _ctype = local_get("/drive/list?path=" + quote("/Clients", safe="/"))
            assert code == 200 and b"Patient, Unit" in body
            saved = local_post("/drive/save", {"path": "/Clients/Patient, Unit/note.txt", "text": "hello"})
            assert saved is not None and saved[0] == 200 and saved[1]["ok"] is True
            code, blob, ctype = local_get("/drive/file?path=" + quote("/Clients/Patient, Unit/note.txt", safe="/"))
            assert code == 200 and blob == b"hello" and ctype.startswith("text/plain")
            shared = local_post("/drive/share", {"path": "/Clients/Patient, Unit/note.txt", "title": "note"})
            assert shared is not None and shared[1]["url"].startswith("https://drive.google.com/")
            assert shared[1]["cloaked"] is False and fake.shared
            missing = local_post("/drive/save", {"path": "/Clients/Missing/a.txt", "b64": base64.b64encode(b"a").decode()})
            assert missing is not None and missing[0] == 404 and "parent" in missing[1]["error"]
    finally:
        gdrive_store.drive_service = old
        gdrive_store.clear_cache()


def test_gdrive_without_credentials_does_not_save() -> None:
    gdrive_store.clear_cache()
    with _env(CHART_BACKEND="gdrive", GOOGLE_DRIVE_SA_FILE=None, GOOGLE_DRIVE_ROOT_ID=None):
        try:
            local_mkdir("/Clients")
        except ChartError as exc:
            assert "GOOGLE_DRIVE_SA_FILE" in str(exc)
        else:
            raise AssertionError("mkdir succeeded without credentials")


def test_nextcloud_round_trip_and_a_missing_parent_fails() -> None:
    fake = FakeCloud()
    old = nextcloud_store._request
    nextcloud_store._request = fake
    try:
        with _env(
            CHART_BACKEND="nextcloud",
            NEXTCLOUD_URL="https://cloud.example.test",
            NEXTCLOUD_USER="ada",
            NEXTCLOUD_APP_PASSWORD="test-app-pw",
        ):
            assert local_mkdir("/Clients/Patient, Unit/Chart") is True
            listed = local_get("/drive/list?path=" + quote("/Clients", safe="/"))
            assert listed is not None and listed[0] == 200 and b"Patient, Unit" in listed[1]
            saved = local_post("/drive/save", {
                "path": "/Clients/Patient, Unit/Chart/note.pdf",
                "b64": base64.b64encode(b"%PDF-1.4").decode(),
            })
            assert saved is not None and saved[1]["ok"] is True
            got = local_get("/drive/file?path=" + quote("/Clients/Patient, Unit/Chart/note.pdf", safe="/"))
            assert got is not None and got[1] == b"%PDF-1.4"
            shared = local_post("/drive/share", {"path": "/Clients/Patient, Unit/Chart/note.pdf"})
            assert shared is not None and shared[1]["url"] == "https://cloud.example.test/s/demo"
            missing = local_post("/drive/save", {"path": "/No/Such/file.txt", "text": "x"})
            assert missing is not None and missing[0] == 404 and "parent" in missing[1]["error"]
            assert any(call.startswith("MKCOL ") for call in fake.calls)
            assert any("/ocs/" in call for call in fake.calls)
    finally:
        nextcloud_store._request = old


def test_nextcloud_without_credentials_does_not_save() -> None:
    with _env(CHART_BACKEND="nextcloud", NEXTCLOUD_URL=None, NEXTCLOUD_USER=None, NEXTCLOUD_APP_PASSWORD=None):
        try:
            local_mkdir("/Clients")
        except ChartError as exc:
            assert "NEXTCLOUD_URL" in str(exc)
            assert "test-app-pw" not in str(exc)
        else:
            raise AssertionError("mkdir succeeded without credentials")


def test_nextcloud_refuses_a_cleartext_url() -> None:
    with _env(CHART_BACKEND="nextcloud", NEXTCLOUD_URL="http://cloud.example.test", NEXTCLOUD_USER="ada", NEXTCLOUD_APP_PASSWORD="test-app-pw"):
        try:
            local_mkdir("/Clients")
        except ChartError as exc:
            assert "https" in str(exc)
        else:
            raise AssertionError("cleartext was accepted")


def test_bad_paths_are_rejected() -> None:
    with _env(CHART_BACKEND="nextcloud", NEXTCLOUD_URL="https://cloud.example.test", NEXTCLOUD_USER="ada", NEXTCLOUD_APP_PASSWORD="test-app-pw"):
        try:
            local_mkdir("/Clients/../../secrets")
        except ChartError as exc:
            assert exc.status == 400
        else:
            raise AssertionError("traversal was accepted")


def test_runtime_list_does_not_call_the_notify_bridge() -> None:
    import app.runtime as runtime
    fake = FakeDrive()
    gdrive_store.clear_cache()
    old_svc = gdrive_store.drive_service
    old_open = runtime.urlopen
    gdrive_store.drive_service = lambda: fake
    runtime.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("notify bridge was called"))
    try:
        with _env(CHART_BACKEND="gdrive", GOOGLE_DRIVE_ROOT_ID="root", GOOGLE_DRIVE_SA_FILE="/not/used"):
            assert local_mkdir("/Clients") is True
            code, body, _ctype = runtime._notify_get("/drive/list?path=/Clients")
            assert code == 200 and b'"ok": true' in body
    finally:
        gdrive_store.drive_service = old_svc
        runtime.urlopen = old_open
        gdrive_store.clear_cache()
