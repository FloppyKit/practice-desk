from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def api_internal_card_link(req: runtime.CardLinkRequest, request: runtime.Request):
    """Card change from the logged-in chat: email (and Spruce text, with consent) the /pay link to the
    on-file contact. Staff secret + login token. Returns only {status, hint}."""
    from app.booking.card_link import send_card_link
    from app.messaging.notify import send_email
    from app.messaging.spruce_api import send_spruce_sms, spruce_api_enabled
    runtime._require_staff('', request)
    row = runtime._verified_client(req.verify_token.strip())
    sms = (lambda to, body: send_spruce_sms(to, body)) if spruce_api_enabled() else None
    return send_card_link(row, pay_url=f'{runtime.SITE_PUBLIC}/pay', send_email=lambda to, subj, body: send_email(to, subj, body), send_sms=sms)

def api_desk_auth_send(req: runtime.DeskAuthEmail, request: runtime.Request):
    """Same reply for any email (no allowlist oracle). Failures are logged, not returned."""
    import logging
    import app.portal.desk_auth as desk_auth
    try:
        desk_auth.send_code(req.email, runtime._client_ip(request), background=True)
    except Exception:
        logging.getLogger('desk.auth').exception('desk auth code send failed')
    return {'ok': True, 'detail': 'If that address is on the staff list, a code is on its way.'}

def api_desk_auth_legacy(req: runtime.DeskAuthLegacy, request: runtime.Request):
    """Old shared secret -> session, only until the first emailed-code sign-in."""
    import app.portal.desk_auth as desk_auth
    ip = runtime._client_ip(request)
    with desk_auth._con() as con:
        limited = desk_auth._limited(con, 'ip-verify:' + ip, desk_auth.VERIFY_PER_IP)
    ok = not limited and desk_auth.legacy_allowed() and runtime.REMIND_SECRET and runtime.hmac.compare_digest((req.secret or '').strip(), runtime.REMIND_SECRET)
    if not ok:
        raise runtime.HTTPException(401, 'unauthorized')
    out = runtime.Response(content=runtime.json.dumps({'ok': True}), media_type='application/json')
    runtime._set_desk_cookie(out, desk_auth.mint('legacy-secret'))
    out.delete_cookie(runtime.STAFF_COOKIE, path='/')
    return out

def api_desk_today():
    """Today's booker visits for the staff desk (late buttons)."""
    runtime._require_staff()
    cfg = runtime.load_config()
    tz = runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name()))
    now = runtime.datetime.now(tz)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + runtime.timedelta(days=1)
    cal_ids = runtime._desk_cal_ids()
    visits = []
    for cid in cal_ids:
        try:
            events = runtime.list_booker_events(cid, day0.astimezone(), day1.astimezone())
        except Exception as e:
            visits.append({'ok': False, 'calendar': cid, 'error': str(e)})
            continue
        for ev in events:
            if ev.get('status') == 'cancelled':
                continue
            visits.append(runtime._event_mail_fields(ev, cid, tz))
    visits = [v for v in visits if v.get('event_id')]
    visits.sort(key=lambda v: v.get('start_iso') or '')
    return {'ok': True, 'now': now.isoformat(), 'visits': visits}

def api_desk_week(start: str=runtime.Query(default=''), days: int=runtime.Query(default=7)):
    """Week of calendar events for the staff board (all events, not only booker)."""
    runtime._require_staff()
    return runtime._desk_week_payload(start, days)

def api_desk_clients(q: str=runtime.Query(default=''), start: str=runtime.Query(default=''), days: int=runtime.Query(default=7)):
    """Find a client: phone book plus people already on this week."""
    runtime._require_staff_or_host()
    book = runtime.search_clients(q) if runtime.supabase_ready() else []
    week_people: list[dict[str, runtime.Any]] = []
    try:
        week = runtime._desk_week_payload(start, days)
        week_people = runtime.people_from_visits(week.get('visits') or [], q)
    except runtime.HTTPException:
        raise
    except Exception:
        week_people = []
    clients = runtime.merge_client_hits(book, week_people)
    return {'ok': True, 'clients': clients, 'sources': {'book': len(book), 'week': len(week_people)}}

