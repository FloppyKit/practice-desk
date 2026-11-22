"""Chart pipe. Proton still uses the notify bridge. Routes call these as runtime.<name>.
urlopen and the save/list helpers are read from app.runtime so a test can replace them.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest

from fastapi import HTTPException

import app.runtime as runtime

def _last_note_listing(vn_path: str) -> dict[str, Any]:
    if not vn_path:
        return {'ok': False, 'status': 'none', 'hint': 'After-visit summary will land in Chart / Visit notes.'}
    try:
        data = runtime._drive_list(vn_path)
    except Exception:
        data = {}
    items = [it for it in data.get('items') or [] if (it.get('type') or '') == 'file' and (it.get('name') or '')]
    if not items:
        return {'ok': False, 'status': 'empty', 'path': vn_path, 'hint': 'No after-visit summary in Visit notes yet.'}
    items.sort(key=lambda it: int(it.get('modify_time') or 0), reverse=True)
    latest = items[0]
    name = latest.get('name') or ''
    return {'ok': True, 'status': 'file', 'name': name, 'path': vn_path + '/' + name, 'hint': name, 'count': len(items)}

def _drive_mkdir(path: str) -> None:
    from app.infra.chart_store import ChartError, local_mkdir
    try:
        if local_mkdir(path):
            return
    except ChartError as e:
        raise HTTPException(e.status if e.status >= 400 else 502, str(e)[:200]) from e
    from app.messaging.notify import NOTIFY_TOKEN
    payload = json.dumps({'path': path}).encode()
    req = UrlRequest(_notify_base() + '/drive/mkdir', data=payload, headers={'Content-Type': 'application/json', 'X-Notify-Token': NOTIFY_TOKEN}, method='POST')
    try:
        with runtime.urlopen(req, timeout=90) as resp:
            json.loads(resp.read().decode() or '{}')
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    except URLError as e:
        raise HTTPException(502, 'Drive is not reachable') from e

def _folder_names(path: str) -> dict[str, str] | None:
    """{lowercase name: real name} of child folders, or None when the folder is missing/unreadable."""
    data = runtime._drive_list(path)
    if not data.get('ok'):
        return None
    return {str(i.get('name') or '').lower(): str(i.get('name') or '') for i in data.get('items') or [] if (i.get('type') or '') == 'folder' and i.get('name')}

def _ensure_visit_notes(chart: str, sub: str='') -> str:
    """Chart/Visit Notes path for signing (or Chart/<sub>, e.g. Consents). Creates what is missing, never duplicates an existing folder:
    a missing living client folder gets the real-client tree (Billing + Chart/Consents, Labs, Letters,
    Orders, Visit Notes); an existing chart only gets Visit Notes if it has none (case-insensitive)."""
    from app.infra.drive_paths import BILLING_CHILD, CHART_SUBFOLDERS, LIVING_ROOT, VISIT_NOTES_FOLDER
    want = sub or VISIT_NOTES_FOLDER
    chart = chart.rstrip('/')
    (client_dir, _) = chart.rsplit('/', 1)
    kids = runtime._folder_names(chart)
    if kids is None:
        if not client_dir.startswith(LIVING_ROOT + '/'):
            raise HTTPException(400, 'chart folder not found (archive tree is never created)')
        if runtime._folder_names(client_dir) is None:
            if runtime._folder_names(LIVING_ROOT) is None:
                raise HTTPException(502, 'Drive is not reachable right now; nothing was created')
            runtime._drive_mkdir(client_dir)
            runtime._drive_mkdir(client_dir + '/' + BILLING_CHILD)
        runtime._drive_mkdir(chart)
        for kid in CHART_SUBFOLDERS:
            runtime._drive_mkdir(chart + '/' + kid)
        if want not in CHART_SUBFOLDERS:
            runtime._drive_mkdir(chart + '/' + want)
        return chart + '/' + want
    have = kids.get(want.lower())
    if have:
        return chart + '/' + have
    runtime._drive_mkdir(chart + '/' + want)
    return chart + '/' + want

def _unique_pdf_name(folder: str, stem: str) -> str:
    """`stem.pdf`, or `stem-2.pdf`, `stem-3.pdf`… when a same-day note is already there (never overwrite)."""
    data = runtime._drive_list(folder)
    names = {str(i.get('name') or '').lower() for i in data.get('items') or []}
    taken = lambda n: n.lower() in names or n[:-4].lower() + ' chart.pdf' in names
    (name, k) = (f'{stem}.pdf', 2)
    while taken(name):
        (name, k) = (f'{stem}-{k}.pdf', k + 1)
    return name

def _drive_list(path: str) -> dict[str, Any]:
    (code, body, _) = runtime._notify_get('/drive/list?path=' + quote(path or '/', safe='/'))
    if code != 200:
        return {}
    try:
        return json.loads(body.decode() or '{}')
    except Exception:
        return {}

def _client_files_payload(row: dict[str, Any], *, confirm: bool=False) -> dict[str, str]:
    from app.infra.drive_paths import existing_paths, paths_from_row
    paths = {'folder': '', 'chart': '', 'billing': ''}
    try:
        if confirm:
            paths = existing_paths(row, runtime._drive_list)
        else:
            paths = paths_from_row(row)
    except Exception:
        paths = {'folder': '', 'chart': '', 'billing': ''}
    return {'chart': row.get('chart_url') or '', 'billing': row.get('billing_url') or '', 'folder_path': paths.get('folder') or '', 'chart_path': paths.get('chart') or '', 'billing_path': paths.get('billing') or ''}

def _notify_base() -> str:
    raw = os.environ.get('NOTIFY_WEBHOOK_URL') or 'http://127.0.0.1:8791/notify'
    return raw[:-7] if raw.endswith('/notify') else raw.rstrip('/')

def _notify_get(path: str) -> tuple[int, bytes, str]:
    from app.infra.chart_store import local_get
    local = local_get(path)
    if local is not None:
        return local
    from app.messaging.notify import NOTIFY_TOKEN
    url = _notify_base() + path
    req = UrlRequest(url, headers={'X-Notify-Token': NOTIFY_TOKEN}, method='GET')
    try:
        with runtime.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get('Content-Type') or 'application/octet-stream'
            return (resp.status, resp.read(), ctype)
    except HTTPError as e:
        return (e.code, e.read(), 'application/json')
    except URLError:
        return (502, json.dumps({'ok': False, 'error': 'Drive is not reachable'}).encode(), 'application/json')

def _notify_post(path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.infra.chart_store import local_post
    local = local_post(path, payload)
    if local is not None:
        return local
    from app.messaging.notify import NOTIFY_TOKEN
    data = json.dumps(payload).encode()
    req = UrlRequest(_notify_base() + path, data=data, headers={'Content-Type': 'application/json', 'X-Notify-Token': NOTIFY_TOKEN}, method='POST')
    try:
        with runtime.urlopen(req, timeout=120) as resp:
            return (resp.status, json.loads(resp.read().decode() or '{}'))
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode() or '{}')
        except Exception:
            body = {'ok': False, 'error': 'notify failed'}
        return (e.code if e.code >= 400 else 502, body)
    except URLError:
        return (502, {'ok': False, 'error': 'Drive is not reachable'})

def _drive_http_save(payload: dict[str, Any]) -> dict[str, Any]:
    """Save bytes on the chart pipe. Proton still uses the notify bridge. Other backends do not."""
    from app.infra.chart_store import local_post
    local = local_post('/drive/save', payload)
    if local is not None:
        (code, body) = local
        if code >= 400 or not body.get('ok'):
            raise HTTPException(code if code >= 400 else 502, str(body.get('error') or 'drive save failed')[:200])
        return body
    from app.messaging.notify import NOTIFY_TOKEN
    data = json.dumps(payload).encode()
    req = UrlRequest(_notify_base() + '/drive/save', data=data, headers={'Content-Type': 'application/json', 'X-Notify-Token': NOTIFY_TOKEN}, method='POST')
    try:
        with runtime.urlopen(req, timeout=180) as resp:
            saved = json.loads(resp.read().decode() or '{}')
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    except URLError as e:
        raise HTTPException(502, 'Drive is not reachable') from e
    if not saved.get('ok'):
        raise HTTPException(502, 'drive save failed')
    return saved
