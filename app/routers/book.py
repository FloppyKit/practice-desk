from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def health():
    cfg = runtime.load_config()
    return {'status': 'ok', 'version': runtime.VERSION, 'git_sha': runtime.GIT_SHA, 'types': list(cfg.get('event_types', {}).keys()), 'timezone': cfg.get('timezone'), 'public_base': runtime.PUBLIC_BASE, 'cancel': True, 'reminders': True, 'reminder_email': ['24h', '1h'], 'reminder_sms_minutes': float(runtime.os.environ.get('REMINDER_SMS_MINUTES', '30')), 'suite': True, 'sms_enabled': runtime.ENABLE_SMS, 'sms_bridge': 'on' if runtime.spruce_api_enabled() or runtime.bridge_enabled() else 'off', 'desk_call': bool(runtime.os.environ.get('TWILIO_ACCOUNT_SID', '').strip() and runtime.os.environ.get('TWILIO_AUTH_TOKEN', '').strip()), 'patient_verify': runtime.supabase_ready(), 'clients': 'sqlite', 'calendar': runtime.os.environ.get('CALENDAR_BACKEND', 'google'), 'calendar_sot': 'caldav' if runtime.using_caldav() else 'google', 'calendar_mirror': runtime.os.environ.get('CALENDAR_MIRROR_GOOGLE', '').strip().lower() in ('1', 'true', 'yes') or (runtime.os.environ.get('CALENDAR_BACKEND') or '').strip().lower() == 'both', 'ics_busy': f'{runtime.PUBLIC_BASE}/cal/busy.ics'}

def cal_subscribe_page():
    busy = f'{runtime.PUBLIC_BASE}/cal/busy.ics'
    html = f'<!doctype html>\n<html lang="en"><head>\n<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>\n<title>Subscribe · Example Practice</title>\n<link rel="stylesheet" href="/static/css/booker.css"/>\n</head><body class="page">\n<main class="card" style="max-width:36rem;margin:3rem auto;padding:1.5rem">\n<h1>Practice calendar</h1>\n<p class="muted">Paste this URL into Proton Calendar → Other calendars → Add calendar from URL. Same link works in Apple or Google as “from URL”.</p>\n<p><code id="u">{busy}</code></p>\n<p><button type="button" id="c">Copy busy feed</button></p>\n<p class="muted">This public feed is <strong>busy-only</strong> — times, no names, no notes. A titled staff feed exists behind a separate secret (not on this page).</p>\n</main>\n<script>\ndocument.getElementById("c").onclick=()=>navigator.clipboard.writeText(document.getElementById("u").textContent);\n</script>\n</body></html>'
    return runtime.HTMLResponse(html)

def ics_busy():
    from app.booking.ics import build_ics
    return runtime._ics_response(build_ics(busy_only=True, calendar_name='Example Practice (busy)'), 'example-busy.ics', 'public, max-age=300')

def ics_practice(token: str=runtime.Query('')):
    import hmac
    if not runtime.ICS_FEED_SECRET or not hmac.compare_digest(token, runtime.ICS_FEED_SECRET):
        raise runtime.HTTPException(status_code=404, detail='not found')
    from app.booking.ics import build_ics
    return runtime._ics_response(build_ics(busy_only=False, calendar_name='Example Practice practice'), 'example-practice.ics', 'private, max-age=120')

def ics_visit(token: str=runtime.Query('')):
    """One-event invite. Same token as cancel — patient already has it."""
    from datetime import timedelta
    from app.messaging.email_templates import visit_heading
    from app.booking.ics import build_visit_ics, _as_dt
    from app.infra.tokens import verify_cancel_token
    try:
        payload = verify_cancel_token(token)
    except Exception:
        raise runtime.HTTPException(status_code=404, detail='not found') from None
    event_id = str(payload.get('e') or '')
    cal_id = str(payload.get('c') or '')
    start = _as_dt(payload.get('s'))
    end = None
    dur = payload.get('d')
    try:
        dur_i = int(dur) if dur else 0
    except (TypeError, ValueError):
        dur_i = 0
    if start and dur_i:
        end = start + timedelta(minutes=dur_i)
    ev = None
    if event_id:
        try:
            ev = runtime.get_event(cal_id, event_id)
        except Exception:
            ev = None
    if ev:
        start = _as_dt((ev.get('start') or {}).get('dateTime')) or start
        end = _as_dt((ev.get('end') or {}).get('dateTime')) or end
        if start and dur_i:
            end = start + timedelta(minutes=dur_i)
    if not start or not end:
        raise runtime.HTTPException(status_code=404, detail='not found')
    title = str(payload.get('t') or (ev or {}).get('summary') or 'Visit')
    summary = visit_heading(title, dur_i or None)
    join = runtime.video_url()
    desc = f'Join: {join}'
    cancel = f'{runtime.PUBLIC_BASE}/cancel?token={token}'
    desc += f'\nCancel / reschedule: {cancel}'
    return runtime._ics_response(build_visit_ics(uid=event_id or f'visit-{token[:16]}', summary=summary, start=start, end=end, timezone=runtime.timezone_name(), description=desc, location=join), 'visit.ics', 'private, max-age=60')

