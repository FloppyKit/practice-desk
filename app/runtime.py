from __future__ import annotations
import re
import hmac
import json
import logging
import os
import time
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
import app.infra.clients_db as clients_db
from app.infra.config import DEFAULT_CALENDAR, get_event_type, load_config, practice_zone, timezone_name
from app.booking.practice_links import book_host, book_url, cors_origins, live_origin, portal_origin, short_host, site_url, video_host, video_url, www_origin
from app.messaging.email_templates import HOST_EMAIL, HOST_NAME, HOST_PHONE, HOST_SHORT, PRACTICE
from app.booking.cal import create_event, delete_event, get_event, list_booker_events, list_events, patch_event, using_caldav
from app.messaging.notify import ENABLE_SMS, parse_email_list, patient_confirm_email, patient_confirm_sms, send_late, send_named_email, staff_notify_email
from app.booking.reminders import process_reminders
from app.messaging.sms_bridge import ack as sms_bridge_ack, bridge_enabled, claim as sms_bridge_claim, due_summary, enqueue as sms_bridge_enqueue, render_agent_template, secret_ok
from app.messaging.spruce_api import spruce_api_enabled
from app.booking.slots import generate_slots
from app.portal.patients import client_by_email, client_by_id, create_client as patient_create_client, search_clients, supabase_ready
from app.booking.week_people import merge_client_hits, people_from_visits
from app.portal.staff_auth import COOKIE as STAFF_COOKIE, secret_from_request
from app.infra.tokens import mint_cancel_token, mint_verify_token, verify_cancel_token, verify_patient_token
import app.portal.cipher_mode as cipher_mode
import app.portal.cipher_queue as cipher_queue
import app.portal.cipher_seal as cipher_seal

def _seed_caldav_bg() -> None:
    try:
        from app.booking.cal import seed_from_google, using_caldav
        if using_caldav():
            result = seed_from_google()
            print('caldav_seed', result, flush=True)
    except Exception as e:
        print('caldav_seed_fail', e, flush=True)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    seed = None
    if os.environ.get('CALDAV_SEED_GOOGLE', '1').strip().lower() in ('1', 'true', 'yes'):
        import threading
        seed = threading.Thread(target=_seed_caldav_bg, name='caldav-seed', daemon=True)
        seed.start()
    import app.booking.gsync as gsync
    gsync.start(wait_for=seed)
    import app.portal.onboarding as onboarding
    onboarding.start_sweeper(_onboarding_fx)
    _warm_drawer_bg()
    yield