def api_internal_intake_submitted(req: runtime.InternalIntakeRequest, request: runtime.Request, background_tasks: runtime.BackgroundTasks):
    """Site intake form landed (already emailed to the practice). Record the completion flag, file the
    signed PDF under Chart/Consents, and confirm any held new-client visit once intake is complete."""
    import app.portal.onboarding as onboarding
    runtime._require_staff(request=request)
    form = (req.form or '').strip()
    if form not in runtime.INTAKE_FORMS:
        raise runtime.HTTPException(400, 'unknown form')
    link = req.meta.get('linkAuth') or {}
    email = str((link.get('email') if link.get('verified') else '') or req.fields.get('email') or '').strip().lower()
    cid = str((link.get('clientId') if link.get('verified') else '') or '').strip()
    rec = onboarding.record_submission(form=form, email=email, link_verified=bool(link.get('verified')), client_id=cid)
    if form == 'personal' and rec['client_id'] and ('smsConsent' in req.fields):
        try:
            runtime.clients_db.set_sms_consent(rec['client_id'], bool(req.fields.get('smsConsent')), 'web')
        except Exception as e:
            print('sms_consent_save_fail', type(e).__name__, flush=True)
    background_tasks.add_task(runtime._file_intake_pdf, rec['id'], rec['client_id'], form, dict(req.fields), dict(req.meta), rec['received_at'])
    if rec['client_id']:
        background_tasks.add_task(runtime._check_client_bg, rec['client_id'])
    st = onboarding.status(client_id=rec['client_id']) if rec['client_id'] else None
    return {'ok': True, 'linked': bool(rec['client_id']), 'flags': (st or {}).get('flags'), 'complete': bool(st and st['complete'])}

def api_desk_intake_link(client_id: str, request: runtime.Request):
    """Staff: mint (or re-mint) this client's personal intake link and save it on the row. Sends nothing."""
    runtime._require_staff(request=request)
    row = runtime.clients_db.get_by_id(client_id)
    if not row:
        raise runtime.HTTPException(404, 'no client')
    url = runtime.mint_client_intake(row)
    got = runtime.clients_db.get_by_id(client_id) or {}
    return {'ok': True, 'url': url, 'minted_at': got.get('intake_minted_at')}

def api_internal_intake_progress(request: runtime.Request, email: str='', client_id: str=''):
    """Site cascade: which steps are done for the emailed-link session (resume at first incomplete),
    plus name/phone to prefill. Staff secret (site server only)."""
    import app.portal.onboarding as onboarding
    runtime._require_staff(request=request)
    row = onboarding.find_client(email, client_id)
    st = onboarding.status(client_id=(row or {}).get('id') or '', email=email)
    holds = onboarding.holds_for_client(st['client_id']) if st['client_id'] else []
    h = holds[0] if holds else None
    return {'ok': True, 'flags': st['flags'], 'complete': st['complete'], 'missing': st['missing'], 'client': {'name': (row or {}).get('display_name') or '', 'given_name': (row or {}).get('given_name') or '', 'family_name': (row or {}).get('family_name') or '', 'phone': (row or {}).get('phone_e164') or '', 'dob': (row or {}).get('dob') or ''} if row else None, 'hold': {'status': h['status'], 'when_label': h['when_label'], 'deadline_label': onboarding.label(runtime.datetime.fromisoformat(h['deadline_at']))} if h else None}

def api_desk_hold_times(event_id: str, req: runtime.HoldTimesRequest, request: runtime.Request):
    """Staff/test only: move ONE hold's deadline (minutes from now). The 48h policy is unchanged."""
    from datetime import timezone as _tz
    import app.portal.onboarding as onboarding
    runtime._require_staff(request=request)
    now = runtime.datetime.now(_tz.utc)
    dl = now + runtime.timedelta(minutes=req.deadline_minutes)
    rm = now + runtime.timedelta(minutes=req.remind_minutes if req.remind_minutes is not None else req.deadline_minutes / 2)
    h = onboarding.set_times(event_id, deadline=dl, remind_at=rm)
    if not h:
        raise runtime.HTTPException(404, 'no hold')
    return {'ok': True, 'hold': {k: h[k] for k in ('event_id', 'status', 'deadline_at', 'remind_at')}}

def api_desk_hold_sweep(request: runtime.Request):
    import app.portal.onboarding as onboarding
    runtime._require_staff(request=request)
    return {'ok': True, **onboarding.sweep(runtime._onboarding_fx())}

def api_desk_onboarding_status(request: runtime.Request, client_id: str='', email: str=''):
    import app.portal.onboarding as onboarding
    runtime._require_staff(request=request)
    st = onboarding.status(client_id=client_id, email=email)
    holds = onboarding.holds_for_client(st['client_id']) if st['client_id'] else []
    return {'ok': True, **st, 'holds': [{k: h[k] for k in ('event_id', 'status', 'when_label', 'deadline_at', 'reminded_at', 'resolved_at')} for h in holds]}

