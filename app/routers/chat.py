from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def api_chat_thread_post(req: runtime.ChatThreadPost):
    from app.messaging.chat_threads import post_message
    name = req.name
    client_id = ''
    (email, phone) = (req.email, req.phone)
    if req.verify_token.strip():
        try:
            payload = runtime.verify_patient_token(req.verify_token.strip())
            from app.portal.patients import client_by_id
            crow = client_by_id(str(payload.get('id') or '')) or {}
            client_id = str(crow.get('id') or '')
            email = email or str(crow.get('email') or '')
            phone = phone or str(crow.get('phone') or '')
            name = name or str(crow.get('preferred_name') or crow.get('name') or '')[:80]
        except ValueError:
            pass
    if not (name or '').strip():
        from app.portal.verify_codes import _lookup
        found = _lookup(req.email or '', req.phone or '')
        if found:
            row = found[2]
            name = str(row.get('preferred_name') or row.get('display_name') or '')[:80]
    try:
        return post_message(body=req.body, token=req.token, label=req.label, email=email, phone=phone, hear_back=req.hear_back, sms_consent=req.sms_consent, name=name, client_id=client_id)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e

def api_chat_thread_get(token: str=runtime.Query(default=''), verify_token: str=runtime.Query(default='')):
    from app.messaging.chat_threads import _read, get_thread, thread_access
    if not token:
        raise runtime.HTTPException(400, 'token required')
    try:
        raw = _read(token)
        if raw and (not thread_access(raw, verify_token.strip())):
            return {'ok': True, 'token': token, 'login_required': True, 'messages': []}
        row = get_thread(token)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    if not row:
        raise runtime.HTTPException(404, 'thread not found')
    return row

async def api_spruce_webhook(request: runtime.Request):
    """Spruce conversationItem.created. Signature required. No message bodies in the response."""
    from app.messaging.chat_threads import handle_spruce_webhook
    raw = await request.body()
    sig = request.headers.get('x-spruce-signature') or ''
    result = handle_spruce_webhook(raw, sig)
    if result.get('unauthorized'):
        raise runtime.HTTPException(401, 'unauthorized')
    return {'ok': True, 'accepted': int(result.get('accepted') or 0)}

def api_sms_bridge_due(request: runtime.Request):
    """Counts only. No phone numbers. Worker uses this while send is off."""
    runtime._require_bridge(request)
    return {'ok': True, **runtime.due_summary()}

def api_sms_bridge_claim(request: runtime.Request):
    """Lease one due text for the desk Mac. Secret header. Not a public pull."""
    runtime._require_bridge(request)
    return {'ok': True, 'job': runtime.sms_bridge_claim()}

def api_sms_bridge_ack(req: runtime.SmsBridgeAck, request: runtime.Request):
    runtime._require_bridge(request)
    try:
        result = runtime.sms_bridge_ack(req.id, req.status)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    if not result.get('ok'):
        raise runtime.HTTPException(400, result.get('detail') or 'ack failed')
    return result

def api_sms_bridge_enqueue(req: runtime.SmsBridgeEnqueue, request: runtime.Request):
    """Voice or website chat may queue a logistics template. No free text."""
    runtime._require_bridge(request)
    if not runtime.bridge_enabled():
        raise runtime.HTTPException(409, 'sms bridge off')
    source = (req.source or '').strip().lower()
    if source not in ('voice', 'chat'):
        raise runtime.HTTPException(400, 'source must be voice or chat')
    template = (req.template or '').strip()
    if template not in ('book_link', 'join'):
        raise runtime.HTTPException(400, 'template not allowed')
    from app.messaging.notify import normalize_e164
    phone = normalize_e164(req.phone)
    if not phone:
        raise runtime.HTTPException(400, 'invalid phone')
    body = runtime.render_agent_template(template)
    return runtime.sms_bridge_enqueue(to=phone, body=body, kind=template, source=source, idempotency_key=(req.idempotency_key or '').strip(), template=template)

def api_desk_mail_setup():
    runtime._require_staff()
    (code, body, _) = runtime._notify_get('/mail/setup')
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return runtime.json.loads(body.decode() or '{}')

def api_desk_mail_list(folder: str=runtime.Query(default='Desk'), unread: bool=runtime.Query(default=False)):
    runtime._require_staff()
    q = '/mail/list?folder=' + runtime.quote(folder or 'Desk', safe='')
    if unread:
        q += '&unread=1'
    (code, body, _) = runtime._notify_get(q)
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return runtime.json.loads(body.decode() or '{}')