def api_types():
    cfg = runtime.load_config()
    out = []
    for (slug, et) in cfg.get('event_types', {}).items():
        if et.get('enabled') is False:
            continue
        if et.get('public') is False:
            continue
        durs = runtime._durations(et)
        prices = {str(d): runtime._price_label(et, d, cfg) for d in durs}
        out.append({'slug': slug, 'title': et.get('title', slug), 'durations': durs, 'prices': prices, 'location': runtime._location_line(et, cfg), 'path': f'/{slug}'})
    return {'types': out}

def api_slots(request: runtime.Request, type: str=runtime.Query(..., alias='type'), duration: int | None=None, date: str | None=None):
    cfg = runtime.load_config()
    et = runtime.get_event_type(cfg, type)
    if not et:
        raise runtime.HTTPException(404, 'Unknown or disabled event type')
    durs = runtime._durations(et)
    dur = int(duration or durs[0])
    if dur not in durs:
        raise runtime.HTTPException(400, f'duration must be one of {durs}')
    if runtime._phone_channel(request):
        rs = None
        if date:
            try:
                rs = runtime.datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name())))
            except ValueError:
                raise runtime.HTTPException(400, 'date must be YYYY-MM-DD')
        slots = runtime._phone_slots(cfg, et, dur, rs)
    else:
        slots = runtime.generate_slots(cfg, et, dur)
    return {'event_type': type, 'title': et.get('title'), 'duration_minutes': dur, 'timezone': cfg.get('timezone'), 'slots': slots[:1000], 'count': len(slots)}

def _book_fill_email(req: runtime.BookRequest):
    if not req.email and (req.verify_token or '').strip():
        try:
            from app.portal.patients import client_by_id as _cbi
            _row = _cbi(str(runtime.verify_patient_token(req.verify_token.strip()).get('id') or '')) or {}
            if _row.get('email'):
                req.email = str(_row['email'])
        except ValueError:
            pass
    if not req.email:
        raise runtime.HTTPException(422, 'email is required')

def _book_type(req: runtime.BookRequest, cfg):
    et = runtime.get_event_type(cfg, req.event_type)
    if not et:
        raise runtime.HTTPException(404, 'Unknown or disabled event type')
    durs = runtime._durations(et)
    dur = int(req.duration_minutes or durs[0])
    if dur not in durs:
        raise runtime.HTTPException(400, f'duration must be one of {durs}')
    return (et, dur)

def _book_consent(req: runtime.BookRequest, request: runtime.Request):
    on_file = None
    if runtime._phone_channel(request):
        import app.portal.onboarding as onboarding
        on_file = onboarding.find_client(str(req.email))
    if req.sms_consent is None and (on_file or req.event_type == 'follow-up'):
        row = on_file
        if row is None:
            row = runtime._client_for_booking(str(req.email), req.phone or '', req.name)
        phone_on_file = (req.phone or (row or {}).get('phone') or (row or {}).get('phone_e164') or '').strip()
        sms_consent = runtime.clients_db.sms_consent_state(row) == 'yes' and bool(phone_on_file)
    else:
        sms_consent = bool(req.sms_consent) and bool((req.phone or '').strip())
    extra_notify = runtime.parse_email_list(req.notify_emails or '', limit=4)
    return (on_file, sms_consent, extra_notify)

def _book_gates(req: runtime.BookRequest, src: str):
    if req.event_type == runtime.HOLD_EVENT_TYPE and src != 'desk':
        import app.portal.onboarding as onboarding
        why = onboarding.established_match(email=str(req.email or ''), phone=req.phone or '')
        if why:
            which = 'email' if why == 'email' else 'phone number'
            raise runtime.HTTPException(422, f'That {which} is already a client. Book a follow-up visit instead: {runtime.PUBLIC_BASE}/follow-up')
    if req.event_type == 'follow-up' and src in ('voice', 'chat', 'agent'):
        token = (req.verify_token or '').strip()
        if src in ('chat', 'agent') and (not token):
            raise runtime.HTTPException(401, 'A code is required before this follow-up can be booked.')
        if token:
            try:
                runtime.verify_patient_token(token)
            except ValueError as e:
                raise runtime.HTTPException(401, f'Patient verification expired or invalid: {e}') from e
        else:
            from app.portal.verify_codes import recognize_for_message
            found = recognize_for_message(email=req.email, phone=req.phone)
            if not found.get('ok'):
                raise runtime.HTTPException(401, 'Follow-up booking needs the phone or email on file.')