def api_internal_client_card(req: runtime.InternalCardRequest, request: runtime.Request):
    """Site pay form writes Stripe pointers. Staff secret. No PAN. No new row."""
    from app.portal.patients import record_card_on_file
    runtime._require_staff(request=request)
    out = record_card_on_file(client_id=req.client_id, email=req.email, customer_id=req.customer_id, payment_method_id=req.payment_method_id, brand=req.brand, last4=req.last4, exp_month=req.exp_month, exp_year=req.exp_year)
    try:
        import app.portal.onboarding as onboarding
        cid = str(out.get('client_id') or req.client_id or '')
        out['confirmed_holds'] = onboarding.check_client(runtime._onboarding_fx(), client_id=cid, email=req.email)
        row = onboarding.find_client(req.email, cid)
        if row and row.get('id'):
            runtime._maybe_send_copies(str(row['id']))
    except Exception as e:
        print('hold_check_fail', type(e).__name__, flush=True)
    return out

def api_desk_refills():
    """Read-only Refills queue (desk session). MDToolbox fields are pending."""
    runtime._require_staff()
    import app.portal.refills as refills
    return {'ok': True, 'requests': refills.list_requests(), 'mdtoolbox': 'pending'}

def api_desk_refill_sent(rid: str):
    """The host marks a refill sent, then one patient text (Spruce). Desk session or staff header only."""
    runtime._require_staff()
    import app.portal.refills as refills
    out = refills.mark_sent(rid, actor=runtime._desk_session_email() or 'staff')
    if not out.get('ok') and out.get('error') == 'not_found':
        raise runtime.HTTPException(404, 'not found')
    return out

def api_desk_refills_summary():
    """PHI-free counts for bots: by status and age bucket only."""
    runtime._require_staff()
    import app.portal.refills as refills
    return refills.summary()

def api_desk_client(id: str=runtime.Query(default=''), email: str=runtime.Query(default=''), name: str=runtime.Query(default='')):
    """One client home: identity, card, visits, form catalog. Notes later."""
    runtime._require_staff_or_host()
    row = runtime.client_by_id(id) if id else None
    if not row and email:
        row = runtime.client_by_email(email)
    if not row and name.strip():
        raw_name = name.strip()
        bare = runtime.re.sub('\\(.*?\\)', ' ', raw_name.split('·')[-1].split(' - ')[-1]).strip()
        from app.booking.cal_reminders import _candidates
        for q in dict.fromkeys([raw_name, *_candidates(raw_name), bare]):
            if row or not q:
                continue
            hits = runtime.search_clients(q, limit=8)
            if len(hits) > 1:
                exact = [h for h in hits if str(h.get('name') or '').strip().lower() == q.lower()]
                hits = exact if len(exact) == 1 else hits
            if len(hits) == 1:
                row = runtime.client_by_id(hits[0]['id'])
    if not row:
        who = (email or '').strip() or name.strip()
        if not who:
            raise runtime.HTTPException(404, 'client not found')
        row = {'id': '', 'name': name.strip(), 'email': (email or '').strip().lower(), 'phone': '', 'dob': '', 'status': 'calendar-only', 'card_on_file': False, 'directory': False, 'given_name': '', 'family_name': '', 'chart_url': '', 'billing_url': ''}
    else:
        row = dict(row)
        row['directory'] = True
    tz = runtime.ZoneInfo(runtime.load_config().get('timezone', runtime.timezone_name()))
    visits = runtime._visits_for_client(row, tz)
    if not row.get('name'):
        for v in visits:
            if v.get('name'):
                row['name'] = v['name']
                break
    upcoming = [v for v in visits if v.get('upcoming')]
    upcoming.sort(key=lambda v: v.get('start_iso') or '')
    past = [v for v in visits if not v.get('upcoming')]
    pay_url = f'{runtime.SITE_PUBLIC}/pay'
    if row.get('email'):
        pay_url += '?' + runtime.urlencode({'email': row['email'], 'name': row.get('name') or ''})
    intake = runtime._intake_summary(row)
    done_by_item = {'personal': 'personal', 'consents': 'consents', 'card': 'card-auth', 'roi': 'roi'}
    forms = [{'id': sid, 'label': spec.get('label') or sid, 'status': runtime._form_status(intake, done_by_item.get(sid, '')), 'url': spec.get('url') or f'{runtime.SITE_PUBLIC}/forms'} for (sid, spec) in runtime.SEND_ITEMS.items() if sid != 'intake']
    from app.infra.drive_paths import VISIT_NOTES_FOLDER, paths_from_row
    stored = paths_from_row(row)
    vn_path = ''
    if stored.get('chart'):
        vn_path = stored['chart'].rstrip('/') + '/' + VISIT_NOTES_FOLDER
    last_note = runtime._last_note_listing(vn_path)
    from app.billing.fees import client_books
    from app.infra.practice import load as load_practice
    last_dur = None
    for v in past[:1] + upcoming[:1]:
        if v.get('duration_minutes'):
            last_dur = v.get('duration_minutes')
            break
    role = str((load_practice() or {}).get('billing_role') or 'psychiatrist')
    books = client_books(str(row.get('id') or ''), duration_minutes=last_dur, role=role)
    return {'ok': True, 'client': row, 'card': {'on_file': bool(row.get('card_on_file')), 'brand': row.get('card_brand') or '', 'last4': row.get('card_last4') or '', 'exp_month': row.get('card_exp_month'), 'exp_year': row.get('card_exp_year')}, 'visits': {'upcoming': upcoming[:12], 'past': past[:16]}, 'forms': forms, 'intake': intake, 'last_note': last_note, 'books': books, 'plan': {'ok': False, 'status': 'none', 'hint': 'Light treatment plan comes after notes.'}, 'files': {'chart': row.get('chart_url') or '', 'billing': row.get('billing_url') or '', 'folder_path': stored.get('folder') or '', 'chart_path': stored.get('chart') or '', 'billing_path': stored.get('billing') or '', 'visit_notes_path': vn_path}, 'links': {'pay': pay_url, 'forms': f'{runtime.SITE_PUBLIC}/forms', 'book': runtime._book_link(str(row['id']))['url'] if row.get('id') else f'{runtime.PUBLIC_BASE}/follow-up', 'mailto': f"mailto:{row['email']}" if row.get('email') else ''}}