def api_desk_mail_thread(id: str=runtime.Query(default='')):
    runtime._require_staff()
    if not id.strip():
        raise runtime.HTTPException(400, 'pick a thread')
    (code, body, _) = runtime._notify_get('/mail/thread?id=' + runtime.quote(id.strip(), safe=''))
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    data = runtime.json.loads(body.decode() or '{}')
    email = (data.get('from') or '').strip()
    client = None
    if email:
        row = runtime.client_by_email(email)
        if row:
            client = {'id': row.get('id') or '', 'name': row.get('name') or '', 'email': row.get('email') or ''}
    data['client'] = client
    return data

def api_desk_mail_attachment(message: str=runtime.Query(default=''), id: str=runtime.Query(default=''), name: str=runtime.Query(default='file')):
    from fastapi.responses import Response
    runtime._require_staff()
    if not message.strip() or not id.strip():
        raise runtime.HTTPException(400, 'pick a file')
    q = '/mail/attachment?message=' + runtime.quote(message.strip(), safe='') + '&id=' + runtime.quote(id.strip(), safe='') + '&name=' + runtime.quote((name or 'file')[:120], safe='')
    (code, body, ctype) = runtime._notify_get(q)
    if code != 200:
        raise runtime.HTTPException(code if code >= 400 else 502, body.decode()[:200])
    safe = (name or 'file').replace('/', '_').replace('\\', '_')[:120]
    return Response(content=body, media_type=ctype or 'application/octet-stream', headers={'Content-Disposition': f'attachment; filename="{safe}"'})

def api_desk_mail_reply(req: runtime.MailReplyRequest):
    runtime._require_staff()
    files = [{'name': f.name, 'b64': f.b64} for f in (req.files or [])[:4] if f.b64]
    (code, payload) = runtime._notify_post('/mail/reply', {'id': req.id, 'body': req.body, 'files': files})
    if code != 200 or not payload.get('ok'):
        raise runtime.HTTPException(code if code >= 400 else 502, payload.get('error') or payload.get('detail') or 'reply failed')
    return payload

def api_late(req: runtime.LateSendRequest):
    """Staff-fired running-late email + SMS. kind=late_soon | late_25."""
    runtime._require_staff()
    kind = req.kind if req.kind in ('late_soon', 'late_25', 'no_show') else 'late_soon'
    cfg = runtime.load_config()
    tz = runtime.ZoneInfo(cfg.get('timezone', runtime.timezone_name()))
    cid = req.calendar_id or runtime.os.environ.get('GOOGLE_CALENDAR_ID') or runtime.DEFAULT_CALENDAR
    try:
        ev = runtime.get_event(cid, req.event_id)
    except Exception as e:
        raise runtime.HTTPException(404, f'event not found: {e}') from e
    fields = runtime._event_mail_fields(ev, cid, tz)
    if not fields.get('start_iso'):
        raise runtime.HTTPException(400, 'event has no start time')
    if kind == 'no_show':
        to = fields.get('email') or ''
        if not to:
            raise runtime.HTTPException(400, 'no patient email on this visit')
        result = runtime.send_named_email(kind='no_show', to=to, fields=fields)
        return {'ok': result.get('ok'), 'kind': kind, **result, 'visit': fields}
    result = runtime.send_late(kind=kind, name=fields['name'], email=fields['email'], phone=fields['phone'], title=fields['title'], duration_minutes=fields.get('duration_minutes'), start_iso=fields['start_iso'], sms_consent=bool(fields.get('sms_consent')), rebook_url=fields['rebook_url'], now_iso=runtime.datetime.now(tz).isoformat())
    return {'ok': result.get('ok'), 'kind': kind, **result, 'visit': fields}

def api_mail_send(req: runtime.MailSendRequest):
    """Send any branded suite email (intake, form, pay, document, superbill…)."""
    runtime._require_staff()
    from app.messaging.email_templates import SUITE
    if req.kind not in SUITE:
        raise runtime.HTTPException(400, f'kind must be one of {sorted(SUITE)}')
    fields = req.model_dump()
    try:
        result = runtime.send_named_email(kind=req.kind, to=str(req.to), fields=fields)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    return {'ok': result.get('ok'), 'kind': req.kind, 'to': str(req.to), **result}

def api_mail_kinds():
    runtime._require_staff()
    from app.messaging.email_templates import SUITE
    return {'kinds': SUITE}

