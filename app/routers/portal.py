from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def api_verify_patient(req: runtime.VerifyRequest, request: runtime.Request):
    """Phone or email on file can file a message. No name, no token, no chart."""
    from app.portal.verify_codes import _lookup, recognize_for_message
    if not (req.email or '').strip() and (not (req.phone or '').strip()):
        return {'ok': False, 'level': 'none', 'reason': 'need_email_or_phone'}
    out = recognize_for_message(email=req.email, phone=req.phone)
    if out.get('ok') and runtime._phone_channel(request):
        row = _lookup(req.email, req.phone)[2]
        ans = (req.sms_consent or '').strip().lower()
        if ans in ('yes', 'no'):
            runtime.clients_db.set_sms_consent(row['id'], ans == 'yes', 'voice')
            row = runtime.clients_db.get_by_id(row['id'])
        out['sms_consent'] = runtime.clients_db.sms_consent_state(row)
    return out

def api_verify_code_send(req: runtime.VerifyRequest):
    """Send a code before any visit, note, or billing detail."""
    from app.portal.verify_codes import start_verify
    return start_verify(email=req.email, phone=req.phone)

def api_verify_patient_code(req: runtime.VerifyCodeRequest):
    """Mint a token only after the code. No chart fields."""
    from app.portal.verify_codes import confirm_code
    result = confirm_code(email=req.email, phone=req.phone, code=req.code)
    if result.get('ok') and (not result.get('multi')):
        result['level'] = 'sensitive'
        result['name'] = str(runtime.verify_patient_token(result['verify_token']).get('n') or '')[:80]
    return result

def api_verify_accounts(req: runtime.AccountsRequest):
    """Accounts on a code-verified contact (first name + last initial). Contact token only; no chart."""
    from app.portal.verify_codes import list_contact_accounts
    return list_contact_accounts(req.contact_token)

def api_verify_pick(req: runtime.PickAccountRequest):
    """Open (or switch to) one account on the verified contact. New token scoped to that client; old one revoked."""
    from app.portal.verify_codes import pick_account
    return pick_account(req.contact_token, req.ref, req.old_token)

def api_public_logout(req: runtime.PublicTokenRequest):
    """Revoke this browser's portal session. Always ok (no oracle)."""
    import app.portal.portal_sessions as portal_sessions
    if portal_sessions.is_session_token(req.verify_token or ''):
        portal_sessions.revoke(req.verify_token.strip())
    return {'ok': True}

async def api_public_portal_upload(background_tasks: runtime.BackgroundTasks, verify_token: str=runtime.Form(default=''), thread: str=runtime.Form(default=''), note: str=runtime.Form(default=''), file: runtime.UploadFile=runtime.File(...)):
    """Portal-only patient upload: verified patient, one file, sniffed + limited, filed to Chart/Uploads."""
    import base64
    import app.portal.uploads as uploads
    from app.messaging.chat_threads import TOKEN_RE, post_message
    if not verify_token.strip():
        raise runtime.HTTPException(401, 'Log in to your portal to send files.')
    row = runtime._verified_client(verify_token.strip())
    blob = await file.read(uploads.MAX_BYTES + 1)

    def _file_into_chart(name: str, data: bytes) -> str:
        chart = runtime._chart_for(row)
        if not chart:
            raise RuntimeError('no chart folder')
        folder = runtime._ensure_visit_notes(chart, 'Uploads')
        dest = folder + '/' + name
        (code, saved) = runtime._notify_post('/drive/save', {'path': dest, 'b64': base64.b64encode(data).decode('ascii')})
        if code >= 400 or not saved.get('ok'):
            raise RuntimeError('drive save failed')
        return dest
    try:
        res = uploads.handle(row, blob, file_into_chart=_file_into_chart)
    except uploads.UploadError as e:
        raise runtime.HTTPException(e.status, str(e)) from e
    label = runtime._UPLOAD_LABEL.get(res['kind'], 'file')
    size = res['size']
    pretty = f'{size / 1048576:.1f} MB' if size >= 1048576 else f'{max(1, round(size / 1024))} KB'
    article = 'an' if label[:1].lower() in 'aeiou' else 'a'
    line = f'📎 Sent {article} {label} ({pretty}). ' + ('This file is already in the chart.' if res.get('duplicate') else 'Filed privately to the chart.')
    caption = (note or '').strip()[:500]
    if caption:
        line = f'{line}\n{caption}'
    thread_out = ''
    try:
        t = post_message(body=line, token=thread if TOKEN_RE.match(thread or '') else '', label='Portal upload', email=str(row.get('email') or ''), name=str(row.get('name') or ''))
        thread_out = str(t.get('token') or '')
    except Exception:
        uploads.log(str(row.get('id') or ''), res['size'], res['kind'], 'thread_note_failed')
    if not res.get('duplicate'):
        background_tasks.add_task(runtime._upload_email, row, res, blob, caption)
    return {'ok': True, 'duplicate': bool(res.get('duplicate')), 'kind': res['kind'], 'size': res['size'], 'message': line, 'thread': thread_out}