def api_desk_client_drive(id: str=runtime.Query(default=''), email: str=runtime.Query(default='')):
    """Map a directory client onto an existing /Client files folder. No new names."""
    runtime._require_staff_or_host()
    row = runtime.client_by_id(id) if id else None
    if not row and email:
        row = runtime.client_by_email(email)
    if not row:
        return {'ok': True, 'folder_path': '', 'chart_path': '', 'billing_path': ''}
    files = runtime._client_files_payload(row, confirm=True)
    return {'ok': True, 'folder_path': files.get('folder_path') or '', 'chart_path': files.get('chart_path') or '', 'billing_path': files.get('billing_path') or ''}

def api_desk_visit_flag(req: runtime.DeskVisitFlag, request: runtime.Request):
    """Mark a visit no-show or late cancel. Does not delete it and does not charge."""
    runtime._require_staff(request=request)
    from app.notes.visit_flags import set_flag
    try:
        flag = set_flag(req.event_id, req.flag)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    return {'ok': True, 'flag': flag}

def api_desk_visit_move(req: runtime.DeskVisitMove, request: runtime.Request):
    """Move a visit. Keeps the length. Notifies only when notify is true."""
    runtime._require_staff(request=request)
    eid = (req.event_id or '').strip()
    cid = (req.calendar_id or '').strip() or (runtime.os.environ.get('GOOGLE_CALENDAR_ID') or runtime.DEFAULT_CALENDAR)
    if not eid or not (req.start or '').strip():
        raise runtime.HTTPException(400, 'need a visit and a time')
    cfg = runtime.load_config()
    tz = runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name()))
    try:
        start = runtime.dateparser.isoparse(req.start)
    except Exception as e:
        raise runtime.HTTPException(400, 'bad time') from e
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    else:
        start = start.astimezone(tz)
    start = runtime._snap_half_hour(start)
    try:
        ev = runtime.get_event(cid, eid)
    except Exception as e:
        raise runtime.HTTPException(404, 'visit not found') from e
    fields = runtime._event_mail_fields(ev, cid, tz)
    dur = int(fields.get('duration_minutes') or 30)
    end = start + runtime.timedelta(minutes=dur)
    try:
        runtime.patch_event(cid, eid, start=start, end=end)
    except Exception as e:
        raise runtime.HTTPException(502, 'Could not move the visit') from e
    when = start.strftime('%a %b %d · %I:%M %p').replace(' 0', ' ')
    notified = {'email': False, 'sms': False}
    if req.notify:
        (email, phone, allow_sms) = runtime._visit_contact(ev, fields)
        if email:
            from app.messaging.notify import send_named_email
            mail = send_named_email(kind='reschedule', to=email, fields={'name': fields.get('name') or 'there', 'email': email, 'title': fields.get('title') or 'Visit', 'when_label': when, 'start_iso': start.isoformat(), 'duration_minutes': dur, 'rebook_url': runtime.PUBLIC_BASE})
            notified['email'] = bool(mail.get('ok'))
        if phone and allow_sms:
            from app.messaging.spruce_api import send_spruce_sms, spruce_api_enabled
            if spruce_api_enabled():
                sms = send_spruce_sms(phone, f'Your visit with {runtime.HOST_SHORT} moved to {when}. {runtime.book_host()} — {runtime.HOST_SHORT}', idempotency_key=f'move:{eid}:{start.isoformat()}')
                notified['sms'] = bool(sms.get('ok'))
    return {'ok': True, 'start': start.isoformat(), 'when_label': when, 'notified': notified}

def api_practice_public():
    """Public letterhead. No PHI. Site, portal, and booker pages read this."""
    from app.infra.practice import public as public_practice
    return public_practice(runtime.PUBLIC_BASE)