def page_chat_link(code: str, request: runtime.Request=None):
    """Chat-reply alert link. GET shows only a button: scanners and crawlers never log in or see content."""
    import app.messaging.chat_links as chat_links
    ip = runtime._c_ip(request)
    if ip and (not runtime._b_rate_ok('c:' + ip)):
        return runtime.HTMLResponse('Too many tries. Wait a minute and try again.', status_code=429, headers=runtime._NO_INDEX)
    if not chat_links.lookup(code):
        return runtime._c_expired()
    return runtime.HTMLResponse(runtime._C_PAGE.format(title=runtime.HOST_SHORT + ' replied to your message', body=f"<form method=post action='/c/{code}'><button type=submit style='font-size:1.1em;padding:.7em 1.4em;border-radius:8px;border:0;background:#1f4e79;color:#fff'>Open my messages</button></form>"), headers=runtime._NO_INDEX)

def page_chat_link_open(code: str, request: runtime.Request=None):
    """Button press: mint the normal portal session (same TTL as a code login) and hand it to the site in the #fragment."""
    from urllib.parse import quote
    import app.messaging.chat_links as chat_links
    from app.portal.patients import client_by_id
    ip = runtime._c_ip(request)
    if ip and (not runtime._b_rate_ok('c:' + ip)):
        return runtime.HTMLResponse('Too many tries. Wait a minute and try again.', status_code=429, headers=runtime._NO_INDEX)
    hit = chat_links.lookup(code)
    row = client_by_id(hit['client_id']) if hit else None
    if not hit or not row:
        return runtime._c_expired()
    name = str(row.get('preferred_name') or row.get('given_name') or row.get('name') or '')[:80]
    vt = runtime.mint_verify_token(client_id=str(row['id']), name=name, matched='chat_link')
    frag = f"cv={quote(vt, safe='')}&cn={quote(name, safe='')}&t={quote(hit['thread'], safe='')}"
    return runtime.RedirectResponse(f'{runtime.SITE_PUBLIC}/portal#{frag}', status_code=303, headers=runtime._NO_INDEX)

def api_desk_sms_optouts(request: runtime.Request):
    """Desk only: clients with texts off. stop = replied STOP in Spruce; unchecked = form box left off."""
    runtime._require_staff('', request)
    with runtime.clients_db.connect() as con:
        rows = con.execute('SELECT id, display_name, sms_consent_at, sms_consent_source FROM clients WHERE sms_consent = 0 ORDER BY sms_consent_at DESC').fetchall()
    out: dict[str, list[dict[str, runtime.Any]]] = {'stop': [], 'unchecked': []}
    for r in rows:
        item = {'id': r[0], 'name': r[1] or '', 'at': r[2] or '', 'source': r[3] or ''}
        out['stop' if (r[3] or '') == 'spruce' else 'unchecked'].append(item)
    return runtime.JSONResponse({'ok': True, **out}, headers={'Cache-Control': 'no-store'})

router.add_api_route('/api/public/chat-threads', api_chat_thread_post, methods=['POST'])
router.add_api_route('/api/public/chat-threads', api_chat_thread_get, methods=['GET'])
router.add_api_route('/api/spruce/webhook', api_spruce_webhook, methods=['POST'])
router.add_api_route('/api/sms-bridge/due', api_sms_bridge_due, methods=['GET'])
router.add_api_route('/api/sms-bridge/claim', api_sms_bridge_claim, methods=['POST'])
router.add_api_route('/api/sms-bridge/ack', api_sms_bridge_ack, methods=['POST'])
router.add_api_route('/api/sms-bridge/enqueue', api_sms_bridge_enqueue, methods=['POST'])
router.add_api_route('/api/desk/mail/setup', api_desk_mail_setup, methods=['GET'])
router.add_api_route('/api/desk/mail/list', api_desk_mail_list, methods=['GET'])
router.add_api_route('/api/desk/mail/thread', api_desk_mail_thread, methods=['GET'])
router.add_api_route('/api/desk/mail/attachment', api_desk_mail_attachment, methods=['GET'])
router.add_api_route('/api/desk/mail/reply', api_desk_mail_reply, methods=['POST'])
router.add_api_route('/api/late', api_late, methods=['POST'])
router.add_api_route('/api/mail/send', api_mail_send, methods=['POST'])
router.add_api_route('/api/mail/kinds', api_mail_kinds, methods=['GET'])
router.add_api_route('/c/{code}', page_chat_link, methods=['GET'])
router.add_api_route('/c/{code}', page_chat_link_open, methods=['POST'])
router.add_api_route('/api/desk/sms-optouts', api_desk_sms_optouts, methods=['GET'])