def api_public_upcoming(req: runtime.PublicTokenRequest):
    """Next visits for a verified current patient. No chart."""
    row = runtime._verified_client(req.verify_token)
    tz = runtime.ZoneInfo(runtime.load_config().get('timezone', runtime.timezone_name()))
    visits = runtime._visits_for_client(row, tz)
    upcoming = [v for v in visits if v.get('upcoming')]
    upcoming.sort(key=lambda v: v.get('start_iso') or '')
    return {'ok': True, 'name': row.get('name') or '', 'visits': [{'event_id': v.get('event_id') or '', 'title': v.get('title') or 'Visit', 'when_label': v.get('when_label') or '', 'duration_minutes': v.get('duration_minutes'), 'start_iso': v.get('start_iso') or ''} for v in upcoming[:6]]}

def api_public_cancel_visit(req: runtime.PublicCancelVisitRequest):
    """Cancel one of this verified patient's visits. Used to reschedule."""
    row = runtime._verified_client(req.verify_token)
    tz = runtime.ZoneInfo(runtime.load_config().get('timezone', runtime.timezone_name()))
    match = None
    for v in runtime._visits_for_client(row, tz):
        if str(v.get('event_id') or '') == req.event_id:
            match = v
            break
    if not match:
        raise runtime.HTTPException(404, 'visit not found')
    runtime.delete_event(match['calendar_id'], match['event_id'])
    return {'ok': True, 'cancelled': match.get('when_label') or ''}

def api_public_folders(req: runtime.PublicTokenRequest):
    """Cloaked visit-notes and superbill folder links. No note bodies."""
    row = runtime._verified_client(req.verify_token)
    return {'ok': True, 'notes_url': row.get('chart_url') or '', 'billing_url': row.get('billing_url') or '', 'notes_label': 'Visit notes', 'billing_label': 'Superbills'}

def api_public_balance(req: runtime.PublicTokenRequest):
    """Owed amount for a verified patient. No line-item detail."""
    row = runtime._verified_client(req.verify_token)
    from app.billing.fees import dollars, owed_cents
    cents = owed_cents(str(row.get('id') or ''))
    return {'ok': True, 'owed_cents': cents, 'owed_pretty': dollars(cents)}

def api_desk_auth_verify(req: runtime.DeskAuthCode, request: runtime.Request):
    import app.portal.desk_auth as desk_auth
    tok = desk_auth.verify_code(req.email, req.code, runtime._client_ip(request))
    if not tok:
        raise runtime.HTTPException(401, "That code didn't work. Request a new one if it expired.")
    out = runtime.Response(content=runtime.json.dumps({'ok': True}), media_type='application/json')
    runtime._set_desk_cookie(out, tok)
    out.delete_cookie(runtime.STAFF_COOKIE, path='/')
    return out

def api_desk_auth_logout(request: runtime.Request):
    import app.portal.desk_auth as desk_auth
    desk_auth.revoke(request.cookies.get(desk_auth.COOKIE, ''))
    out = runtime.Response(content=runtime.json.dumps({'ok': True}), media_type='application/json')
    out.delete_cookie(desk_auth.COOKIE, path='/')
    out.delete_cookie(runtime.STAFF_COOKIE, path='/')
    return out

router.add_api_route('/api/verify-patient', api_verify_patient, methods=['POST'])
router.add_api_route('/api/verify-patient/code-send', api_verify_code_send, methods=['POST'])
router.add_api_route('/api/verify-patient/code', api_verify_patient_code, methods=['POST'])
router.add_api_route('/api/verify-patient/accounts', api_verify_accounts, methods=['POST'])
router.add_api_route('/api/verify-patient/pick', api_verify_pick, methods=['POST'])
router.add_api_route('/api/public/logout', api_public_logout, methods=['POST'])
router.add_api_route('/api/public/portal/upload', api_public_portal_upload, methods=['POST'])
router.add_api_route('/api/public/upcoming', api_public_upcoming, methods=['POST'])
router.add_api_route('/api/public/cancel-visit', api_public_cancel_visit, methods=['POST'])
router.add_api_route('/api/public/folders', api_public_folders, methods=['POST'])
router.add_api_route('/api/public/balance', api_public_balance, methods=['POST'])
router.add_api_route('/api/desk/auth/verify', api_desk_auth_verify, methods=['POST'])
router.add_api_route('/api/desk/auth/logout', api_desk_auth_logout, methods=['POST'])