def api_practice_logo():
    from app.infra.practice import asset_bytes
    got = asset_bytes('logo')
    if not got:
        raise runtime.HTTPException(404, 'no logo')
    (data, ctype) = got
    return runtime.Response(content=data, media_type=ctype, headers={'Cache-Control': 'public, max-age=300'})

def api_practice_photo():
    from app.infra.practice import asset_bytes
    got = asset_bytes('photo')
    if not got:
        raise runtime.HTTPException(404, 'no photo')
    (data, ctype) = got
    return runtime.Response(content=data, media_type=ctype, headers={'Cache-Control': 'public, max-age=300'})

def api_practice_background(page: str=runtime.Query(default='home')):
    from app.infra.practice import PAGE_SLUGS, asset_bytes
    slug = (page or 'home').strip().lower()
    if slug not in PAGE_SLUGS:
        raise runtime.HTTPException(404, 'unknown page')
    got = asset_bytes(f'bg-{slug}') or (asset_bytes('background') if slug == 'home' else None)
    if not got:
        raise runtime.HTTPException(404, 'no background')
    (data, ctype) = got
    return runtime.Response(content=data, media_type=ctype, headers={'Cache-Control': 'public, max-age=300'})

def api_desk_practice_get():
    runtime._require_staff()
    from app.portal.desk_call import config as call_config
    from app.infra.practice import load as load_practice, pages_labeled, public as public_practice, service_set
    p = load_practice()
    pub = public_practice('' if runtime._demo_on() else runtime.PUBLIC_BASE)
    return {'ok': True, **p, **{k: pub[k] for k in ('logo_url', 'photo_url', 'backgrounds', 'has_logo', 'has_photo', 'theme', 'page_titles')}, 'services': service_set(str(p.get('billing_role') or '')), 'pages': [{'id': a, 'label': b} for (a, b) in pages_labeled(p.get('page_titles'))], 'call': call_config()}

def api_desk_practice_save(req: runtime.DeskPracticeSave):
    runtime._require_staff()
    from app.infra.practice import public as public_practice, save as save_practice
    saved = save_practice(req.model_dump())
    from app.infra.practice import service_set
    pub = public_practice('' if runtime._demo_on() else runtime.PUBLIC_BASE)
    return {'ok': True, **saved, **{k: pub[k] for k in ('logo_url', 'photo_url', 'backgrounds', 'has_logo', 'has_photo', 'theme', 'page_titles')}, 'services': service_set(str(saved.get('billing_role') or ''))}