def _book_span(req: runtime.BookRequest, cfg, et, dur: int):
    tz = runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name()))
    start = runtime.dateparser.isoparse(req.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    else:
        start = start.astimezone(tz)
    start = runtime._snap_half_hour(start)
    cal_dur = int(et.get('calendar_duration_minutes') or dur)
    if cal_dur < dur:
        cal_dur = dur
    end = start + runtime.timedelta(minutes=cal_dur)
    price_label = runtime._price_label(et, dur, cfg)
    location_line = runtime._location_line(et, cfg)
    return (start, end, cal_dur, price_label, location_line)

def _book_slot(req: runtime.BookRequest, request: runtime.Request, cfg, et, dur: int, start, src: str):
    staff_override = src == 'desk'
    if not staff_override:
        if runtime._phone_channel(request):
            slots = runtime._phone_slots(cfg, et, dur, start)
        else:
            slots = runtime.generate_slots(cfg, et, dur)
        ok = any((s['start'] == start.isoformat() for s in slots))
        if not ok:
            ok = any((abs((runtime.dateparser.isoparse(s['start']) - start).total_seconds()) < 60 for s in slots))
        if not ok:
            raise runtime.HTTPException(409, 'That slot is no longer available. Please pick another.')

def _book_cipher(req: runtime.BookRequest):
    cipher_on = runtime.cipher_mode.enabled()
    occ = runtime.cipher_mode.occupancy()
    dual = runtime.cipher_mode.dual_write()
    envelope_id = (req.cipher_envelope_id or '').strip()
    if cipher_on and envelope_id and (not runtime.cipher_queue.get_envelope(envelope_id)):
        raise runtime.HTTPException(400, 'cipher mode: unknown cipher_envelope_id')
    if occ and (not dual) and (not envelope_id):
        raise runtime.HTTPException(400, 'cipher mode: sealed intake required (cipher_envelope_id)')
    return (cipher_on, occ, dual, envelope_id)

def _book_hold_plan(req: runtime.BookRequest, start, src: str, occ, request: runtime.Request):
    hold: dict[str, runtime.Any] | None = None
    desk_intake_url = ''
    if req.event_type == runtime.HOLD_EVENT_TYPE and src != 'desk' and (not occ):
        hold = runtime._plan_hold(req, start, phone=runtime._phone_channel(request) or src in ('voice', 'chat', 'agent'))
    elif req.event_type == runtime.HOLD_EVENT_TYPE and src == 'desk' and (not occ):
        desk_intake_url = runtime._desk_intake_link(str(req.email))
    elif req.event_type != runtime.HOLD_EVENT_TYPE and (not occ):
        _bk = runtime._client_for_booking(str(req.email), req.phone or '', req.name)
        if _bk:
            desk_intake_url = runtime._desk_intake_link(str(req.email), str(_bk['id']))
    return (hold, desk_intake_url)

def _book_payload(req: runtime.BookRequest, start, dur: int, cal_dur: int, sms_consent, extra_notify, price_label, location_line, event_id: str, cal_id, title, hold, occ, envelope_id: str=''):
    if occ:
        summary = f'Visit · {event_id[:8]}'
    elif cal_dur != dur:
        summary = f'{req.name} – {title} ({dur} min visit · {cal_dur} min block)'
    else:
        summary = f'{req.name} – {title} ({dur} min)'
    when_label = start.strftime('%a %b %d · %I:%M %p').replace(' 0', ' ')
    if occ:
        desc_lines = ['Booked via Example Practice sovereign booker', 'Occupancy hold — visit detail is sealed (cipher P1).', f'Type: {req.event_type}', f'Client duration: {dur} minutes', f'Calendar block: {cal_dur} minutes', f'When: {when_label} {runtime.timezone_name()}']
    else:
        desc_lines = ['Booked via Example Practice sovereign booker', f'Name: {req.name}', f'Email: {req.email}', f"Phone: {req.phone or '(none)'}", f"SMS consent: {('yes' if sms_consent else 'no')}", f'Type: {req.event_type}', f'Client duration: {dur} minutes', f'Calendar block: {cal_dur} minutes', f"Fee: {price_label or '(see practice site)'}", f'Location: {location_line}', f'When: {when_label} {runtime.timezone_name()}']
        if req.notes:
            desc_lines.append(f'Notes: {req.notes}')
        if extra_notify:
            desc_lines.append('Also notify: ' + ', '.join(extra_notify))
        if hold:
            desc_lines.insert(1, f"HELD {hold['deadline_label']}: paperwork due (intake + consents + card on file). Released automatically if not finished.")
    rebook_url = f'{runtime.PUBLIC_BASE}/{req.event_type}'
    token = runtime.mint_cancel_token(event_id=event_id, calendar_id=cal_id, email=str(req.email), title=f'{title} ({dur} min)', when_label=when_label, name='' if occ else req.name, event_type=req.event_type, start_iso=start.isoformat(), duration_minutes=dur, purpose='cancel')
    cancel_url = f'{runtime.PUBLIC_BASE}/cancel?token={token}'
    ics_url = f'{runtime.PUBLIC_BASE}/cal/visit.ics?token={token}'
    desc_lines.append('')
    desc_lines.append('Cancel / reschedule:')
    desc_lines.append(cancel_url)
    desc_lines.append(f'Rebook: {rebook_url}')
    description = '\n'.join(desc_lines)
    if occ:
        private_props = {'booker': '1', 'cipher': '1', 'event_type': req.event_type, 'duration_minutes': str(dur), 'rem24': '1', 'rem1': '1', 'rem2': '1', 'remsms': '1'}
        if envelope_id:
            private_props['cipher_envelope_id'] = envelope_id
    else:
        private_props = {'booker': '1', 'patient_email': str(req.email), 'patient_name': req.name, 'patient_phone': req.phone or '', 'sms_consent': '1' if sms_consent else '0', 'event_type': req.event_type, 'title': title, 'when_label': when_label, 'duration_minutes': str(dur), 'price_label': price_label, 'rem24': '0', 'rem2': '0'}
        if hold:
            private_props['hold'] = 'held'
            private_props['hold_deadline'] = hold['deadline_iso']
    if hold:
        summary = runtime.HOLD_PREFIX + summary
    return (summary, description, when_label, private_props, cancel_url, ics_url, rebook_url)

def _book_write(cfg, cal_id, summary: str, description: str, start, end, private_props, event_id: str, hold):
    try:
        created = runtime.create_event(calendar_id=cal_id, summary=summary, description=description, start=start, end=end, timezone=str(cfg.get('timezone', runtime.timezone_name())), attendee_email=None, private_props=private_props, uid=event_id, **{'status': 'tentative'} if hold else {})
    except Exception as e:
        raise runtime.HTTPException(502, f'Calendar write failed: {e}') from e
    return created

def _book_save_consent(req: runtime.BookRequest, request: runtime.Request, hold, on_file):
    if req.sms_consent is not None:
        cid = (hold or {}).get('client_id') or (on_file or {}).get('id')
        if not cid:
            cid = (runtime._client_for_booking(str(req.email), req.phone or '', req.name) or {}).get('id')
        if cid:
            try:
                runtime.clients_db.set_sms_consent(cid, bool(req.sms_consent) and bool((req.phone or '').strip()), 'voice' if runtime._phone_channel(request) else 'web')
            except Exception as e:
                print('sms_consent_save_fail', type(e).__name__, flush=True)
                raise

def _book_record_hold(req: runtime.BookRequest, hold, event_id: str, cal_id, sms_consent, title, when_label, start, summary: str, src: str):
    if hold:
        try:
            import app.portal.onboarding as onboarding
            onboarding.create_hold(event_id=event_id, calendar_id=cal_id, client_id=hold['client_id'], email=str(req.email), name=req.name, phone=req.phone or '', sms_consent=sms_consent, title=title, when_label=when_label, event_type=req.event_type, start=start, booked_at=hold['booked_at'], deadline=hold['deadline'], intake_url=hold['intake_url'], summary=summary, source=src)
        except Exception as e:
            print('hold_record_fail', type(e).__name__, flush=True)
            hold = None
    return hold

def _book_dual_write(req: runtime.BookRequest, cipher_on, dual):
    if cipher_on and dual:
        try:
            runtime.patient_create_client(name=req.name, email=str(req.email), phone=req.phone or '')
        except Exception:
            raise

def _book_queue_notices(req: runtime.BookRequest, background_tasks: runtime.BackgroundTasks, title, when_label, cancel_url, sms_consent, price_label, location_line, extra_notify, rebook_url, dur: int, start, cfg, ics_url, event_id: str, hold, desk_intake_url):
    if req.send_email:
        background_tasks.add_task(runtime._send_booking_notices, name=req.name, email=str(req.email), phone=req.phone or '', title=title, when_label=when_label, event_type=req.event_type, cancel_url=cancel_url, notes=req.notes or '', sms_consent=sms_consent, price_label=price_label, location_line=location_line, extra_notify=extra_notify, rebook_url=rebook_url, duration_minutes=dur, start_iso=start.isoformat(), timezone=str(cfg.get('timezone', runtime.timezone_name())), ics_url=ics_url, event_id=event_id, hold_until_label=(hold or {}).get('deadline_label', ''), intake_url=(hold or {}).get('intake_url', '') or desk_intake_url)

def _book_result(created, event_id: str, summary, start, end, dur: int, cal_dur: int, when_label, cancel_url, ics_url, sms_consent, extra_notify, price_label, location_line, req: runtime.BookRequest, hold, desk_intake_url, request: runtime.Request):
    out = {'ok': True, 'event_id': event_id, 'html_link': created.get('htmlLink'), 'summary': summary, 'start': start.isoformat(), 'end': end.isoformat(), 'client_duration_minutes': dur, 'calendar_duration_minutes': cal_dur, 'when_label': when_label, 'cancel_url': cancel_url, 'ics_url': ics_url, 'sms_consent': sms_consent, 'notify_emails': extra_notify, 'price_label': price_label, 'location': location_line, 'email': {'queued': bool(req.send_email)}, 'message': "You're booked. A confirmation email with a cancel link is on the way."}
    if desk_intake_url and (not hold) and runtime._phone_channel(request):
        out['intake_link_sent'] = True
    if hold:
        out['held'] = True
        out['hold_until'] = hold['deadline_iso']
        out['hold_until_label'] = hold['deadline_label']
        out['message'] = f"Your spot is held {hold['deadline_label']}. Please complete your intake paperwork and add a card on file before your visit; the link is also in your email" + (' and text.' if sms_consent else '.')
        if not hold['phone']:
            out['intake_url'] = hold['intake_url']
    return out

def api_book(req: runtime.BookRequest, background_tasks: runtime.BackgroundTasks, request: runtime.Request):
    _book_fill_email(req)
    cfg = runtime.load_config()
    (et, dur) = _book_type(req, cfg)
    (on_file, sms_consent, extra_notify) = _book_consent(req, request)
    src = (req.source or 'web').strip().lower()
    _book_gates(req, src)
    (start, end, cal_dur, price_label, location_line) = _book_span(req, cfg, et, dur)
    _book_slot(req, request, cfg, et, dur, start, src)
    cal_id = et.get('google_calendar_id') or runtime.DEFAULT_CALENDAR
    title = et.get('title', req.event_type)
    event_id = runtime.uuid.uuid4().hex
    (cipher_on, occ, dual, envelope_id) = _book_cipher(req)
    (hold, desk_intake_url) = _book_hold_plan(req, start, src, occ, request)
    (summary, description, when_label, private_props, cancel_url, ics_url, rebook_url) = _book_payload(req, start, dur, cal_dur, sms_consent, extra_notify, price_label, location_line, event_id, cal_id, title, hold, occ, envelope_id)
    created = _book_write(cfg, cal_id, summary, description, start, end, private_props, event_id, hold)
    _book_save_consent(req, request, hold, on_file)
    hold = _book_record_hold(req, hold, event_id, cal_id, sms_consent, title, when_label, start, summary, src)
    _book_dual_write(req, cipher_on, dual)
    _book_queue_notices(req, background_tasks, title, when_label, cancel_url, sms_consent, price_label, location_line, extra_notify, rebook_url, dur, start, cfg, ics_url, event_id, hold, desk_intake_url)
    return _book_result(created, event_id, summary, start, end, dur, cal_dur, when_label, cancel_url, ics_url, sms_consent, extra_notify, price_label, location_line, req, hold, desk_intake_url, request)

def api_reminders_run(request: runtime.Request, dry_run: bool=runtime.Query(default=False)):
    """Cron entrypoint. x-staff-secret header only (no ?secret=, so it never lands in URLs/logs)."""
    if not runtime.REMIND_SECRET or not runtime.hmac.compare_digest(runtime._staff_secret('', request), runtime.REMIND_SECRET):
        raise runtime.HTTPException(401, 'unauthorized')
    return runtime.process_reminders(dry_run=dry_run)

def cancel_page(token: str='', done: str=''):
    if not token:
        return runtime.HTMLResponse(runtime._cancel_html(error='Missing cancel token.'), status_code=400)
    try:
        payload = runtime.verify_cancel_token(token)
    except ValueError as e:
        return runtime.HTMLResponse(runtime._cancel_html(error=str(e)), status_code=400)
    details = runtime._visit_details_from_payload(payload)
    rebook = f"/{payload.get('et')}" if payload.get('et') else '/'
    if details.get('gone') == '1' or done == '1':
        return runtime.HTMLResponse(runtime._cancel_html(done=True, note='This visit is cancelled.', title=details.get('title', ''), when_label=details.get('when_label', ''), name=details.get('name', ''), rebook_path=rebook))
    return runtime.HTMLResponse(runtime._cancel_html(token=token, email=details.get('email') or payload.get('m', ''), ready=True, title=details.get('title', ''), when_label=details.get('when_label', ''), name=details.get('name', ''), rebook_path=rebook))

def cancel_submit(token: str=runtime.Form(...)):
    try:
        payload = runtime.verify_cancel_token(token)
    except ValueError as e:
        return runtime.HTMLResponse(runtime._cancel_html(error=str(e)), status_code=400)
    details = runtime._visit_details_from_payload(payload)
    rebook = f"/{payload.get('et')}" if payload.get('et') else '/'
    try:
        runtime.delete_event(payload['c'], payload['e'])
    except Exception as e:
        msg = str(e)
        if '404' in msg or 'Not Found' in msg:
            return runtime.HTMLResponse(runtime._cancel_html(done=True, note='This visit was already cancelled or removed.', title=details.get('title', ''), when_label=details.get('when_label', ''), name=details.get('name', ''), email=details.get('email', ''), rebook_path=rebook))
        return runtime.HTMLResponse(runtime._cancel_html(error=f'Could not cancel: {msg}'), status_code=502)
    to = (details.get('email') or payload.get('m') or '').strip()
    if to:
        try:
            mail = runtime.send_named_email(kind='cancel', to=to, fields={'name': details.get('name') or '', 'email': to, 'title': details.get('title') or 'Visit', 'when_label': details.get('when_label') or '', 'rebook_url': f'{runtime.PUBLIC_BASE}{rebook}' if rebook.startswith('/') else rebook})
        except Exception:
            return runtime.HTMLResponse(runtime._cancel_html(error='The visit is cancelled, but the notice could not be sent.'), status_code=502)
        if not mail.get('ok'):
            return runtime.HTMLResponse(runtime._cancel_html(error='The visit is cancelled, but the notice could not be sent.'), status_code=502)
    from urllib.parse import quote
    return runtime.RedirectResponse(url=f'/cancel?token={quote(token)}&done=1', status_code=303)

def api_cancel(token: str=runtime.Form(...)):
    try:
        payload = runtime.verify_cancel_token(token)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    try:
        runtime.delete_event(payload['c'], payload['e'])
    except Exception as e:
        msg = str(e)
        if '404' in msg or 'Not Found' in msg:
            return {'ok': True, 'already_cancelled': True}
        raise runtime.HTTPException(502, f'Cancel failed: {msg}') from e
    return {'ok': True, 'cancelled': True}

def api_desk_books(month: str=runtime.Query(default='')):
    """Clinic month + year rollup. Not a fourth tile."""
    runtime._require_staff()
    from app.billing.fees import clinic_books
    return clinic_books(month=month)

def api_desk_book(req: runtime.DeskBookRequest, background_tasks: runtime.BackgroundTasks, request: runtime.Request):
    """Staff book anyone, any time. Confirmation checkbox emails and texts the patient and emails staff."""
    runtime._require_staff(request=request)
    (name, email, phone) = (req.name.strip(), req.email.strip(), req.phone.strip())
    row = runtime.client_by_id(req.client_id) if req.client_id else None
    if req.client_id and (not row):
        raise runtime.HTTPException(404, 'client not found')
    if row:
        name = name or row['name']
        email = email or row['email']
        phone = phone or row['phone']
    if not name or not email:
        raise runtime.HTTPException(400, 'need name and email (pick a client or type them)')
    sms_consent = None
    if req.send_email and phone:
        raw = runtime.clients_db.get_by_id(req.client_id) if req.client_id else None
        if raw is None:
            try:
                raw = runtime._client_for_booking(email, phone, name)
            except Exception:
                raw = None
        state = runtime.clients_db.sms_consent_state(raw)
        sms_consent = False if state == 'no' else True
    book = runtime.BookRequest(event_type=req.event_type, start=req.start, duration_minutes=req.duration_minutes, name=name, email=email, phone=phone, notes=req.notes, source='desk', send_email=req.send_email, sms_consent=sms_consent)
    out = api_book(book, background_tasks, request)
    if isinstance(out, dict):
        out['staff_email'] = bool(req.send_email)
        out['patient_sms'] = bool(sms_consent)
    return out

def api_desk_visit_cancel(req: runtime.DeskVisitCancel, request: runtime.Request):
    """Delete the visit. Notify the patient only when notify is true."""
    runtime._require_staff(request=request)
    eid = (req.event_id or '').strip()
    cid = (req.calendar_id or '').strip() or (runtime.os.environ.get('GOOGLE_CALENDAR_ID') or runtime.DEFAULT_CALENDAR)
    if not eid:
        raise runtime.HTTPException(400, 'need a visit')
    cfg = runtime.load_config()
    tz = runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name()))
    try:
        ev = runtime.get_event(cid, eid)
    except Exception as e:
        raise runtime.HTTPException(404, 'visit not found') from e
    fields = runtime._event_mail_fields(ev, cid, tz)
    if req.notify:
        (email, phone, allow_sms) = runtime._visit_contact(ev, fields)
        if not email and (not phone):
            raise runtime.HTTPException(409, 'No email or phone on this visit, so it was not deleted. Uncheck notify to delete only.')
    else:
        (email, phone, allow_sms) = ('', '', False)
    try:
        runtime.delete_event(cid, eid)
    except Exception as e:
        raise runtime.HTTPException(502, 'Could not delete the visit') from e
    notified = {'email': False, 'sms': False}
    if req.notify:
        when = fields.get('when_label') or 'your visit'
        title = fields.get('title') or 'Visit'
        if email:
            from app.messaging.notify import send_named_email
            try:
                mail = send_named_email(kind='cancel', to=email, fields={'name': fields.get('name') or 'there', 'email': email, 'title': title, 'when_label': when, 'start_iso': fields.get('start_iso') or '', 'duration_minutes': fields.get('duration_minutes') or 30, 'rebook_url': runtime.PUBLIC_BASE})
            except Exception as e:
                raise runtime.HTTPException(502, 'Visit was deleted, but the cancellation email failed') from e
            if not mail.get('ok'):
                raise runtime.HTTPException(502, 'Visit was deleted, but the cancellation email failed')
            notified['email'] = True
        if phone and allow_sms:
            from app.messaging.spruce_api import send_spruce_sms, spruce_api_enabled
            if spruce_api_enabled():
                try:
                    sms = send_spruce_sms(phone, f'Your visit ({when}) with {runtime.HOST_SHORT} has been cancelled. Book a new time at {runtime.PUBLIC_BASE} — {runtime.HOST_SHORT}', idempotency_key=f'cancel:{eid}')
                except Exception as e:
                    raise runtime.HTTPException(502, 'Visit was deleted, but the cancellation text failed') from e
                if not sms.get('ok'):
                    raise runtime.HTTPException(502, 'Visit was deleted, but the cancellation text failed')
                notified['sms'] = True
    try:
        from app.notes.visit_flags import set_flag
        set_flag(eid, '')
    except Exception as e:
        raise runtime.HTTPException(502, 'Visit was deleted, but the flag could not be cleared') from e
    return {'ok': True, 'deleted': True, 'notified': notified}

