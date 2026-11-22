"""Desk sign-in. Routes call these as runtime.<name>.
A name a test replaces is read from app.runtime at call time.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, Response

import app.runtime as runtime
from app.portal.staff_auth import COOKIE as STAFF_COOKIE, secret_from_request

def _host_secret() -> str:
    s = os.environ.get('LIVEKIT_HOST_URL_SECRET', '').strip()
    if s:
        return s
    office = os.environ.get('LIVEKIT_OFFICE_URL', '').strip()
    if '/r/' in office:
        return office.rstrip('/').split('/r/')[-1]
    return ''

def _staff_secret(secret: str='', request: Request | None=None) -> str:
    """x-staff-secret header (cron / server-to-server). The `secret` query/body value is ignored for staff."""
    return secret_from_request(request or runtime._current_request.get())

def _desk_session_email(request: Request | None=None) -> str:
    import app.portal.desk_auth as desk_auth
    req = request or runtime._current_request.get()
    tok = req.cookies.get(desk_auth.COOKIE, '') if req is not None else ''
    return desk_auth.check(tok) or ''

def _staff_ok(request: Request | None=None) -> bool:
    if runtime.REMIND_SECRET and hmac.compare_digest(_staff_secret('', request), runtime.REMIND_SECRET):
        return True
    return bool(_desk_session_email(request))

def _require_staff(secret: str='', request: Request | None=None) -> None:
    if not _staff_ok(request):
        raise HTTPException(401, 'unauthorized')

def _require_staff_or_host(secret: str='', request: Request | None=None) -> None:
    """Desk secret or LiveKit host-link secret (office page)."""
    if _staff_ok(request):
        return
    got = _staff_secret('', request)
    host = _host_secret()
    if host and got and hmac.compare_digest(got, host):
        return
    raise HTTPException(401, 'unauthorized')

def _desk_cookie_refresh(resp: Response) -> None:
    """Re-send the session cookie (rolling 90 days) and drop the old raw-secret cookie."""
    import app.portal.desk_auth as desk_auth
    req = runtime._current_request.get()
    tok = req.cookies.get(desk_auth.COOKIE, '') if req is not None else ''
    if tok and desk_auth.check(tok):
        _set_desk_cookie(resp, tok)
    resp.delete_cookie(STAFF_COOKIE, path='/')

def _set_desk_cookie(resp: Response, token: str) -> None:
    import app.portal.desk_auth as desk_auth
    resp.set_cookie(desk_auth.COOKIE, token, httponly=True, secure=True, samesite='strict', max_age=desk_auth.SESSION_TTL, path='/')

def _client_ip(request: Request) -> str:
    fwd = (request.headers.get('x-forwarded-for') or '').split(',')[0].strip()
    return fwd or (request.client.host if request.client else '')

def _set_staff_cookie(resp: Response, secret: str) -> None:
    if not secret:
        return
    resp.set_cookie(STAFF_COOKIE, secret, httponly=True, secure=True, samesite='lax', max_age=60 * 60 * 24 * 30, path='/')