def api_desk_services_get():
    runtime._require_staff()
    from app.booking.event_store import list_types
    return {'ok': True, 'services': list_types(runtime.load_config()), 'days': ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']}

def api_desk_services_save(req: runtime.DeskServiceSave):
    runtime._require_staff()
    from app.booking.event_store import list_types, save_type
    try:
        saved = save_type(runtime.load_config(), req.slug, req.model_dump(), creating=req.creating)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    return {'ok': True, 'service': saved, 'services': list_types(runtime.load_config())}

def api_desk_services_delete(slug: str=runtime.Query(default='')):
    runtime._require_staff()
    from app.booking.event_store import delete_type, list_types
    if not slug:
        raise runtime.HTTPException(400, 'need a slug')
    delete_type(runtime.load_config(), slug)
    return {'ok': True, 'services': list_types(runtime.load_config())}

async def api_desk_practice_asset(kind: str=runtime.Query(default='logo'), file: runtime.UploadFile=runtime.File(...)):
    runtime._require_staff()
    from app.infra.practice import public as public_practice, save_asset
    raw = await file.read()
    try:
        saved = save_asset(kind, raw, file.content_type or '')
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    pub = public_practice('' if runtime._demo_on() else runtime.PUBLIC_BASE)
    return {'ok': True, **saved, **{k: pub[k] for k in ('logo_url', 'photo_url', 'backgrounds', 'has_logo', 'has_photo')}}

async def api_twilio_desk_voice(request: runtime.Request):
    """TwiML for desktop handset. Destination comes from a one-time nonce, not the browser."""
    from app.portal.desk_call import browser_twiml, reject_twiml, take_pending, valid_twilio_signature
    form = {str(k): str(v) for (k, v) in (await request.form()).items()}
    url = runtime.PUBLIC_BASE + '/api/twilio/desk-voice'
    sig = request.headers.get('X-Twilio-Signature') or ''
    if not valid_twilio_signature(url, form, sig):
        raise runtime.HTTPException(403, 'bad signature')
    nonce = (form.get('nonce') or form.get('CallNonce') or '').strip()
    rec = take_pending(nonce)
    if not rec:
        return runtime.Response(content=reject_twiml('That call expired.'), media_type='text/xml')
    dest = str(rec.get('to') or '')
    did = str(rec.get('from') or '')
    if not dest or not did:
        return runtime.Response(content=reject_twiml('Call cannot be completed.'), media_type='text/xml')
    return runtime.Response(content=browser_twiml(dest, did), media_type='text/xml')

def api_desk_meta():
    """Staff links — office + guest waiting room."""
    runtime._require_staff()
    runtime._warm_client_window()
    from app.infra.chart_store import ChartError, backend_name
    try:
        chart_backend = backend_name()
    except ChartError as exc:
        raise runtime.HTTPException(exc.status if exc.status >= 400 else 502, str(exc)[:200]) from exc
    resp = {'ok': True, 'office_url': runtime.os.environ.get('LIVEKIT_OFFICE_URL', '').strip(), 'guest_url': runtime.os.environ.get('LIVEKIT_GUEST_URL', runtime.video_url()).strip(), 'agent': True, 'drive': True, 'chart_backend': chart_backend, 'practice': True, 'superbill': True, 'vault_key': runtime.os.environ.get('DESK_VAULT_SECRET') or runtime.REMIND_SECRET}
    prev = runtime.os.environ.get('DESK_VAULT_SECRET_PREV', '').strip()
    if prev:
        resp['vault_key_prev'] = prev
    out = runtime.Response(content=runtime.json.dumps(resp), media_type='application/json')
    runtime._desk_cookie_refresh(out)
    return out

def api_desk_drive_list(path: str=runtime.Query(default='/')):
    runtime._require_staff_or_host()
    (code, body, _) = runtime._notify_get('/drive/list?path=' + runtime.quote(path or '/', safe='/'))
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return runtime.json.loads(body.decode() or '{}')

def api_desk_drive_file(path: str=runtime.Query(default='')):
    from fastapi.responses import Response
    runtime._require_staff_or_host()
    if not path or path == '/':
        raise runtime.HTTPException(400, 'pick a file')
    (code, body, ctype) = runtime._notify_get('/drive/file?path=' + runtime.quote(path, safe='/'))
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    name = path.rsplit('/', 1)[-1] or 'file'
    return Response(content=body, media_type=ctype or 'application/octet-stream', headers={'Content-Disposition': f'inline; filename="{name}"'})

def api_desk_drive_save(req: runtime.DriveSaveRequest):
    """Overwrite a text file on the chart store (RAM upload)."""
    runtime._require_staff_or_host()
    return runtime._drive_http_save({'path': req.path, 'text': req.text, 'b64': req.b64})

def api_desk_drive_share(req: runtime.DriveShareRequest):
    """Create a Proton file share (cloak via Shlink when set) and send email and/or SMS."""
    runtime._require_staff()
    from app.messaging.notify import send_named_email, send_sms
    from app.portal.patients import normalize_phone
    path = (req.path or '').strip()
    if not path or path == '/':
        raise runtime.HTTPException(400, 'pick a file')
    (name, email, phone) = (req.name.strip(), req.email.strip(), req.phone.strip())
    if req.client_id:
        row = runtime.client_by_id(req.client_id)
        if not row:
            raise runtime.HTTPException(404, 'client not found')
        name = name or row.get('name') or ''
        email = email or row.get('email') or ''
        phone = phone or row.get('phone') or ''
    want_mail = bool(req.send_email)
    want_sms = bool(req.send_sms)
    if not want_mail and (not want_sms):
        raise runtime.HTTPException(400, 'check email, SMS, or both')
    if want_mail and (not email):
        raise runtime.HTTPException(400, 'need an email for that send')
    if want_sms and (not (phone or normalize_phone(phone))):
        raise runtime.HTTPException(400, 'need a phone for SMS')
    title = path.rsplit('/', 1)[-1] or 'file'
    (code, body) = runtime._notify_post('/drive/share', {'path': path, 'title': title})
    if code >= 400 or not body.get('ok'):
        raise runtime.HTTPException(code if code >= 400 else 502, body.get('error') or 'share failed')
    url = str(body.get('url') or '').strip()
    if not url.startswith('https://'):
        raise runtime.HTTPException(502, 'share failed')
    cloaked = bool(body.get('cloaked'))
    mail: dict[str, runtime.Any] = {'ok': False, 'skipped': True}
    sms: dict[str, runtime.Any] = {'ok': False, 'skipped': True}
    if want_mail:
        mail = send_named_email(kind='document', to=email, fields={'name': name or 'there', 'email': email, 'title': title, 'doc_title': title, 'url': url})
    if want_sms:
        first = (name or 'there').split()[0]
        sms = send_sms(phone, f'Hi {first}, a file is ready from the practice: {url} — {runtime.HOST_SHORT}', kind='file', source='desk', template='file')
    ok = bool(mail.get('ok') or sms.get('ok'))
    from app.infra.chart_store import ChartError, backend_name
    try:
        kind = 'cloak' if cloaked else backend_name()
    except ChartError as exc:
        raise runtime.HTTPException(exc.status if exc.status >= 400 else 502, str(exc)[:200]) from exc
    return {'ok': ok, 'cloaked': cloaked, 'kind': kind, 'email': {'ok': bool(mail.get('ok')), 'skipped': bool(mail.get('skipped')), 'detail': mail.get('detail') or ''}, 'sms': {'ok': bool(sms.get('ok')), 'queued': bool(sms.get('queued')), 'skipped': bool(sms.get('skipped')), 'detail': sms.get('detail') or ''}}

def api_desk_agent(req: runtime.DeskAgentRequest, background_tasks: runtime.BackgroundTasks, secret: str=runtime.Query(default='')):
    """Pa (Bedrock Nova Pro): look up, create, send forms/mail, book."""
    runtime._require_staff(secret)
    from app.portal.desk_agent import run_desk_agent

    def book_fn(args: dict) -> dict:
        book = runtime.BookRequest(event_type='follow-up', start=str(args.get('start') or ''), duration_minutes=int(args.get('duration_minutes') or 30), name=str(args.get('name') or ''), email=str(args.get('email') or ''), phone=str(args.get('phone') or ''), notes=str(args.get('notes') or ''), source='desk')
        return api_book(book, background_tasks)

    def send_fn(args: dict) -> dict:
        items = args.get('items') or []
        if isinstance(items, str):
            items = [items]
        fake = runtime.DeskSendRequest(items=[str(x) for x in items], name=str(args.get('name') or ''), email=str(args.get('email') or ''), send_email=bool(args.get('send_email', True)))
        return api_desk_send(fake, secret)

    def mail_fn(args: dict) -> dict:
        from app.messaging.notify import send_named_email
        kind = str(args.get('kind') or 'form_share')
        to = str(args.get('to') or '')
        fields = {'name': args.get('name') or '', 'email': to, 'title': args.get('title') or '', 'form_name': args.get('title') or kind, 'url': args.get('url') or '', 'notes': args.get('notes') or ''}
        return send_named_email(kind=kind, to=to, fields=fields)

    def link_fn(args: dict) -> dict:
        return runtime._desk_book_link(str(args.get('client_id') or ''), send_email=bool(args.get('send_email')), dry_run=bool(args.get('dry_run')))
    try:
        return run_desk_agent(req.messages, book_fn=book_fn, send_fn=send_fn, mail_fn=mail_fn, link_fn=link_fn)
    except Exception as e:
        raise runtime.HTTPException(502, str(e)[:200]) from e

def api_desk_send_items():
    runtime._require_staff()
    return {'ok': True, 'items': [{'id': k, 'label': v['label'], 'group': 'form'} for (k, v) in runtime.SEND_ITEMS.items()]}

def api_desk_send(req: runtime.DeskSendRequest):
    """Staff: email or copy one or more form links."""
    runtime._require_staff()
    ids = runtime._resolve_send_ids(req)
    if not ids:
        raise runtime.HTTPException(400, f'pick at least one form; use {sorted(runtime.SEND_ITEMS)}')
    (name, email) = (req.name.strip(), req.email.strip())
    if req.client_id:
        row = runtime.client_by_id(req.client_id)
        if not row:
            raise runtime.HTTPException(404, 'client not found')
        name = name or row['name']
        email = email or row['email']
    if not email and name:
        from app.booking.cal_reminders import match_client
        (hit, _how) = match_client(name, name)
        if hit and hit.get('email'):
            email = str(hit['email'])
    if not email:
        raise runtime.HTTPException(400, 'need an email (pick a client or type one)')
    links = runtime._build_send_links(ids, name=name, email=email)
    labels = [x['label'] for x in links]
    label = labels[0] if len(labels) == 1 else ', '.join(labels[:-1]) + ' and ' + labels[-1]
    first_url = links[0]['url']
    emailed = False
    mail: dict[str, runtime.Any] = {}
    if req.send_email:
        if len(links) == 1 and links[0]['id'] == 'intake':
            kind = 'intake'
        elif len(links) == 1:
            kind = 'form_share'
        else:
            kind = 'forms_bundle'
        fields = {'name': name, 'email': email, 'title': label, 'form_name': label, 'url': first_url, 'notes': req.notes, 'links': links}
        mail = runtime.send_named_email(kind=kind, to=email, fields=fields)
        emailed = bool(mail.get('ok'))
    return {'ok': True, 'item': ids[0], 'items': ids, 'label': label, 'url': first_url, 'links': links, 'emailed': emailed, 'email': email, 'name': name, 'emailDetail': None if emailed else mail.get('detail') if mail else None}

def desk_page():
    path = runtime.STATIC / 'desk.html'
    if not path.exists():
        raise runtime.HTTPException(404)
    html = runtime._paint_page(path.read_text(encoding='utf-8'))
    if runtime._demo_on():
        if 'data-demo="' not in html:
            html = html.replace('<html', '<html data-demo="1"', 1)
    return runtime.Response(content=html, media_type='text/html; charset=utf-8', headers={'Cache-Control': 'no-store'})

def desk_service_worker():
    path = runtime.STATIC / 'js' / 'desk-sw.js'
    if not path.exists():
        raise runtime.HTTPException(404)
    return runtime.FileResponse(path, media_type='application/javascript', headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

def favicon():
    return runtime.FileResponse(runtime.STATIC / 'favicon.ico', media_type='image/x-icon', headers={'Cache-Control': 'public, max-age=86400'})

def hub():
    return runtime.RedirectResponse('/new-client', status_code=302)
from app.routers.book import api_book

router.add_api_route('/api/internal/card-link', api_internal_card_link, methods=['POST'])
router.add_api_route('/api/desk/auth/send', api_desk_auth_send, methods=['POST'])
router.add_api_route('/api/desk/auth/legacy', api_desk_auth_legacy, methods=['POST'])
router.add_api_route('/api/desk/today', api_desk_today, methods=['GET'])
router.add_api_route('/api/desk/week', api_desk_week, methods=['GET'])
router.add_api_route('/api/desk/clients', api_desk_clients, methods=['GET'])
router.add_api_route('/api/internal/intake/submitted', api_internal_intake_submitted, methods=['POST'])
router.add_api_route('/api/desk/intake/link/{client_id}', api_desk_intake_link, methods=['POST'])
router.add_api_route('/api/internal/intake/progress', api_internal_intake_progress, methods=['GET'])
router.add_api_route('/api/desk/onboarding/hold/{event_id}/times', api_desk_hold_times, methods=['POST'])
router.add_api_route('/api/desk/onboarding/sweep', api_desk_hold_sweep, methods=['POST'])
router.add_api_route('/api/desk/onboarding/status', api_desk_onboarding_status, methods=['GET'])
router.add_api_route('/api/internal/client/card', api_internal_client_card, methods=['POST'])
router.add_api_route('/api/desk/refills', api_desk_refills, methods=['GET'])
router.add_api_route('/api/desk/refills/{rid}/sent', api_desk_refill_sent, methods=['POST'])
router.add_api_route('/api/desk/refills/summary', api_desk_refills_summary, methods=['GET'])
router.add_api_route('/api/desk/client', api_desk_client, methods=['GET'])
router.add_api_route('/api/desk/client/drive', api_desk_client_drive, methods=['GET'])
router.add_api_route('/api/desk/visit/flag', api_desk_visit_flag, methods=['POST'])
router.add_api_route('/api/desk/visit/move', api_desk_visit_move, methods=['POST'])
router.add_api_route('/api/practice', api_practice_public, methods=['GET'])
router.add_api_route('/api/practice/logo', api_practice_logo, methods=['GET'])
router.add_api_route('/api/practice/photo', api_practice_photo, methods=['GET'])
router.add_api_route('/api/practice/background', api_practice_background, methods=['GET'])
router.add_api_route('/api/desk/practice', api_desk_practice_get, methods=['GET'])
router.add_api_route('/api/desk/practice', api_desk_practice_save, methods=['POST'])
router.add_api_route('/api/desk/services', api_desk_services_get, methods=['GET'])
router.add_api_route('/api/desk/services', api_desk_services_save, methods=['POST'])
router.add_api_route('/api/desk/services', api_desk_services_delete, methods=['DELETE'])
router.add_api_route('/api/desk/practice/asset', api_desk_practice_asset, methods=['POST'])
router.add_api_route('/api/twilio/desk-voice', api_twilio_desk_voice, methods=['POST'])
router.add_api_route('/api/desk/meta', api_desk_meta, methods=['GET'])
router.add_api_route('/api/desk/drive/list', api_desk_drive_list, methods=['GET'])
router.add_api_route('/api/desk/drive/file', api_desk_drive_file, methods=['GET'])
router.add_api_route('/api/desk/drive/save', api_desk_drive_save, methods=['POST'])
router.add_api_route('/api/desk/drive/share', api_desk_drive_share, methods=['POST'])
router.add_api_route('/api/desk/agent', api_desk_agent, methods=['POST'])
router.add_api_route('/api/desk/send-items', api_desk_send_items, methods=['GET'])
router.add_api_route('/api/desk/send', api_desk_send, methods=['POST'])
router.add_api_route('/desk', desk_page, methods=['GET'])
router.add_api_route('/desk-sw.js', desk_service_worker, methods=['GET'])
router.add_api_route('/favicon.ico', favicon, methods=['GET'], include_in_schema=False)
router.add_api_route('/', hub, methods=['GET'])