def api_desk_call_get(sid: str=runtime.Query(default='')):
    runtime._require_staff_or_host()
    from app.portal.desk_call import call_status, config as call_config
    if sid:
        try:
            return call_status(sid)
        except RuntimeError as e:
            raise runtime.HTTPException(502, str(e)[:200]) from e
    return call_config()

def api_desk_call_place(req: runtime.DeskCallRequest):
    runtime._require_staff_or_host()
    from app.portal.desk_call import place_call
    try:
        out = place_call(to=req.to, client_id=req.client_id, name=req.name, handset=req.handset)
    except RuntimeError as e:
        raise runtime.HTTPException(502, str(e)[:200]) from e
    if not out.get('ok'):
        raise runtime.HTTPException(400, str(out.get('error') or 'could not place call'))
    return out

def api_desk_call_hangup(req: runtime.DeskCallRequest):
    runtime._require_staff_or_host()
    from app.portal.desk_call import hangup_call
    try:
        out = hangup_call(req.sid)
    except RuntimeError as e:
        raise runtime.HTTPException(502, str(e)[:200]) from e
    if not out.get('ok'):
        raise runtime.HTTPException(400, str(out.get('error') or 'could not hang up'))
    return out

def page_new():
    return runtime._book_page('new-client')

def page_follow():
    return runtime._book_page('follow-up')