VERSION = '0.4.0'
GIT_SHA = (os.environ.get('GIT_SHA') or '').strip() or 'unknown'
app = FastAPI(title='Practice Desk Booker', version=VERSION, lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=cors_origins(), allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
STATIC = Path(__file__).resolve().parent.parent / 'static'
if STATIC.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')
PUBLIC_BASE = os.environ.get('PUBLIC_BASE_URL', book_url()).rstrip('/')
REMIND_SECRET = os.environ.get('REMIND_CRON_SECRET', '')
SITE_PUBLIC = os.environ.get('SITE_PUBLIC_URL', site_url()).rstrip('/')
INTAKE_MINT_URL = os.environ.get('INTAKE_MINT_URL', 'http://127.0.0.1:3000/api/intake/mint')
INTAKE_MINT_SECRET = os.environ.get('INTAKE_MINT_SECRET', '').strip()
ICS_FEED_SECRET = os.environ.get('ICS_FEED_SECRET', '').strip()
_current_request: ContextVar[Request | None] = ContextVar('desk_request', default=None)

@app.middleware('http')
async def _bind_request(request: Request, call_next):
    token = _current_request.set(request)
    try:
        return await call_next(request)
    finally:
        _current_request.reset(token)
SEND_ITEMS: dict[str, dict[str, Any]] = {'intake': {'label': 'Intake packet', 'mail': 'intake', 'mint': True, 'forms': ['personal', 'consents', 'card-auth']}, 'personal': {'label': 'Personal information', 'mail': 'form_share', 'url': f'{SITE_PUBLIC}/forms/personal'}, 'consents': {'label': 'Consents & policies', 'mail': 'form_share', 'url': f'{SITE_PUBLIC}/forms/consents'}, 'card': {'label': 'Payment authorization', 'mail': 'form_share', 'url': f'{SITE_PUBLIC}/forms/card'}, 'roi': {'label': 'Release of information', 'mail': 'form_share', 'url': f'{SITE_PUBLIC}/forms/roi'}, 'pay': {'label': 'Payment on file', 'mail': 'form_share', 'url': f'{SITE_PUBLIC}/pay'}}

def _prefilled_book_url(slug: str, *, name: str='', email: str='', phone: str='', duration: int | None=None) -> str:
    q: dict[str, str] = {}
    if name:
        q['name'] = name
    if email:
        q['email'] = email
    if phone:
        q['phone'] = phone
    if duration:
        q['duration'] = str(int(duration))
    return f'{PUBLIC_BASE}/{slug}' + ('?' + urlencode(q) if q else '')

def _book_link(client_id: str, *, slug: str='follow-up', duration: int | None=None) -> dict[str, Any]:
    """Short client-bound booking link: book.example.com/b/<8-char code>. No PHI, no expiry,
    reused per client until revoked. Long HMAC links got mangled when relayed, so the short code stays.
    slug/duration kept for callers; the code always opens follow-up."""
    code = clients_db.book_code_for(str(client_id))
    return {'url': f'{PUBLIC_BASE}/b/{code}'}
_BOOK_FAIL_LOG = Path(os.environ.get('BOOKER_LOG_DIR', '/data/logs')) / 'booklink-failures.jsonl'
_B_HITS: dict[str, list[float]] = {}

def _b_rate_ok(ip: str, limit: int=30, window: float=60.0) -> bool:
    now = time.time()
    hits = [t for t in _B_HITS.get(ip, []) if now - t < window]
    hits.append(now)
    _B_HITS[ip] = hits[-limit - 1:]
    if len(_B_HITS) > 5000:
        _B_HITS.clear()
    return len(hits) <= limit

def _log_book_fail(token: str, reason: str, where: str) -> None:
    """No PHI, never the token: length, shape, reason, and which minter's format it looks like."""
    t = str(token or '')
    src = 'short-code' if '.' not in t and len(t) <= 16 else 'long-hmac (booker _book_link, pre-short)' if t.count('.') == 1 else 'unknown'
    try:
        _BOOK_FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _BOOK_FAIL_LOG.open('a') as f:
            f.write(json.dumps({'ts': datetime.now(timezone.utc).isoformat(), 'where': where, 'len': len(t), 'parts': [len(x) for x in t.split('.')][:3], 'reason': reason, 'source': src}) + '\n')
    except Exception:
        pass

def _book_token_client(token: str, where: str='b') -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Short code or legacy long token -> (payload, client row). Failures are logged without the token."""
    import re as _re
    from urllib.parse import unquote
    from app.infra.tokens import verify_book_token
    raw = _re.sub('\\s+', '', unquote(str(token or '')))
    code = _re.sub('[^A-Za-z0-9]', '', raw)
    if '.' not in raw.rstrip('.)!,;:\'"') and 6 <= len(code) <= 16:
        hit = clients_db.book_code_lookup(code[:clients_db.BOOK_CODE_LEN])
        if not hit:
            _log_book_fail(raw, 'unknown code', where)
            return None
        if hit[1]:
            _log_book_fail(raw, 'revoked code', where)
            return None
        row = clients_db.get_by_id(hit[0])
        if not row:
            _log_book_fail(raw, 'unknown client', where)
            return None
        return ({'k': 'bk', 'id': hit[0], 's': 'follow-up', 'v': int(row.get('book_link_v') or 0)}, row)
    try:
        p = verify_book_token(raw)
    except Exception as e:
        _log_book_fail(raw, str(e)[:60] or 'decode error', where)
        return None
    row = clients_db.get_by_id(str(p['id']))
    if not row:
        _log_book_fail(raw, 'unknown client', where)
        return None
    if int(p.get('v') or 0) != int(row.get('book_link_v') or 0):
        _log_book_fail(raw, 'bad version (revoked)', where)
        return None
    return (p, row)

def _mint_intake_url(email: str, name: str='', forms: list[str] | None=None, client_id: str='') -> str:
    """Mint a personal intake link. With client_id the link is bound to that client row and saved on it."""
    payload = {'secret': INTAKE_MINT_SECRET, 'email': email, 'name': name, 'clientId': client_id, 'sendEmail': False, 'ttlDays': 21, 'forms': forms or ['personal', 'consents', 'card-auth']}
    req = UrlRequest(INTAKE_MINT_URL, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode() or '{}')
    except HTTPError as e:
        detail = e.read().decode()[:200] if e.fp else str(e)
        raise HTTPException(502, f'intake mint failed: {detail}') from e
    except URLError as e:
        raise HTTPException(502, f'intake mint unreachable: {e.reason}') from e
    url = str((data or {}).get('url') or '')
    if not url:
        raise HTTPException(502, 'intake mint returned no url')
    if client_id:
        clients_db.set_intake_link(client_id, url)
    return url

def mint_client_intake(row: dict[str, Any], ttl_days: int=21) -> str:
    """Per-client link (client id in the token), stored on the row with minted_at."""
    em = str(row.get('email') or '').strip().lower()
    if not em:
        raise HTTPException(400, 'client has no email')
    return _mint_intake_url(em, str(row.get('display_name') or ''), client_id=str(row['id']))

def _client_for_booking(email: str, phone: str='', name: str='') -> dict[str, Any] | None:
    """Existing client for a booking. Shared family emails: prefer the matching name, then phone."""
    rows = clients_db.list_by_email(email)
    if len(rows) <= 1:
        return rows[0] if rows else None
    nm = (name or '').strip().lower()
    for r in rows:
        if nm and nm == str(r.get('display_name') or '').strip().lower():
            return r
    from app.messaging.notify import normalize_e164
    ph = normalize_e164(phone) if phone else ''
    for r in rows:
        if ph and ph == r.get('phone_e164'):
            return r
    return rows[0]

class BookRequest(BaseModel):
    event_type: str
    start: str
    duration_minutes: int | None = None
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str = Field(default='', max_length=40)
    notes: str = Field(default='', max_length=500)
    sms_consent: bool | None = None
    notify_emails: str = Field(default='', max_length=400)
    source: str = Field(default='web', max_length=20)
    verify_token: str = Field(default='', max_length=800)
    send_email: bool = True
    cipher_envelope_id: str = Field(default='', max_length=80)

class VerifyRequest(BaseModel):
    email: str = ''
    phone: str = ''
    full_name: str = ''
    first_name: str = ''
    last_name: str = ''
    dob: str = ''
    portal: bool = False
    sms_consent: str = ''

def _durations(et: dict[str, Any]) -> list[int]:
    d = et.get('duration_minutes', 30)
    if isinstance(d, list):
        return [int(x) for x in d]
    return [int(d)]

def _snap_half_hour(dt: datetime) -> datetime:
    """Force visit starts onto :00 or :30 in the event timezone."""
    total = dt.hour * 60 + dt.minute
    if dt.second >= 30:
        total += 1
    snapped = (total + 15) // 30 * 30
    (extra_days, snapped) = divmod(snapped, 24 * 60)
    (hour, minute) = divmod(snapped, 60)
    out = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if extra_days:
        out = out + timedelta(days=extra_days)
    return out

def _price_label(et: dict[str, Any], dur: int, cfg: dict[str, Any] | None=None) -> str:
    labels = et.get('price_labels') or {}
    if str(dur) in labels:
        return str(labels[str(dur)])
    if dur in labels:
        return str(labels[dur])
    by = et.get('price_by_duration') or {}
    if dur in by or str(dur) in by:
        amt = by.get(dur, by.get(str(dur)))
        if amt == 0 or amt == '0':
            return 'No charge'
        return f'${amt}'
    if et.get('price_label'):
        return str(et['price_label'])
    if 'price_usd' in et:
        amt = et['price_usd']
        if amt == 0 or amt == '0':
            return 'No charge'
        return f'${amt}'
    return ''

def _location_line(et: dict[str, Any], cfg: dict[str, Any]) -> str:
    loc = et.get('location') or cfg.get('location') or 'Video visit (telehealth)'
    url = et.get('location_url') or cfg.get('location_url') or video_url()
    return f'{loc} · {url}'

def _ics_response(body: bytes, filename: str, cache: str) -> Response:
    return Response(content=body, media_type='text/calendar; charset=utf-8', headers={'Content-Disposition': f'inline; filename="{filename}"', 'Cache-Control': cache})

class VerifyCodeRequest(BaseModel):
    email: str = ''
    phone: str = ''
    code: str = ''

class AccountsRequest(BaseModel):
    contact_token: str

class PickAccountRequest(BaseModel):
    contact_token: str
    ref: int
    old_token: str = ''
PHONE_WINDOW_DAYS = 92
PHONE_NOTICE_MINUTES = 14 * 60

def _phone_channel(request: Request) -> bool:
    return (request.headers.get('x-desk-channel') or '').strip().lower() == 'voice'

def _phone_slots(cfg, et, dur: int, range_start: datetime | None=None):
    return generate_slots(cfg, et, dur, range_start=range_start, days=PHONE_WINDOW_DAYS, min_notice_minutes=PHONE_NOTICE_MINUTES)

def _send_booking_notices(*, name: str, email: str, phone: str, title: str, when_label: str, event_type: str, cancel_url: str, notes: str, sms_consent: bool, price_label: str='', location_line: str='Video visit · ' + video_url(), extra_notify: list[str] | None=None, rebook_url: str='', duration_minutes: int | None=None, start_iso: str | None=None, timezone: str=timezone_name(), ics_url: str='', event_id: str='', hold_until_label: str='', intake_url: str='') -> None:
    """Run after response — Proton can take 10–60s; do not block the thanks screen."""
    patient_confirm_email(name=name, email=email, phone=phone, title=title, when_label=when_label, cancel_url=cancel_url, price_label=price_label, location_line=location_line, rebook_url=rebook_url, duration_minutes=duration_minutes, start_iso=start_iso, timezone=timezone, notes=notes, ics_url=ics_url, hold_until_label=hold_until_label, intake_url=intake_url)
    patient_confirm_sms(phone=phone, title=title, when_label=when_label, sms_consent=sms_consent, duration_minutes=duration_minutes, event_id=event_id, hold_until_label=hold_until_label, intake_url=intake_url)
    staff_notify_email(name=name, email=email, phone=phone, title=title, when_label=when_label, event_type=event_type, cancel_url=cancel_url, notes=notes, sms_consent=sms_consent, price_label=price_label, extra_notify=extra_notify)
HOLD_EVENT_TYPE = 'new-client'
from app.portal.onboarding import HOLD_PREFIX

def _plan_hold(req: 'BookRequest', start: datetime, *, phone: bool) -> dict[str, Any] | None:
    """None for existing clients (unchanged) or when the hold can't be set up (fail open)."""
    import app.portal.onboarding as onboarding
    try:
        (needed, _row) = onboarding.needs_hold(str(req.email))
        if not needed:
            return None
        row = onboarding.ensure_new_client(name=req.name, email=str(req.email), phone=req.phone or '')
    except Exception as e:
        print('hold_plan_fail', type(e).__name__, flush=True)
        return None
    try:
        intake_url = _mint_intake_url(str(req.email), req.name, client_id=str(row['id']))
    except Exception as e:
        print('hold_mint_fail', type(e).__name__, flush=True)
        intake_url = f'{SITE_PUBLIC}/forms'
    booked = datetime.now(start.tzinfo)
    deadline = onboarding.deadline_for(booked, start)
    return {'client_id': row['id'], 'intake_url': intake_url, 'booked_at': booked, 'deadline': deadline, 'deadline_iso': deadline.isoformat(), 'deadline_label': onboarding.hold_phrase(booked, deadline), 'phone': phone}

def _desk_intake_link(email: str, client_id: str='') -> str:
    """Booked client with intake unfinished: send the (per-client) intake cascade link, no hold."""
    import app.portal.onboarding as onboarding
    try:
        if onboarding.status(client_id=client_id, email=email)['complete']:
            return ''
        row = clients_db.get_by_id(client_id) if client_id else None
        return _mint_intake_url(email, str((row or {}).get('display_name') or ''), client_id=client_id)
    except Exception:
        return ''

def _onboarding_fx():
    import app.portal.onboarding as onboarding
    from app.messaging.notify import default_staff_emails, send_email, send_sms

    def staff(subject: str, body: str) -> None:
        for to in default_staff_emails():
            send_email(to, subject, body)
    return onboarding.Effects(get_event=get_event, patch_event=patch_event, delete_event=delete_event, send_email=send_email, send_sms=send_sms, staff_email=staff, rebook_url=f'{PUBLIC_BASE}/{HOLD_EVENT_TYPE}')

class ChatThreadPost(BaseModel):
    body: str
    token: str = ''
    label: str = ''
    email: str = ''
    phone: str = ''
    hear_back: str = ''
    sms_consent: bool = False
    name: str = ''
    verify_token: str = ''

class SmsBridgeEnqueue(BaseModel):
    template: str
    phone: str
    source: str = 'voice'
    idempotency_key: str = ''

class SmsBridgeAck(BaseModel):
    id: str
    status: str

def _require_bridge(request: Request) -> None:
    if not secret_ok(request.headers.get('x-bridge-secret', '')):
        raise HTTPException(401, 'unauthorized')

def _visit_details_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Prefer live Google event; fall back to token display fields."""
    title = (payload.get('t') or '').strip()
    when_label = (payload.get('w') or '').strip()
    name = (payload.get('n') or '').strip()
    email = (payload.get('m') or '').strip()
    try:
        ev = get_event(payload['c'], payload['e'])
        if ev.get('status') == 'cancelled':
            return {'title': title or 'Visit', 'when_label': when_label, 'name': name, 'email': email, 'gone': '1'}
        summary = (ev.get('summary') or title or 'Visit').strip()
        start = (ev.get('start') or {}).get('dateTime') or (ev.get('start') or {}).get('date')
        if start and (not when_label):
            try:
                dt = dateparser.isoparse(start.replace('Z', '+00:00'))
                tz = ZoneInfo(timezone_name())
                when_label = dt.astimezone(tz).strftime('%a %b %d · %I:%M %p').replace(' 0', ' ')
            except Exception:
                when_label = start
        priv = (ev.get('extendedProperties') or {}).get('private') or {}
        if priv.get('title'):
            summary = priv.get('title') or summary
        if priv.get('when_label'):
            when_label = priv.get('when_label') or when_label
        if priv.get('patient_name'):
            name = priv.get('patient_name') or name
        return {'title': summary, 'when_label': when_label, 'name': name, 'email': email, 'gone': '0'}
    except Exception:
        return {'title': title or 'Visit', 'when_label': when_label, 'name': name, 'email': email, 'gone': '0'}

class PublicTokenRequest(BaseModel):
    verify_token: str

class PublicCancelVisitRequest(BaseModel):
    verify_token: str
    event_id: str

def _verified_client(token: str) -> dict:
    from app.portal.patients import client_by_id
    try:
        payload = verify_patient_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e)) from e
    row = client_by_id(str(payload.get('id') or ''))
    if not row:
        raise HTTPException(404, 'not found')
    return row
_UPLOAD_LABEL = {'pdf': 'PDF', 'jpeg': 'JPG image', 'png': 'PNG image', 'heic': 'HEIC photo', 'docx': 'Word document', 'xlsx': 'Excel spreadsheet'}

def _upload_email(row: dict[str, Any], res: dict[str, Any], blob: bytes, note: str='') -> None:
    """Notice to the practice inbox (relay's fixed address). Attach when 8 MB or less."""
    import base64
    import app.portal.uploads as uploads
    label = _UPLOAD_LABEL.get(res.get('kind') or '', 'file')
    lines = ['A patient sent a file through the portal chat.', '', f"Client: {row.get('name') or ''} ({row.get('id') or ''})", f"Type: {label}, {res.get('size', 0) / 1048576:.1f} MB", f"Filed to chart: {res.get('chart_path') or ''}"]
    if (note or '').strip():
        lines.extend(['', 'Note:', note.strip()[:500]])
    files = []
    if len(blob) <= uploads.EMAIL_ATTACH_MAX:
        files = [{'name': res.get('name') or 'upload', 'b64': base64.b64encode(blob).decode('ascii')}]
    else:
        lines.append('The file is over 8 MB, so it is not attached here; open it in the chart.')
    try:
        (code, out) = _notify_post('/mail/send', {'subject': '[Portal] File uploaded', 'body': '\n'.join(lines), 'files': files})
        uploads.log(str(row.get('id') or ''), len(blob), str(res.get('kind') or ''), 'email_sent' if code < 400 and out.get('ok') else 'email_failed')
    except Exception:
        uploads.log(str(row.get('id') or ''), len(blob), str(res.get('kind') or ''), 'email_failed')

class CardLinkRequest(BaseModel):
    verify_token: str = ''

def _cancel_html(*, token: str='', email: str='', ready: bool=False, done: bool=False, error: str='', note: str='', title: str='', when_label: str='', name: str='', rebook_path: str='/') -> str:

    def _esc(s: str) -> str:
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    visit_card = ''
    if title or when_label or name:
        rows = []
        if title:
            rows.append(f'<div class="cancel-row"><span class="k">Visit</span><span class="v">{_esc(title)}</span></div>')
        if when_label:
            rows.append(f'<div class="cancel-row"><span class="k">When</span><span class="v">{_esc(when_label)} <span class="meta">{timezone_name()}</span></span></div>')
        if name:
            rows.append(f'<div class="cancel-row"><span class="k">Name</span><span class="v">{_esc(name)}</span></div>')
        if email:
            rows.append(f'<div class="cancel-row"><span class="k">Email</span><span class="v">{_esc(email)}</span></div>')
        visit_card = f"""<div class="cancel-visit" style="margin:12px 0 16px;padding:12px 14px;border:1px solid var(--border, #ddd);border-radius:10px">{''.join(rows)}</div>"""
    rebook_href = rebook_path if rebook_path.startswith('/') else f'/{rebook_path}'
    if done:
        body = f'\n        <p style="font-size:1.15rem;font-weight:650;margin:0 0 8px">This visit is cancelled.</p>\n        <p>It is off the calendar.</p>\n        {visit_card}\n        <p style="margin-top:12px">To reschedule, pick a new time below.</p>\n        <p style="margin-top:16px"><a class="btn" href="{_esc(rebook_href)}" style="display:inline-block;text-decoration:none">Book a new time</a></p>\n        <p style="margin-top:10px"><a class="back" href="{site_url()}/contact">All booking options</a></p>\n        '
    elif error:
        body = f'\n        <div class="msg err">{_esc(error)}</div>\n        <p style="margin-top:16px"><a class="btn secondary" href="/" style="display:inline-block;text-decoration:none">Back to booking</a></p>\n        '
    else:
        body = f'''\n        <p>You are about to cancel the following visit. This removes it from the practice calendar.</p>\n        <p class="meta" style="color:var(--muted);margin-top:8px">To reschedule: cancel here, then book a new time on the next screen.</p>\n        {visit_card or '<p class="meta" style="color:var(--muted)">Linked email: ' + _esc(email or '(on file)') + '</p>'}\n        <form method="post" action="/cancel" class="form" style="margin-top:16px">\n          <input type="hidden" name="token" value="{_esc(token)}" />\n          <button class="btn" type="submit">Yes, cancel this visit</button>\n        </form>\n        <p style="margin-top:14px"><a class="back" href="/">Keep my visit · back to booking</a></p>\n        '''
    return f"""<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1" />\n  <title>{('Visit cancelled' if done else 'Cancel visit')} · {HOST_SHORT}</title>\n  <link rel="stylesheet" href="/static/css/booker.css" />\n  <style>\n    .cancel-row {{ display:flex; gap:12px; margin:6px 0; flex-wrap:wrap; }}\n    .cancel-row .k {{ min-width:4.5rem; color:var(--muted,#666); font-size:0.85rem; }}\n    .cancel-row .v {{ font-weight:600; }}\n    .cancel-row .meta {{ font-weight:400; color:var(--muted,#666); font-size:0.85rem; }}\n  </style>\n</head>\n<body>\n  <div class="wrap">\n    <div class="brand">\n      <div class="kicker">{HOST_SHORT}</div>\n      <h1>{('Visit cancelled' if done else 'Cancel visit')}</h1>\n    </div>\n    <div class="card">\n      {body}\n    </div>\n    <p class="foot">Questions: {HOST_EMAIL} · {HOST_PHONE}</p>\n  </div>\n</body>\n</html>"""

class MailSendRequest(BaseModel):
    kind: str
    to: EmailStr
    name: str = ''
    title: str = ''
    url: str = ''
    amount: str = ''
    form_name: str = ''
    notes: str = ''
    when_label: str = ''
    duration_minutes: int | None = None
    start_iso: str = ''
    rebook_url: str = ''
    doc_title: str = ''
    due_date: str = ''

class LateSendRequest(BaseModel):
    event_id: str
    calendar_id: str = ''
    kind: str = 'late_soon'

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
    return secret_from_request(request or _current_request.get())

def _desk_session_email(request: Request | None=None) -> str:
    import app.portal.desk_auth as desk_auth
    req = request or _current_request.get()
    tok = req.cookies.get(desk_auth.COOKIE, '') if req is not None else ''
    return desk_auth.check(tok) or ''

def _staff_ok(request: Request | None=None) -> bool:
    if REMIND_SECRET and hmac.compare_digest(_staff_secret('', request), REMIND_SECRET):
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
    req = _current_request.get()
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

class DeskAuthEmail(BaseModel):
    email: str = Field(default='', max_length=200)

class DeskAuthCode(BaseModel):
    email: str = Field(default='', max_length=200)
    code: str = Field(default='', max_length=20)

class DeskAuthLegacy(BaseModel):
    secret: str = Field(default='', max_length=400)

def _set_staff_cookie(resp: Response, secret: str) -> None:
    if not secret:
        return
    resp.set_cookie(STAFF_COOKIE, secret, httponly=True, secure=True, samesite='lax', max_age=60 * 60 * 24 * 30, path='/')

def _event_mail_fields(ev: dict[str, Any], cid: str, tz: ZoneInfo) -> dict[str, Any]:
    priv = (ev.get('extendedProperties') or {}).get('private') or {}
    start_raw = (ev.get('start') or {}).get('dateTime') or ''
    end_raw = (ev.get('end') or {}).get('dateTime') or ''
    start = None
    end = None
    if start_raw:
        start = datetime.fromisoformat(start_raw.replace('Z', '+00:00')).astimezone(tz)
    if end_raw:
        end = datetime.fromisoformat(end_raw.replace('Z', '+00:00')).astimezone(tz)
    dur_raw = str(priv.get('duration_minutes') or '').strip()
    duration = int(dur_raw) if dur_raw.isdigit() else None
    if duration is None and start and end:
        duration = max(15, int((end - start).total_seconds() // 60))
    et = priv.get('event_type') or ''
    return {'event_id': ev.get('id') or '', 'calendar_id': cid, 'name': priv.get('patient_name') or '', 'email': priv.get('patient_email') or '', 'phone': priv.get('patient_phone') or '', 'sms_consent': priv.get('sms_consent') == '1', 'title': priv.get('title') or ev.get('summary') or 'Visit', 'event_type': et, 'duration_minutes': duration, 'price_label': priv.get('price_label') or '', 'when_label': start.strftime('%a %b %d · %I:%M %p').replace(' 0', ' ') if start else '', 'start_iso': start.isoformat() if start else '', 'end_iso': end.isoformat() if end else '', 'rebook_url': f'{PUBLIC_BASE}/{et}' if et else PUBLIC_BASE, 'minutes_from_start': round((datetime.now(tz) - start).total_seconds() / 60.0, 1) if start else None}

class DeskBookRequest(BaseModel):
    event_type: str = 'follow-up'
    start: str
    duration_minutes: int | None = None
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    notes: str = ''
    send_email: bool = True

class DeskSendRequest(BaseModel):
    item: str = ''
    items: list[str] = []
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    notes: str = ''
    duration_minutes: int | None = None
    send_email: bool = True

def _desk_week_payload(start: str='', days: int=7) -> dict[str, Any]:
    """Week of calendar events for the staff board (all events, not only booker)."""
    cfg = load_config()
    tz = ZoneInfo(cfg.get('timezone', timezone_name()))
    now = datetime.now(tz)
    if start:
        try:
            day0 = datetime.strptime(start[:10], '%Y-%m-%d').replace(tzinfo=tz)
        except ValueError:
            raise HTTPException(400, 'start must be YYYY-MM-DD') from None
    else:
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day0 = day0 - timedelta(days=day0.weekday())
    days = max(1, min(int(days), 14))
    day1 = day0 + timedelta(days=days)
    cal_ids = _desk_cal_ids()
    visits = []
    (t0, t1) = (day0.astimezone(), day1.astimezone())
    for cid in cal_ids:
        try:
            events = list_events(cid, t0, t1)
        except Exception as e:
            visits.append({'ok': False, 'calendar': cid, 'error': str(e)})
            continue
        for ev in events:
            if ev.get('status') == 'cancelled':
                continue
            if ev.get('start', {}).get('date') and (not ev.get('start', {}).get('dateTime')):
                fields = _event_mail_fields(ev, cid, tz)
                fields['all_day'] = True
                fields['title'] = ev.get('summary') or 'Blocked'
                visits.append(fields)
                continue
            fields = _event_mail_fields(ev, cid, tz)
            if not fields.get('name'):
                fields['title'] = ev.get('summary') or fields.get('title') or 'Busy'
            visits.append(fields)
    visits = [v for v in visits if v.get('event_id') or v.get('title')]
    visits.sort(key=lambda v: v.get('start_iso') or '')
    _attach_intake(visits)
    try:
        from app.notes.visit_flags import for_events
        flags = for_events([str(v.get('event_id') or '') for v in visits])
        for v in visits:
            v['attendance'] = flags.get(str(v.get('event_id') or ''), '')
    except Exception:
        pass
    return {'ok': True, 'start': day0.date().isoformat(), 'end': (day1 - timedelta(days=1)).date().isoformat(), 'timezone': str(tz), 'visits': visits, 'types': {k: {'title': v.get('title') or k, 'durations': _durations(v) if isinstance(v, dict) else [30]} for (k, v) in (cfg.get('event_types') or {}).items() if v.get('enabled', True)}}

def _attach_intake(visits: list[dict[str, Any]]) -> None:
    """Calendar card: hold state + intake flags for new-client visits. Never raises."""
    try:
        import app.portal.onboarding as onboarding
        holds = onboarding.holds_by_event([str(v.get('event_id') or '') for v in visits if v.get('event_type') == HOLD_EVENT_TYPE])
        for v in visits:
            h = holds.get(str(v.get('event_id') or ''))
            if not h:
                continue
            st = onboarding.status(client_id=h.get('client_id') or '', email=h.get('email') or '')
            v['hold'] = {'status': h['status'], 'deadline_at': h['deadline_at']}
            v['intake'] = {'flags': st['flags'], 'complete': st['complete'], 'missing': st['missing']}
    except Exception:
        return

class InternalIntakeRequest(BaseModel):
    form: str
    fields: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
INTAKE_FORMS = ('personal', 'consents', 'card-auth', 'roi')

def _chart_for(row: dict[str, Any]) -> str:
    from app.infra.drive_paths import LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths
    chart = paths_from_row(row).get('chart') or ''
    if not chart:
        try:
            chart = resolve_client_paths(row, _drive_list).get('chart') or ''
        except Exception:
            chart = ''
    if not chart:
        dest = person_folder_name(row)
        if dest:
            chart = f'{LIVING_ROOT}/{dest}/Chart'
    return chart
_COPIES_LOCK = threading.Lock()

def _maybe_send_copies(client_id: str) -> dict[str, Any] | None:
    """Once intake is complete, email + text the client a 30-day no-login link to their signed copies
    (and the Notice of Privacy Practices). Re-sends with a fresh link when a newer document is signed.
    Never raises; logs no PHI."""
    import app.billing.billing as billing
    import app.notes.intake_copies as intake_copies
    import app.portal.onboarding as onboarding
    try:
        with _COPIES_LOCK:
            st = onboarding.status(client_id=client_id)
            if not intake_copies.due(client_id, bool(st['complete'])):
                return None
            view = dict(client_by_id(client_id) or {})
            if not view:
                return None
            ids = [d['id'] for d in intake_copies.docs_for(client_id)]
            n = len(ids)
            billing.revoke_shares(client_id, 'docs')
            link = billing.mint_share(billing_id=client_id, client_id=client_id, source='docs', path=','.join(ids), name='Signed intake documents', kind='docs')
        url = f"{PUBLIC_BASE}/docs/{link['token']}"
        first = (str(view.get('name') or 'there').split() or ['there'])[0]
        sent = _bill_send(kind='intake_copies', view=view, fields={'name': view.get('name') or '', 'url': url, 'count': n, 'npp_url': intake_copies.NPP_URL}, sms_body=f'Hi {first}, copies of the forms you signed for {HOST_SHORT}, plus our privacy notice (private link, good for 30 days): {url}', send_email=True, send_sms=True, dry=False, key=f"docs-{link['id']}")
        print('intake_copies_sent', n, 'email', (sent.get('email') or {}).get('ok'), 'sms', (sent.get('sms') or {}).get('ok'), flush=True)
        return sent
    except Exception as e:
        print('intake_copies_fail', type(e).__name__, flush=True)
        return None

def _file_intake_pdf(sub_id: str, client_id: str, form: str, fields: dict[str, Any], meta: dict[str, Any], received_at: str) -> dict[str, Any]:
    """Render the signed submission and save it to the client's Chart/Consents (created if missing)."""
    import base64
    import app.portal.onboarding as onboarding
    from app.notes.intake_pdf import intake_pdf
    row = client_by_id(client_id) if client_id else None
    if not row:
        onboarding.set_pdf(sub_id, status='no client')
        return {'ok': False, 'detail': 'no client'}
    try:
        tz = ZoneInfo(load_config().get('timezone', timezone_name()))
        when = datetime.fromisoformat(received_at).astimezone(tz)
        link = meta.get('linkAuth') or {}
        pdf = intake_pdf(form=form, fields=fields, received_at=when.strftime('%Y-%m-%d %I:%M:%S %p CT'), ip=str(meta.get('ip') or ''), user_agent=str(meta.get('userAgent') or ''), link_verified=bool(link.get('verified')), link_email=str(link.get('email') or ''), client_name=row.get('name') or '')
        chart = _chart_for(row)
        if not chart:
            raise HTTPException(400, 'no chart folder for this client')
        folder = _ensure_visit_notes(chart, 'Consents')
        given = (row.get('given_name') or '').strip()
        family = (row.get('family_name') or '').strip()
        ini = (given[:1] + family[:1]).upper() if given and family else ''
        stem = f'{when.date().isoformat()} {ini} {form}' if ini else f'{when.date().isoformat()} {form}'
        dest = folder + '/' + _unique_pdf_name(folder, stem)
        (code, saved) = _notify_post('/drive/save', {'path': dest, 'b64': base64.b64encode(pdf).decode('ascii')})
        if code >= 400 or not saved.get('ok'):
            raise HTTPException(502, 'drive save failed')
        onboarding.set_pdf(sub_id, path=dest, status='saved')
        _maybe_send_copies(client_id)
        return {'ok': True, 'path': dest}
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else type(e).__name__
        onboarding.set_pdf(sub_id, status=f'failed: {str(detail)[:80]}')
        print('intake_pdf_fail', form, str(detail)[:80], flush=True)
        return {'ok': False, 'detail': str(detail)[:80]}

def _check_client_bg(client_id: str) -> list[str]:
    import app.portal.onboarding as onboarding
    try:
        return onboarding.check_client(_onboarding_fx(), client_id=client_id)
    except Exception as e:
        print('hold_check_fail', type(e).__name__, flush=True)
        return []

class HoldTimesRequest(BaseModel):
    deadline_minutes: float
    remind_minutes: float | None = None

class InternalCardRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    customer_id: str = ''
    payment_method_id: str = ''
    brand: str = ''
    last4: str = ''
    exp_month: int | None = None
    exp_year: int | None = None

def _intake_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Per-client intake flags for the desk card. Never raises."""
    try:
        import app.notes.intake_copies as intake_copies
        import app.portal.onboarding as onboarding
        if not (row.get('id') or row.get('email')):
            return {}
        st = onboarding.status(client_id=str(row.get('id') or ''), email=str(row.get('email') or ''))
        holds = onboarding.holds_for_client(st['client_id']) if st['client_id'] else []
        open_hold = next((h for h in holds if h['status'] == 'held'), None)
        return {'flags': st['flags'], 'complete': st['complete'], 'missing': st['missing'], 'forms': st['forms'], 'copies': intake_copies.summary(st['client_id']) if st['client_id'] else None, 'hold': {k: open_hold[k] for k in ('event_id', 'when_label', 'deadline_at', 'reminded_at')} if open_hold else None}
    except Exception:
        return {}

def _form_status(intake: dict[str, Any], form: str) -> str:
    f = ((intake or {}).get('forms') or {}).get(form) if form else None
    if not intake or not form:
        return 'unknown'
    return 'done' if f and f.get('done') else 'not yet'

def _desk_cal_ids() -> set[str]:
    cal_id = os.environ.get('GOOGLE_CALENDAR_ID') or DEFAULT_CALENDAR
    ids = {cal_id}
    for et in (load_config().get('event_types') or {}).values():
        if et.get('google_calendar_id'):
            ids.add(et['google_calendar_id'])
    if using_caldav() and ids:
        return {next(iter(ids))}
    return ids

def _refill_visits(client: dict[str, Any]) -> list[dict[str, Any]]:
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    c = dict(client)
    c.setdefault('name', c.get('display_name') or '')
    c.setdefault('phone', c.get('phone_e164') or '')
    return _visits_for_client(c, tz)

def _wire_refills() -> None:
    import app.portal.refills as refills
    refills.VISITS_FN = _refill_visits
_wire_refills()

def _warm_client_window() -> None:
    """Desk load: pre-read the drawer's visit window so the first client click is warm."""

    def run() -> None:
        try:
            tz = ZoneInfo(load_config().get('timezone', timezone_name()))
            _visits_for_client({}, tz)
        except Exception:
            pass
    threading.Thread(target=run, name='client-window-warm', daemon=True).start()

def _drawer_window(tz: ZoneInfo) -> tuple[datetime, datetime]:
    day = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return ((day - timedelta(days=180)).astimezone(), (day + timedelta(days=91)).astimezone())

def _warm_drawer_bg() -> None:
    """Boot: fetch the drawer window once so the first client open after a restart isn't cold."""
    import threading

    def run() -> None:
        try:
            tz = ZoneInfo(load_config().get('timezone', timezone_name()))
            (a, b) = _drawer_window(tz)
            for cid in _desk_cal_ids():
                list_events(cid, a, b)
        except Exception as e:
            print('drawer_warm_fail', type(e).__name__, flush=True)
    threading.Thread(target=run, name='drawer-warm', daemon=True).start()

def _visits_for_client(client: dict[str, Any], tz: ZoneInfo) -> list[dict[str, Any]]:
    now = datetime.now(tz)
    (tmin, tmax) = _drawer_window(tz)
    email = (client.get('email') or '').strip().lower()
    name = (client.get('name') or '').strip().lower()
    client_phone = ''.join((ch for ch in str(client.get('phone') or '') if ch.isdigit()))
    from app.portal.patients import normalize_phone as _norm_phone
    e164 = _norm_phone(client_phone) if len(client_phone) >= 10 else None
    shared = bool(email) and len(clients_db.list_by_email(email)) > 1 or (bool(e164) and len(clients_db.list_by_phone(e164)) > 1)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in _desk_cal_ids():
        try:
            events = list_events(cid, tmin, tmax)
        except Exception:
            continue
        for ev in events:
            if ev.get('status') == 'cancelled':
                continue
            fields = _event_mail_fields(ev, cid, tz)
            ev_email = (fields.get('email') or '').strip().lower()
            ev_name = (fields.get('name') or '').strip().lower()
            summary = str(ev.get('summary') or '').lower()
            named = bool(name) and (ev_name == name or (len(name) > 4 and name in summary))
            hit = bool(email and ev_email == email)
            ev_phone = ''.join((ch for ch in str(fields.get('phone') or '') if ch.isdigit()))
            if not hit and len(client_phone) >= 10 and (ev_phone[-10:] == client_phone[-10:]):
                hit = True
            if shared and (not named):
                hit = False
            if named:
                hit = True
            eid = str(fields.get('event_id') or '')
            if not hit or not eid or eid in seen:
                continue
            seen.add(eid)
            start_iso = fields.get('start_iso') or ''
            fields['upcoming'] = bool(start_iso and start_iso >= now.isoformat())
            out.append(fields)
    out.sort(key=lambda v: v.get('start_iso') or '', reverse=True)
    try:
        from app.notes.visit_flags import for_events
        flags = for_events([str(v.get('event_id') or '') for v in out])
        for v in out:
            v['attendance'] = flags.get(str(v.get('event_id') or ''), '')
    except Exception:
        pass
    return out

class DeskFeeRequest(BaseModel):
    client_id: str = ''
    fee_id: str = ''
    dos: str = ''
    amount: str = ''
    amount_cents: int | None = None
    kind: str = 'visit'
    note: str = ''
    event_id: str = ''
    charge: bool = False
    duration_minutes: int | None = None
    notify: bool = True

def _fee_cents(req: DeskFeeRequest) -> int:
    from app.billing.fees import parse_amount
    if req.amount_cents is not None:
        return int(req.amount_cents)
    return parse_amount(req.amount)

class DeskNoteSignRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    event_id: str = ''
    dos: str = ''
    name: str = ''
    dob: str = ''
    physician: str = ''
    return_visit: str = ''
    medications: str = ''
    supplements: str = ''
    recommendations: str = ''
    long_run: str = ''
    template: str = 'soap'
    data: str = ''
    subjective: str = ''
    objective: str = ''
    assessment: str = ''
    plan: str = ''
    addendum: str = ''
    duration: str = ''
    values: dict[str, str] = {}

def _scribe_link_visits(row: dict[str, Any], rs: datetime, re_: datetime) -> tuple[str, list[dict[str, Any]]]:
    """Candidate calendar visits around a recording. -> (client_id used, visits)."""
    from app.booking.visit_link import parse_ts
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    cid = str(row.get('client_id') or '')
    if not cid and row.get('email'):
        c = client_by_email(str(row['email']))
        cid = str((c or {}).get('id') or '')
    out: list[dict[str, Any]] = []
    if cid:
        c = client_by_id(cid)
        for v in _refill_visits(dict(c)) if c else []:
            out.append({'event_id': v.get('event_id'), 'client_id': cid, 'start': parse_ts(v.get('start_iso') or ''), 'end': parse_ts(v.get('end_iso') or '')})
        return (cid, out)
    for cal_id in _desk_cal_ids():
        try:
            evs = list_events(cal_id, (rs - timedelta(hours=12)).astimezone(), (re_ + timedelta(hours=12)).astimezone())
        except Exception:
            continue
        for ev in evs:
            if ev.get('status') == 'cancelled':
                continue
            f = _event_mail_fields(ev, cal_id, tz)
            if not (f.get('start_iso') and f.get('end_iso')):
                continue
            out.append({'event_id': f.get('event_id'), 'client_id': '', 'start': parse_ts(f.get('start_iso') or ''), 'end': parse_ts(f.get('end_iso') or '')})
    return ('', out)

def link_scribe_row(row: dict[str, Any]) -> str:
    import app.notes.scribe_drafts as scribe_drafts
    from app.booking.visit_link import match, multi_visit, recording_window
    (rs, re_) = recording_window(row)
    if not rs:
        scribe_drafts.set_link(row['id'], '', 'none')
        return 'none'
    (cid, visits) = _scribe_link_visits(row, rs, re_)
    if multi_visit(rs, re_, visits):
        scribe_drafts.set_link(row['id'], '', 'multi')
        return 'multi'
    (eid, status) = match(rs, re_, cid, visits)
    scribe_drafts.set_link(row['id'], eid, status)
    return status

def backfill_scribe_links(only_unset: bool=True) -> dict[str, int]:
    import app.notes.scribe_drafts as scribe_drafts
    counts = {'linked': 0, 'ambiguous': 0, 'none': 0, 'multi': 0}
    for r in scribe_drafts.rows_for_link(only_unset):
        st = link_scribe_row(r)
        counts[st] = counts.get(st, 0) + 1
    return counts

def _ct_day_bounds(event_start_iso: str) -> tuple[datetime, datetime] | None:
    from app.booking.visit_link import parse_ts
    st = parse_ts(event_start_iso)
    if not st:
        return None
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    local = st.astimezone(tz)
    d0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc))

class DeskNoteUpdateRequest(BaseModel):
    client_id: str = ''
    event_id: str = ''
    transcript_id: str = ''
    fields: dict[str, str] = {}
    transcript: str = ''

def _booked_meet_again(client_id: str, event_id: str, visit_day: str) -> str:
    """Date (+ clock time and booked length) of the next visit, for the Meet again line. Empty if none."""
    from app.notes.note_carry import meet_again_line
    (day, minutes) = _next_booked_visit(client_id, event_id, visit_day)
    anchor = visit_day if len((visit_day or '').strip()) >= 10 else datetime.now().date().isoformat()
    (line, _src) = meet_again_line(anchor, day, '', next_minutes=minutes)
    return line

def _next_booked_day(client_id: str, event_id: str, visit_day: str) -> str:
    return _next_booked_visit(client_id, event_id, visit_day)[0]

def _next_booked_visit(client_id: str, event_id: str, visit_day: str) -> tuple[str, int]:
    """Earliest later visit on the calendar. Start time when known, plus its booked length in minutes."""
    row = client_by_id(client_id) if client_id else None
    if not row:
        return ('', 0)
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    try:
        visits = _visits_for_client(row, tz)
    except Exception:
        return ('', 0)
    best = ''
    best_iso = ''
    best_minutes = 0
    for v in visits:
        if str(v.get('event_id') or '') == (event_id or ''):
            continue
        start = str(v.get('start_iso') or '')
        day = start[:10]
        if len(day) != 10 or (visit_day and day <= visit_day[:10]):
            continue
        if not best or day < best:
            best = day
            best_iso = start
            best_minutes = _visit_minutes(v)
    return (best_iso, best_minutes)

def _visit_minutes(visit: dict[str, Any]) -> int:
    """Booked length of a calendar visit in minutes; 0 when the end is unknown."""
    try:
        start = datetime.fromisoformat(str(visit.get('start_iso') or '').replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(visit.get('end_iso') or '').replace('Z', '+00:00'))
    except ValueError:
        return 0
    mins = int(round((end - start).total_seconds() / 60))
    return mins if 5 <= mins <= 600 else 0

class AvsShareRequest(BaseModel):
    note_id: str
    send_email: bool = True
    send_sms: bool = True
    to_email: str = ''
    to_phone: str = ''

class AvsRevokeRequest(BaseModel):
    note_id: str

class DeskNoteTemplateSave(BaseModel):
    id: str
    title: str = ''
    body: str = ''
    sections: list[dict] = []

class DeskNoteTemplateOrder(BaseModel):
    ids: list[str] = []

class DeskPhraseSave(BaseModel):
    trigger: str = ''
    id: str = ''
    title: str = ''
    body: str = ''

class DeskPhraseOrder(BaseModel):
    ids: list[str] = []

class DeskNoteDraftRequest(BaseModel):
    transcript: str = ''
    name: str = ''
    dos: str = ''
    duration: str = ''
SCRIBE_NOTE_URL = os.environ.get('SCRIBE_NOTE_URL', 'http://127.0.0.1:8795/note')

class DeskScribeIngestRequest(BaseModel):
    transcript: str = ''
    note: str = ''
    title: str = ''
    template: str = 'avs'
    duration_minutes: int = 0
    client_id: str = ''
    email: str = ''
    name: str = ''
    room: str = ''
    ended_at: str = ''
    started_at: str = ''

def _scribe_ingest_authorized(request: Request) -> None:
    """Staff header or dedicated edge ingest token. Query secrets are not accepted."""
    tok = (os.environ.get('SCRIBE_INGEST_TOKEN') or '').strip()
    hdr = ''
    try:
        hdr = (request.headers.get('X-Scribe-Ingest-Token') or '').strip()
    except Exception:
        logging.getLogger('desk.scribe').exception('scribe ingest header read failed')
        hdr = ''
    if tok and hdr and hmac.compare_digest(hdr, tok):
        return
    _require_staff(request=request)

class DeskVisitFlag(BaseModel):
    event_id: str = ''
    flag: str = ''

class DeskVisitCancel(BaseModel):
    event_id: str = ''
    calendar_id: str = ''
    notify: bool = False

class DeskVisitMove(BaseModel):
    event_id: str = ''
    calendar_id: str = ''
    start: str = ''
    notify: bool = False

def _visit_contact(ev: dict[str, Any], fields: dict[str, Any]) -> tuple[str, str, bool]:
    """Email, phone, and whether a text is allowed. No guess when two people match."""
    email = str(fields.get('email') or '').strip()
    phone = str(fields.get('phone') or '').strip()
    opted_out = False
    if not email or not phone:
        from app.booking.cal_reminders import match_client
        (client, how) = match_client(str(fields.get('name') or ''), str(ev.get('summary') or fields.get('title') or ''))
        if how == 'matched' and client:
            email = email or str(client.get('email') or '')
            phone = phone or str(client.get('phone_e164') or client.get('phone') or '')
            raw_c = client.get('sms_consent')
            opted_out = raw_c in (0, '0') and str(client.get('sms_consent_source') or '') == 'spruce'
    return (email, phone, not opted_out)

class CipherDrainRequest(BaseModel):
    id: str = ''

class DeskAgentRequest(BaseModel):
    messages: list[dict[str, str]] = []

class DeskPracticeSave(BaseModel):
    display_name: str = ''
    legal_name: str = ''
    clinician_name: str = ''
    clinician_title: str = ''
    address: str = ''
    phone: str = ''
    fax: str = ''
    email: str = ''
    website: str = ''
    meet: str = ''
    ein: str = ''
    npi: str = ''
    licenses: list[dict] = []
    billing_role: str = ''
    theme: dict = {}
    page_titles: dict = {}
    clinician_callback: str = ''
    call_handset: str = ''

class DeskServiceSave(BaseModel):
    slug: str = ''
    title: str = ''
    enabled: bool = True
    public: bool = True
    duration_minutes: Any = None
    calendar_duration_minutes: int | None = None
    price_usd: float | None = None
    price_by_duration: dict | None = None
    weekly_hours: dict | None = None
    slot_interval_minutes: int = 30
    minimum_notice_minutes: int = 120
    booking_window_days: int = 30
    creating: bool = False

class DeskCallRequest(BaseModel):
    to: str = ''
    client_id: str = ''
    name: str = ''
    sid: str = ''
    handset: str = ''

class DeskSuperbillRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    dos: str = ''
    pos: str = '10'
    address: str = ''
    diagnoses: list[dict] = []
    lines: list[dict] = []
    items: list[str] = []
    custom_fields: list[dict] = []
    preview: bool = False

class SuperbillSendRequest(BaseModel):
    superbill_id: str
    send_email: bool = True
    send_sms: bool = True

def _bill_client(client_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    import app.infra.clients_db as clients_db
    raw = clients_db.get_by_id((client_id or '').strip()) if client_id else None
    if not raw:
        raise HTTPException(404, 'client not found')
    return (raw, client_by_id(raw['id']) or {})

def _bill_billing_folder(view: dict[str, Any]) -> str:
    from app.infra.drive_paths import BILLING_CHILD, LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths
    billing = paths_from_row(view).get('billing') or ''
    if not billing:
        try:
            billing = resolve_client_paths(view, _drive_list).get('billing') or ''
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, 'Could not read the billing folder') from e
    if not billing:
        dest = person_folder_name(view)
        if dest:
            billing = f'{LIVING_ROOT}/{dest}/{BILLING_CHILD}'
    return billing

def _bill_drive_save(dest: str, data: bytes) -> None:
    import base64
    (code, saved) = _notify_post('/drive/save', {'path': dest, 'b64': base64.b64encode(data).decode('ascii')})
    if code >= 400 or not saved.get('ok'):
        raise HTTPException(502, 'drive save failed')

class BillConfirmRequest(BaseModel):
    client_id: str = ''
    event_id: str = ''
    dos: str = ''
    visit_code: str = ''
    confirm_code: str = ''
    diagnoses: list[Any] = []
    save_dx: bool = False
    payment: str = 'link'
    confirm_charge: bool = False
    send_email: bool = True
    send_sms: bool = False
    share_superbill: bool = True
    dry_run: bool = True
    specimen: bool = False
    share_ttl_seconds: int | None = None

class BillSuperbillRequest(BaseModel):
    billing_id: str = ''
    diagnoses: list[Any] = []
    save_dx: bool = False
    share_superbill: bool = True
    send_email: bool = True
    send_sms: bool = False
    specimen: bool = False
    share_ttl_seconds: int | None = None

class BillShareRequest(BaseModel):
    billing_id: str = ''
    pay_link: bool = False
    send_email: bool = True
    send_sms: bool = False
    share_ttl_seconds: int | None = None

def _bill_send(*, kind: str, view: dict[str, Any], fields: dict[str, Any], sms_body: str, send_email: bool, send_sms: bool, dry: bool, key: str) -> dict[str, Any]:
    import app.billing.billing as billing
    from app.messaging.email_templates import render_named
    from app.messaging.notify import normalize_e164
    from app.messaging.spruce_api import send_spruce_sms
    out: dict[str, Any] = {'email': {'skipped': True}, 'sms': {'skipped': True}}
    email = str(view.get('email') or '')
    phone = str(view.get('phone') or '')
    if send_email:
        if not email:
            out['email'] = {'ok': False, 'detail': 'no email on file'}
        elif dry:
            subject = render_named(kind, {'email': email, **fields})[0]
            out['email'] = {'ok': True, 'dry_run': True, 'sent': False, 'subject': subject}
        else:
            r = send_named_email(kind=kind, to=email, fields=fields)
            out['email'] = {'ok': bool(r.get('ok')), 'sent': bool(r.get('ok'))}
    if send_sms:
        e164 = normalize_e164(phone) or ''
        if not billing.sms_enabled():
            out['sms'] = {'ok': False, 'skipped': True, 'detail': 'SMS is off (BILLING_SMS unset or Spruce not configured)'}
        elif not e164:
            out['sms'] = {'ok': False, 'detail': 'no phone on file'}
        elif dry:
            out['sms'] = {'ok': True, 'dry_run': True, 'sent': False, 'via': 'spruce_api', 'to': '•••' + e164[-4:], 'text': sms_body[:480]}
        else:
            r = send_spruce_sms(e164, sms_body, idempotency_key=key)
            out['sms'] = {'ok': bool(r.get('ok')), 'sent': bool(r.get('sent')), 'via': 'spruce_api', 'request_id': r.get('request_id') or '', 'detail': r.get('detail') or ''}
    return out

def _bill_share(rec: dict[str, Any], view: dict[str, Any], *, send_email: bool, send_sms: bool, ttl: int | None) -> dict[str, Any]:
    import app.billing.billing as billing
    from app.billing.superbill_pdf import superbill_filename
    if rec.get('superbill_status') != 'saved':
        raise HTTPException(409, 'No saved superbill to share yet.')
    dry = bool(rec.get('dry_run'))
    seconds = billing.SHARE_TTL_SECONDS
    if ttl is not None and dry:
        seconds = int(ttl)
    revoked = billing.revoke_shares(rec['id'])
    link = billing.mint_share(billing_id=rec['id'], client_id=rec['client_id'], source=rec.get('superbill_source') or 'drive', path=rec.get('superbill_path') or '', name=superbill_filename(rec['dos']), ttl_seconds=seconds)
    url = f"{PUBLIC_BASE}/sb/{link['token']}"
    inv = rec.get('invoice') or {}
    first = (str(view.get('name') or 'there').split() or ['there'])[0]
    sent = _bill_send(kind='superbill', view=view, fields={'name': view.get('name') or '', 'title': inv.get('label') or 'Visit', 'amount': inv.get('amount_pretty') or '', 'url': url}, sms_body=f'Hi {first}, your superbill is ready (link good for 30 days): {url} — {HOST_SHORT}', send_email=send_email, send_sms=send_sms, dry=dry, key=f"sb-{link['id']}")
    return {'ok': True, 'url': url, 'share_id': link['id'], 'expires_iso': link['expires_iso'], 'replaced': revoked, **sent}

def _bill_superbill(rec: dict[str, Any], raw: dict[str, Any], view: dict[str, Any], *, specimen: bool) -> dict[str, Any]:
    import app.billing.billing as billing
    from app.infra.practice import asset_bytes, load as load_practice, next_statement
    from app.billing.superbill_pdf import superbill_filename, superbill_pdf
    dry = bool(rec.get('dry_run'))
    prac = load_practice()
    missing = billing.letterhead_missing(prac)
    dx = rec.get('dx') or []
    if missing and (not (dry and specimen)):
        msg = billing.refusal_message(missing)
        billing.update_billing(rec['id'], superbill_status='refused', superbill_detail=msg[:400])
        return {'ok': False, 'status': 'refused', 'missing': missing, 'detail': msg}
    if not dx:
        msg = 'Superbill not built: add at least one ICD-10 code. The invoice is not affected.'
        billing.update_billing(rec['id'], superbill_status='refused', superbill_detail=msg)
        return {'ok': False, 'status': 'refused', 'missing': ['ICD-10 code'], 'detail': msg}
    statement = next_statement() if not dry else 'DRY-' + rec['id'][:6].upper()
    doc = billing.superbill_doc(prac=prac, client=view, rec=rec, dx=dx, statement=statement, paid=rec.get('payment_status') == 'paid', specimen=bool(missing))
    doc['logo'] = asset_bytes('logo')
    pdf = superbill_pdf(doc)
    fname = superbill_filename(rec['dos'])
    if dry:
        local = f"{rec['id']}.pdf"
        (billing.dryrun_dir() / local).write_bytes(pdf)
        (source, path) = ('local', local)
    else:
        folder = _bill_billing_folder(view)
        if not folder:
            msg = 'Superbill not saved: no billing folder for this client.'
            billing.update_billing(rec['id'], superbill_status='failed', superbill_detail=msg)
            return {'ok': False, 'status': 'failed', 'detail': msg}
        _drive_mkdir(folder)
        path = folder.rstrip('/') + '/' + fname
        _bill_drive_save(path, pdf)
        source = 'drive'
    billing.update_billing(rec['id'], superbill_status='saved', superbill_source=source, superbill_path=path, superbill_detail='specimen (dry run)' if missing else '', statement=statement)
    return {'ok': True, 'status': 'saved', 'source': source, 'path': path, 'name': fname, 'statement': statement, 'bytes': len(pdf), 'specimen': bool(missing)}

def _share_page(message: str, status: int, title: str='Superbill link') -> HTMLResponse:
    from html import escape
    from app.infra.practice import load as load_practice
    who = str(load_practice().get('display_name') or '')
    body = f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>{escape(title)}</title><body style='font-family:Inter,system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#111'><img src='/api/practice/logo' alt='' style='height:56px'><p style='color:#2f57a5;margin:.4rem 0'>{escape(who)}</p><h1 style='font-size:1.2rem'>{escape(title)}</h1><p>{escape(message)}</p></body>"
    return HTMLResponse(body, status_code=status, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})
_PAY_CLOSED = {'paid': 'This invoice is paid. Thank you!', 'void': 'This invoice is closed. No payment is needed.', 'uncollectible': 'This invoice is closed. Contact the practice with any questions.'}

def _last_note_listing(vn_path: str) -> dict[str, Any]:
    if not vn_path:
        return {'ok': False, 'status': 'none', 'hint': 'After-visit summary will land in Chart / Visit notes.'}
    try:
        data = _drive_list(vn_path)
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
        with urlopen(req, timeout=90) as resp:
            json.loads(resp.read().decode() or '{}')
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    except URLError as e:
        raise HTTPException(502, 'Drive is not reachable') from e

def _folder_names(path: str) -> dict[str, str] | None:
    """{lowercase name: real name} of child folders, or None when the folder is missing/unreadable."""
    data = _drive_list(path)
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
    kids = _folder_names(chart)
    if kids is None:
        if not client_dir.startswith(LIVING_ROOT + '/'):
            raise HTTPException(400, 'chart folder not found (archive tree is never created)')
        if _folder_names(client_dir) is None:
            if _folder_names(LIVING_ROOT) is None:
                raise HTTPException(502, 'Drive is not reachable right now; nothing was created')
            _drive_mkdir(client_dir)
            _drive_mkdir(client_dir + '/' + BILLING_CHILD)
        _drive_mkdir(chart)
        for kid in CHART_SUBFOLDERS:
            _drive_mkdir(chart + '/' + kid)
        if want not in CHART_SUBFOLDERS:
            _drive_mkdir(chart + '/' + want)
        return chart + '/' + want
    have = kids.get(want.lower())
    if have:
        return chart + '/' + have
    _drive_mkdir(chart + '/' + want)
    return chart + '/' + want

def _unique_pdf_name(folder: str, stem: str) -> str:
    """`stem.pdf`, or `stem-2.pdf`, `stem-3.pdf`… when a same-day note is already there (never overwrite)."""
    data = _drive_list(folder)
    names = {str(i.get('name') or '').lower() for i in data.get('items') or []}
    taken = lambda n: n.lower() in names or n[:-4].lower() + ' chart.pdf' in names
    (name, k) = (f'{stem}.pdf', 2)
    while taken(name):
        (name, k) = (f'{stem}-{k}.pdf', k + 1)
    return name

def _drive_list(path: str) -> dict[str, Any]:
    (code, body, _) = _notify_get('/drive/list?path=' + quote(path or '/', safe='/'))
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
            paths = existing_paths(row, _drive_list)
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
        with urlopen(req, timeout=90) as resp:
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
        with urlopen(req, timeout=120) as resp:
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
        with urlopen(req, timeout=180) as resp:
            saved = json.loads(resp.read().decode() or '{}')
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    except URLError as e:
        raise HTTPException(502, 'Drive is not reachable') from e
    if not saved.get('ok'):
        raise HTTPException(502, 'drive save failed')
    return saved

class MailReplyFile(BaseModel):
    name: str = 'file'
    b64: str = ''

class MailReplyRequest(BaseModel):
    id: str
    body: str = ''
    files: list[MailReplyFile] = []

class DriveSaveRequest(BaseModel):
    path: str
    text: str = ''
    b64: str = ''

class DriveShareRequest(BaseModel):
    path: str
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    send_email: bool = True
    send_sms: bool = False

def _resolve_send_ids(req: DeskSendRequest) -> list[str]:
    raw = list(req.items or [])
    if req.item:
        raw.append(req.item)
    ids: list[str] = []
    for x in raw:
        if x in SEND_ITEMS and x not in ids:
            ids.append(x)
    if 'intake' in ids:
        ids = [x for x in ids if x not in ('personal', 'consents', 'card')]
    return ids

def _build_send_links(ids: list[str], *, name: str, email: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for item_id in ids:
        spec = SEND_ITEMS[item_id]
        if spec.get('mint'):
            try:
                url = _mint_intake_url(email, name, spec.get('forms'))
            except HTTPException:
                url = f'{SITE_PUBLIC}/forms'
        else:
            url = str(spec.get('url') or f'{SITE_PUBLIC}/forms')
        links.append({'id': item_id, 'label': spec['label'], 'url': url})
    return links

def _demo_on() -> bool:
    return (os.environ.get('DEMO') or '').strip().lower() in ('1', 'true', 'yes', 'on')

def _paint_page(html: str) -> str:
    """Fill the example identity from config when a page is served."""
    tz = timezone_name()
    if 'data-tz=' not in html:
        html = html.replace('<html', '<html data-tz="' + tz + '"', 1)
    pairs = (('https://book.' + 'example.com', book_url()), ('https://meet.' + 'example.com', video_url()), ('https://live.' + 'example.com', live_origin()), ('https://portal.' + 'example.com', portal_origin()), ('https://www.' + 'example.com', www_origin()), ('https://' + 'example.com', site_url()), ('go.' + 'example.com', short_host()), ('book.' + 'example.com', book_host()), ('meet.' + 'example.com', video_host()), ('desk@' + 'example.com', HOST_EMAIL), ('555-' + '010-0100', HOST_PHONE), ('the ' + 'clinician', HOST_SHORT), ('America/' + 'Chicago', tz))
    for (old, new) in pairs:
        if old != new and old in html:
            html = html.replace(old, new)
    return html

def _page(name: str, cache: bool=True):
    path = STATIC / name
    if not path.exists():
        raise HTTPException(404)
    headers = {} if cache else {'Cache-Control': 'no-store'}
    if path.suffix == '.html':
        return Response(content=_paint_page(path.read_text(encoding='utf-8')), media_type='text/html; charset=utf-8', headers=headers)
    return FileResponse(path, headers=headers)
_BOOK_DESCRIPTIONS = {'new-client': 'Book a new-patient video evaluation (about 90–100 minutes, $600) with Example Clinician, DO, MBA. Times are shown in Central Time.', 'follow-up': 'Book a follow-up video visit with Example Clinician, DO, MBA (current patients). Times are shown in Central Time.'}

def _book_page(slug: str) -> HTMLResponse:
    """book.html with the visit title, meta description and JSON-LD rendered on the server (JS still hydrates the rest)."""
    import html as _html
    path = STATIC / 'book.html'
    if not path.exists():
        raise HTTPException(404)
    raw = path.read_text(encoding='utf-8')
    et = get_event_type(load_config(), slug) if slug else None
    title = str((et or {}).get('title') or 'Book a visit').strip() or 'Book a visit'
    desc = _BOOK_DESCRIPTIONS.get(slug, f'Book a {title.lower()} with {HOST_SHORT} by secure video. Times are shown in Central Time.')
    page_url = f'{book_url()}/{slug}' if slug else book_url() + '/'
    ld = {'@context': 'https://schema.org', '@type': 'WebPage', '@id': page_url + '#webpage', 'url': page_url, 'name': f'{title} · {HOST_SHORT}', 'description': desc, 'inLanguage': 'en-US', 'isPartOf': {'@id': site_url() + '/#website'}, 'about': {'@id': site_url() + '/#practice'}, 'provider': {'@type': ['MedicalBusiness', 'MedicalOrganization'], '@id': site_url() + '/#practice', 'name': HOST_SHORT, 'url': site_url(), 'telephone': '+1-' + HOST_PHONE}, 'potentialAction': {'@type': 'ReserveAction', 'target': page_url}}
    ld_json = json.dumps(ld).replace('<', '\\u003c')
    head_extra = f'  <meta name="description" content="{_html.escape(desc)}" />\n  <link rel="canonical" href="{_html.escape(page_url)}" />\n  <script type="application/ld+json">{ld_json}</script>\n'
    out = raw.replace('<title>Book · ' + HOST_SHORT + '</title>', f'<title>{_html.escape(title)} · {HOST_SHORT}</title>', 1)
    out = out.replace('<h1 id="book-title">Book a visit</h1>', f'<h1 id="book-title">{_html.escape(title)}</h1>', 1)
    out = out.replace('</head>', head_extra + '</head>', 1)
    if slug == 'follow-up':
        start = out.find('<label class="check">')
        end = out.find('</label>', start)
        if start != -1 and end != -1 and ('name="sms_consent"' in out[start:end]):
            out = out[:start] + out[end + len('</label>'):]
        out = out.replace('placeholder="Optional — needed for SMS"', 'placeholder="Optional"', 1)
    return HTMLResponse(_paint_page(out), headers={'Cache-Control': 'no-store'})
_NO_INDEX = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer'}
_C_PAGE = "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=robots content='noindex,nofollow'><meta name=referrer content=no-referrer><meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title></head><body style='font-family:system-ui;max-width:32em;margin:3em auto;padding:0 1em;line-height:1.5'><h1 style='font-size:1.3em'>{title}</h1>{body}</body></html>"

def _c_expired() -> HTMLResponse:
    return HTMLResponse(_C_PAGE.format(title='Log in to read your messages', body=f"<p>This link has expired or was replaced. Your messages are safe.</p><p><a href='{SITE_PUBLIC}/portal' style='font-size:1.1em'>Log in with a code</a> to read them.</p>"), status_code=410, headers=_NO_INDEX)

def _c_ip(request: Request | None) -> str:
    if request is None:
        return ''
    return request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else '')

class BookPrefillRequest(BaseModel):
    token: str

class DeskBookLinkRequest(BaseModel):
    client_id: str
    duration: int | None = None
    send_email: bool = False
    dry_run: bool = False

def _desk_book_link(client_id: str, *, duration: int | None=None, send_email: bool=False, dry_run: bool=False) -> dict[str, Any]:
    row = clients_db.get_by_id(str(client_id or '')) if client_id else None
    if not row:
        return {'ok': False, 'error': 'client not found'}
    link = _book_link(row['id'], duration=duration)
    out: dict[str, Any] = {'ok': True, 'url': link['url'], 'email': {'skipped': True}}
    if send_email:
        to = str(row.get('email') or '')
        if not to:
            out['email'] = {'ok': False, 'detail': 'no email on file'}
        elif dry_run:
            out['email'] = {'ok': True, 'dry_run': True, 'sent': False}
        else:
            from app.messaging.notify import send_named_email
            r = send_named_email(kind='book_link', to=to, fields={'name': row.get('display_name') or '', 'url': link['url'], 'title': 'a follow-up visit'})
            out['email'] = {'ok': bool(r.get('ok')), 'sent': bool(r.get('ok'))}
    return out

class DeskBookLinkRevoke(BaseModel):
    client_id: str
__all__ = [k for k in globals() if not k.startswith('__')]