def page_intro():
    return runtime.RedirectResponse('/new-client', status_code=301)

def page_thanks():
    return runtime._page('thanks.html')

def page_book_token(token: str, request: runtime.Request=None):
    """Personal booking link. Redirects to the service page with the code in ?c=.
    booker.js also accepts a legacy #bt= fragment, fills the form, then clears the address bar."""
    ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else '') if request is not None else ''
    if ip and (not runtime._b_rate_ok(ip)):
        return runtime.HTMLResponse('Too many tries. Wait a minute and try again.', status_code=429, headers=runtime._NO_INDEX)
    hit = runtime._book_token_client(token, 'b')
    if not hit:
        return runtime.HTMLResponse(f"<!doctype html><meta name=robots content=noindex><meta name=viewport content='width=device-width'><title>Booking link didn't work</title><body style='font-family:system-ui;max-width:32em;margin:3em auto;padding:0 1em;line-height:1.5'><h1 style='font-size:1.3em'>This booking link didn't work</h1><p>It may have been cut off when it was copied, or replaced with a newer one.</p><p><a href='{runtime.PUBLIC_BASE}/follow-up'>Book a follow-up here</a>. You'll type your name, email and phone yourself. Or ask the office for a new link.</p></body>", status_code=410, headers=runtime._NO_INDEX)
    from urllib.parse import quote
    return runtime.RedirectResponse(f"/{hit[0]['s']}?c={quote(token, safe='')}", status_code=303, headers=runtime._NO_INDEX)

def api_book_prefill(req: runtime.BookPrefillRequest):
    """Token -> current name/email/phone for the client it is bound to. no-store."""
    hit = runtime._book_token_client(req.token, 'prefill')
    if not hit:
        return runtime.JSONResponse({'ok': False}, status_code=410, headers=runtime._NO_INDEX)
    (p, row) = hit
    return runtime.JSONResponse({'ok': True, 'slug': p['s'], 'duration': p.get('d'), 'name': row.get('display_name') or '', 'email': row.get('email') or '', 'phone': row.get('phone_e164') or ''}, headers=runtime._NO_INDEX)

def api_desk_book_link(req: runtime.DeskBookLinkRequest):
    """Desk: mint a client's personal booking link (book host /b/<token>); optional email."""
    runtime._require_staff()
    return runtime._desk_book_link(req.client_id, duration=req.duration, send_email=req.send_email, dry_run=req.dry_run)

def api_desk_book_link_revoke(req: runtime.DeskBookLinkRevoke):
    """Kill the client's current booking link (e.g. forwarded) and return the reissued one."""
    runtime._require_staff()
    if not runtime.clients_db.get_by_id(req.client_id):
        raise runtime.HTTPException(404, 'client not found')
    runtime.clients_db.bump_book_link_version(req.client_id)
    return {'ok': True, 'url': runtime._book_link(req.client_id)['url']}

router.add_api_route('/health', health, methods=['GET'])
router.add_api_route('/cal', cal_subscribe_page, methods=['GET'])
router.add_api_route('/cal/busy.ics', ics_busy, methods=['GET'])
router.add_api_route('/cal/practice.ics', ics_practice, methods=['GET'])
router.add_api_route('/cal/visit.ics', ics_visit, methods=['GET'])
router.add_api_route('/api/types', api_types, methods=['GET'])
router.add_api_route('/api/slots', api_slots, methods=['GET'])
router.add_api_route('/api/book', api_book, methods=['POST'])
router.add_api_route('/api/reminders/run', api_reminders_run, methods=['POST'])
router.add_api_route('/cancel', cancel_page, methods=['GET'], response_class=runtime.HTMLResponse)
router.add_api_route('/cancel', cancel_submit, methods=['POST'], response_class=runtime.HTMLResponse)
router.add_api_route('/api/cancel', api_cancel, methods=['POST'])
router.add_api_route('/api/desk/books', api_desk_books, methods=['GET'])
router.add_api_route('/api/desk/book', api_desk_book, methods=['POST'])
router.add_api_route('/api/desk/visit/cancel', api_desk_visit_cancel, methods=['POST'])
router.add_api_route('/api/desk/call', api_desk_call_get, methods=['GET'])
router.add_api_route('/api/desk/call', api_desk_call_place, methods=['POST'])
router.add_api_route('/api/desk/call/hangup', api_desk_call_hangup, methods=['POST'])
router.add_api_route('/new-client', page_new, methods=['GET'])
router.add_api_route('/follow-up', page_follow, methods=['GET'])
router.add_api_route('/intro-15', page_intro, methods=['GET'])
router.add_api_route('/thanks', page_thanks, methods=['GET'])
router.add_api_route('/b/{token}', page_book_token, methods=['GET'])
router.add_api_route('/api/public/book-prefill', api_book_prefill, methods=['POST'])
router.add_api_route('/api/desk/book-link', api_desk_book_link, methods=['POST'])
router.add_api_route('/api/desk/book-link/revoke', api_desk_book_link_revoke, methods=['POST'])
