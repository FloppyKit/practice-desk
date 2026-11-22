from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def api_desk_note_sign(req: runtime.DeskNoteSignRequest, background_tasks: runtime.BackgroundTasks):
    """Sign an after-visit summary to Chart/Visit notes. Body never logged."""
    import base64
    from app.infra.drive_paths import LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths
    from app.notes.note_pdf import after_visit_pdf, markdown_pdf
    from app.notes.note_templates import get_template, render_template
    runtime._require_staff()
    from app.billing.billing import letterhead_missing, refusal_message
    from app.infra.practice import load as load_practice
    missing = letterhead_missing(load_practice())
    if missing:
        raise runtime.HTTPException(409, refusal_message(missing))
    row = runtime.client_by_id(req.client_id) if req.client_id else None
    if not row and req.email:
        row = runtime.client_by_email(req.email)
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    stored = paths_from_row(row)
    chart = stored.get('chart') or ''
    if not chart:
        try:
            chart = resolve_client_paths(row, runtime._drive_list).get('chart') or ''
        except Exception:
            chart = ''
    if not chart:
        dest = person_folder_name(row)
        if dest:
            chart = f'{LIVING_ROOT}/{dest}/Chart'
    if not chart:
        raise runtime.HTTPException(400, 'no chart folder for this client')
    dos = (req.dos or '').strip()[:10]
    if len(dos) != 10:
        dos = runtime.datetime.now().date().isoformat()
    given = (row.get('given_name') or '').strip()
    family = (row.get('family_name') or '').strip()
    ini = (given[:1] + family[:1]).upper() if given and family else ''
    tid = (req.template or 'avs').strip().lower()
    if not get_template(tid):
        tid = 'avs'
    slug = 'avs' if tid == 'avs' else tid
    stem = f'{dos} {ini} {slug}'.strip() if ini else f'{dos} {slug}'
    vn = runtime._ensure_visit_notes(chart)
    fname = runtime._unique_pdf_name(vn, stem)
    dest = vn.rstrip('/') + '/' + fname
    fields = {'name': req.name or row.get('name') or '', 'dob': req.dob or row.get('dob') or '', 'physician': req.physician or '', 'dos': dos, 'return_visit': req.return_visit, 'medications': req.medications, 'supplements': req.supplements, 'recommendations': req.recommendations, 'long_run': req.long_run, 'data': req.data, 'subjective': req.subjective, 'objective': req.objective, 'assessment': req.assessment, 'plan': req.plan, 'addendum': req.addendum, 'addendum_date': runtime.datetime.now().date().isoformat(), 'duration': req.duration}
    spec = get_template(tid) or {}
    for (k, v) in (req.values or {}).items():
        if isinstance(k, str) and k.isidentifier():
            fields[k] = str(v or '')[:8000]
    from app.infra.practice import load as load_practice
    prac = load_practice()
    if not fields['physician']:
        fields['physician'] = prac.get('clinician_name') or 'Example Clinician, D.O.'
    from app.notes.note_pdf import CHART_ONLY
    from app.infra.practice import asset_bytes, format_licenses
    if not str(fields.get('return_visit') or '').strip():
        fields['return_visit'] = runtime._booked_meet_again(str(row.get('id') or req.client_id), req.event_id, dos)
    letterhead = {'name': prac.get('display_name') or '', 'address': prac.get('address') or '', 'phone': prac.get('phone') or '', 'fax': prac.get('fax') or '', 'email': prac.get('email') or '', 'clinician': prac.get('clinician_name') or '', 'licenses': format_licenses(prac.get('licenses') or [])}
    logo = asset_bytes('logo')
    sections = spec.get('sections') or []
    is_avs = tid in ('avs', 'avs-followup')
    chart_pdf = b''
    if is_avs:
        pdf = after_visit_pdf(fields, title='After Visit Summary', letterhead=letterhead, patient=True, logo=logo)
        if any((str(fields.get(k) or '').strip() for k in CHART_ONLY)):
            chart_pdf = after_visit_pdf(fields, sections, title=str(spec.get('title') or 'After-Visit Summary') + ' (chart copy)', letterhead=letterhead, logo=logo)
    elif sections:
        chart_title = str(spec.get('title') or '')
        if tid == 'soap':
            chart_title = (chart_title or 'SOAP note') + ' (chart only)'
        pdf = after_visit_pdf(fields, sections, title=chart_title, letterhead=letterhead, logo=logo)
    else:
        pdf = markdown_pdf(render_template(tid, fields))
    def _save(path: str, blob: bytes) -> None:
        runtime._drive_http_save({'path': path, 'b64': base64.b64encode(blob).decode('ascii')})
    try:
        _save(dest, pdf)
    except runtime.HTTPException as e:
        detail = str(getattr(e, 'detail', '') or '').lower()
        missing = e.status_code >= 400 and any((w in detail for w in ('not found', 'no such', 'folder', 'exist', 'parent')))
        if not missing:
            raise
        vn = runtime._ensure_visit_notes(chart)
        fname = runtime._unique_pdf_name(vn, stem)
        dest = vn + '/' + fname
        _save(dest, pdf)
    chart_dest = ''
    if chart_pdf:
        chart_dest = dest[:-len('.pdf')] + ' chart.pdf'
        background_tasks.add_task(_save, chart_dest, chart_pdf)
    import app.notes.signed_notes as signed_notes
    note_id = signed_notes.record(client_id=str(row.get('id') or ''), dos=dos, template=tid, fields=fields, avs_path=dest if is_avs else '', avs_name=fname if is_avs else '', chart_path=chart_dest)
    try:
        from app.notes.style_pack_learn import learn_safe
        background_tasks.add_task(learn_safe, dict(fields), tid)
    except Exception:
        pass
    try:
        from app.notes.note_prefs import remember_signed
        background_tasks.add_task(remember_signed, str(row.get('id') or ''), str(req.event_id or ''), dict(fields), dos=dos)
    except Exception:
        pass
    return {'ok': True, 'path': dest, 'name': fname, 'bytes': len(pdf), 'template': tid, 'note_id': note_id, 'chart_path': chart_dest, 'shareable': is_avs}

def api_desk_note_last_avs(client_id: str=runtime.Query(default='')):
    """Last signed AVS for a client: prefill fields for the next note + its share status."""
    runtime._require_staff()
    import app.billing.billing as billing
    import app.notes.signed_notes as signed_notes
    note = signed_notes.last_for_client(client_id.strip())
    if not note:
        return {'ok': True, 'note': None, 'prefill': {}}
    share = billing.active_share(note['id'], 'avs') if note.get('avs_path') else None
    return {'ok': True, 'note': {'id': note['id'], 'dos': note['dos'], 'template': note['template'], 'name': note.get('avs_name') or '', 'signed_at': note['signed_at'], 'shareable': bool(note.get('avs_path'))}, 'prefill': signed_notes.prefill(note), 'share': share}

def api_desk_note_drive_prior(client_id: str=runtime.Query(default=''), event_id: str=runtime.Query(default=''), dos: str=runtime.Query(default='')):
    """Most recent Visit Notes file on Proton Drive for the open chart. Body never logged."""
    runtime._require_staff()
    from urllib.parse import quote
    from app.notes.drive_note import name_dos, parse_note_text, pick_latest, text_of
    from app.infra.drive_paths import VISIT_NOTES_FOLDER, archive_chart_paths, client_file_folders, paths_from_row

    def pick_in(chart_path: str):
        chart_path = (chart_path or '').rstrip('/')
        if not chart_path:
            return ('', None)
        kids = runtime._folder_names(chart_path) or {}
        vn_path = chart_path + '/' + (kids.get('visit notes') or VISIT_NOTES_FOLDER)
        listing = runtime._drive_list(vn_path)
        return (vn_path, pick_latest(listing.get('items') or []))
    cid = client_id.strip()
    row = runtime.client_by_id(cid) if cid else None
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    chart = (paths_from_row(row).get('chart') or '').rstrip('/')
    if not chart:
        return {'ok': True, 'prior': None, 'detail': 'no chart folder', 'meet_again': runtime._booked_meet_again(cid, event_id.strip(), dos.strip())}
    (vn, picked) = pick_in(chart)
    if not picked:
        try:
            listed = [str(it.get('name') or '') for it in client_file_folders(runtime._drive_list) if (it.get('type') or '') == 'folder']
        except Exception:
            listed = []
        for alt in archive_chart_paths(row, listed):
            if alt.rstrip('/') == chart:
                continue
            (alt_vn, alt_picked) = pick_in(alt)
            if alt_picked:
                (vn, picked) = (alt_vn, alt_picked)
                break
    if not picked:
        return {'ok': True, 'prior': None, 'path': vn, 'meet_again': runtime._booked_meet_again(cid, event_id.strip(), dos.strip())}
    name = str(picked.get('name') or '')
    (code, blob, _ctype) = runtime._notify_get('/drive/file?path=' + quote(vn + '/' + name, safe='/'))
    if code != 200 or not blob:
        from app.infra.chart_store import ChartError, backend_name
        try:
            label = {'proton': 'Proton Drive', 'gdrive': 'Google Drive', 'nextcloud': 'Nextcloud'}.get(backend_name(), 'the chart')
        except ChartError:
            label = 'the chart'
        return {'ok': False, 'detail': f'could not read the note from {label}'}
    text = text_of(name, blob)
    fields = parse_note_text(text)
    from app.notes.note_pdf import reflow_body
    fields = {k: reflow_body(v) if k != 'dos' else v for (k, v) in fields.items()}
    return {'ok': True, 'meet_again': runtime._booked_meet_again(cid, event_id.strip(), dos.strip()), 'prior': {'name': name, 'dos': name_dos(name), 'path': vn + '/' + name, 'fields': fields}}

def api_desk_note_carry(client_id: str=runtime.Query(default=''), event_id: str=runtime.Query(default=''), visit_start: str=runtime.Query(default='')):
    """Most recent note (signed first, else imported) mapped for the next draft, plus which transcript
    'Update from transcript' would use (linked to this visit, else newest within 48 h). Bodies never logged."""
    runtime._require_staff()
    import app.notes.note_carry as note_carry
    cid = client_id.strip()
    (text, source) = note_carry.transcript_for(cid, event_id.strip())
    picks: list[dict[str, runtime.Any]] = []
    if source != 'linked':
        b = runtime._ct_day_bounds(visit_start) if visit_start else None
        if not b:
            now = runtime.datetime.now(runtime.timezone.utc)
            b = (now - runtime.timedelta(hours=48), now + runtime.timedelta(minutes=5))
        picks = note_carry.unlinked_on_day(cid, *b)
    heard = note_carry.coverage(text) if text else {'heard': 'none', 'flags': ['missing'], 'chars': 0, 'words': 0, 'note': 'No transcript for this visit.'}
    return {'ok': True, 'carry': note_carry.carry(cid), 'transcript_available': bool(text), 'transcript_source': source, 'transcript_choices': picks, 'coverage': {k: heard[k] for k in ('heard', 'flags', 'note', 'chars', 'words')}}

def api_desk_note_update_from_transcript(req: runtime.DeskNoteUpdateRequest):
    """Button click only: revise the draft from the visit transcript (Bedrock, AWS BAA). Never signs."""
    runtime._require_staff()
    import app.notes.note_carry as note_carry
    tr = (req.transcript or '').strip()
    if not tr:
        if req.transcript_id.strip() and note_carry.is_multi(req.transcript_id.strip()):
            raise runtime.HTTPException(409, "This transcript covers more than one visit, so it can't update this note (it could mix patients). Use a transcript from this visit only.")
        (tr, _src) = note_carry.transcript_for(req.client_id.strip(), req.event_id.strip(), req.transcript_id.strip())
    if len(tr) < 20:
        raise runtime.HTTPException(400, 'No transcript for this visit')
    heard = note_carry.coverage(tr)
    if 'thin' in heard['flags']:
        return {'ok': True, 'changed': {}, 'coverage': heard}
    visit_day = str((req.fields or {}).get('dos') or '')[:10]
    (next_day, next_minutes) = runtime._next_booked_visit(req.client_id.strip(), req.event_id.strip(), visit_day)
    try:
        changed = note_carry.update_from_transcript(dict(req.fields or {}), tr, client_id=req.client_id.strip(), event_id=req.event_id.strip(), visit_day=visit_day, next_visit_day=next_day, next_visit_minutes=next_minutes)
    except Exception as e:
        raise runtime.HTTPException(502, 'Update failed: ' + type(e).__name__) from e
    (_line, meet_from) = note_carry.meet_again_line(visit_day, next_day, tr, next_minutes=next_minutes)
    return {'ok': True, 'changed': changed, 'coverage': heard, 'meet_again_from': meet_from}

def api_desk_note_preview(req: runtime.DeskNoteSignRequest):
    """Patient AVS as it would be shared. Does not sign, save, or send."""
    runtime._require_staff()
    from app.notes.note_pdf import after_visit_pdf
    from app.notes.note_templates import get_template
    from app.billing.billing import letterhead_missing, refusal_message
    from app.infra.practice import asset_bytes, format_licenses, load as load_practice
    missing = letterhead_missing(load_practice())
    if missing:
        raise runtime.HTTPException(409, refusal_message(missing))
    row = runtime.client_by_id(req.client_id) if req.client_id else None
    if not row and req.email:
        row = runtime.client_by_email(req.email)
    row = row or {}
    prac = load_practice()
    dos = (req.dos or '').strip()[:10]
    fields = {'name': req.name or row.get('name') or '', 'dob': req.dob or row.get('dob') or '', 'physician': req.physician or prac.get('clinician_name') or '', 'dos': dos, 'return_visit': req.return_visit, 'medications': req.medications, 'supplements': req.supplements, 'recommendations': req.recommendations, 'subjective': req.subjective, 'objective': req.objective, 'assessment': req.assessment, 'plan': req.plan}
    for (k, v) in (req.values or {}).items():
        if isinstance(k, str) and k.isidentifier():
            fields[k] = str(v or '')[:8000]
    if not str(fields.get('return_visit') or '').strip():
        fields['return_visit'] = runtime._booked_meet_again(str(row.get('id') or req.client_id), req.event_id, dos)
    letterhead = {'name': prac.get('display_name') or '', 'address': prac.get('address') or '', 'phone': prac.get('phone') or '', 'fax': prac.get('fax') or '', 'email': prac.get('email') or '', 'clinician': prac.get('clinician_name') or '', 'licenses': format_licenses(prac.get('licenses') or [])}
    logo = asset_bytes('logo')
    tid = (req.template or 'avs').strip().lower()
    spec = get_template(tid) or {}
    if tid in ('avs', 'avs-followup', ''):
        pdf = after_visit_pdf(fields, title='After Visit Summary', letterhead=letterhead, patient=True, logo=logo)
    else:
        pdf = after_visit_pdf(fields, spec.get('sections') or [], title=(spec.get('title') or 'Note') + ' preview', letterhead=letterhead, logo=logo)
    return runtime.Response(content=pdf, media_type='application/pdf', headers={'Content-Disposition': 'inline; filename=preview.pdf', 'Cache-Control': 'no-store'})

def api_desk_note_avs_share(req: runtime.AvsShareRequest):
    """Send (or re-send) the signed AVS: turn off any old link, mint a fresh 30-day one, email + Spruce text."""
    runtime._require_staff()
    import app.billing.billing as billing
    import app.notes.signed_notes as signed_notes
    from app.billing.superbill_pdf import pretty_date
    note = signed_notes.get(req.note_id.strip())
    if not note or not note.get('avs_path'):
        raise runtime.HTTPException(404, 'signed AVS not found')
    view = dict(runtime.client_by_id(note['client_id']) or {})
    if not view:
        raise runtime.HTTPException(404, 'client not found')
    if req.to_email.strip():
        view['email'] = req.to_email.strip()
    if req.to_phone.strip():
        view['phone'] = req.to_phone.strip()
    revoked = billing.revoke_shares(note['id'], 'avs')
    link = billing.mint_share(billing_id=note['id'], client_id=note['client_id'], source='avs', path=note['avs_path'], name=note.get('avs_name') or 'After Visit Summary.pdf', kind='avs')
    url = f"{runtime.PUBLIC_BASE}/avs/{link['token']}"
    first = (str(view.get('name') or 'there').split() or ['there'])[0]
    sent = runtime._bill_send(kind='avs', view=view, fields={'name': view.get('name') or '', 'url': url, 'dos_label': pretty_date(note['dos'])}, sms_body=f'Hi {first}, your after-visit summary from {runtime.HOST_SHORT} (link good for 30 days): {url}', send_email=req.send_email, send_sms=req.send_sms, dry=False, key=f"avs-{link['id']}")
    return {'ok': True, 'url': url, 'share_id': link['id'], 'expires_iso': link['expires_iso'], 'replaced': revoked, **sent}

def api_desk_note_avs_revoke(req: runtime.AvsRevokeRequest):
    runtime._require_staff()
    import app.billing.billing as billing
    import app.notes.signed_notes as signed_notes
    if not signed_notes.get(req.note_id.strip()):
        raise runtime.HTTPException(404, 'signed AVS not found')
    return {'ok': True, 'revoked': billing.revoke_shares(req.note_id.strip(), 'avs')}

def public_avs_share(token: str):
    """Branded, signed, expiring AVS link (30 days, no login; re-send or turn off from the desk)."""
    import app.billing.billing as billing
    try:
        row = billing.verify_share(token, kind='avs')
    except billing.ShareError as e:
        return runtime._share_page(e.message, e.status, title='After-visit summary')
    (code, data, _ctype) = runtime._notify_get('/drive/file?path=' + runtime.quote(str(row.get('path') or ''), safe='/'))
    if code != 200 or not data.startswith(b'%PDF'):
        return runtime._share_page('The file could not be loaded right now. Try again shortly.', 502, title='After-visit summary')
    billing.note_hit(row['id'])
    name = (row.get('name') or 'After Visit Summary.pdf').replace('"', '')
    return runtime.Response(content=data, media_type='application/pdf', headers={'Content-Disposition': f'inline; filename="{name}"', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

def public_intake_copies(token: str):
    """Client's copies of their signed intake documents (30-day, no-login, revocable) + privacy notice."""
    from html import escape
    import app.billing.billing as billing
    import app.notes.intake_copies as intake_copies
    from app.infra.practice import load as load_practice
    try:
        row = billing.verify_share(token, kind='docs')
    except billing.ShareError as e:
        return runtime._share_page(e.message, e.status, title='Your signed documents')
    docs = intake_copies.docs_for(str(row.get('client_id') or ''))
    billing.note_hit(row['id'])
    tz = runtime.ZoneInfo(runtime.load_config().get('timezone', runtime.timezone_name()))
    who = str(load_practice().get('display_name') or '')
    exp = runtime.datetime.fromtimestamp(int(row.get('expires_at') or 0), runtime.timezone.utc).astimezone(tz)
    items = ''.join((f"<li style='margin:.6rem 0;padding:.8rem 1rem;border:1px solid #e5e7eb;border-radius:12px'><a style='color:#111;font-weight:600' href='/docs/{escape(token)}/{escape(d['id'])}'>{escape(d['label'])}</a><div style='color:#6b7280;font-size:.85rem'>Signed {escape(runtime.datetime.fromisoformat(d['received_at']).astimezone(tz).strftime('%b %-d, %Y · %-I:%M %p CT'))} · PDF</div></li>" for d in docs)) or '<li>No signed documents yet.</li>'
    body = f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'><title>Your signed documents</title><body style='font-family:Inter,system-ui,sans-serif;max-width:34rem;margin:8vh auto;padding:0 1rem;color:#111'><img src='/api/practice/logo' alt='' style='height:56px'><p style='color:#2f57a5;margin:.4rem 0'>{escape(who)}</p><h1 style='font-size:1.3rem;margin:.6rem 0'>Your signed documents</h1><p style='color:#374151'>Here are copies of the forms you signed, for your records. Tap one to open or save it.</p><ul style='list-style:none;padding:0'>{items}</ul><h2 style='font-size:1rem;margin-top:1.6rem'>Notice of Privacy Practices</h2><p style='color:#374151'>How we use and protect your health information, and your rights. <a href='{escape(intake_copies.NPP_URL)}' style='color:#2f57a5'>Open the notice (PDF)</a>. You can ask us for a paper copy anytime.</p><p style='color:#6b7280;font-size:.85rem;margin-top:1.6rem'>This private link works until {escape(exp.strftime('%b %-d, %Y'))}. Questions? Reply to the email or call the practice.</p></body>"
    return runtime.HTMLResponse(body, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

def public_intake_copy_file(token: str, sub_id: str):
    import app.billing.billing as billing
    import app.notes.intake_copies as intake_copies
    try:
        row = billing.verify_share(token, kind='docs')
    except billing.ShareError as e:
        return runtime._share_page(e.message, e.status, title='Your signed documents')
    d = intake_copies.doc(str(row.get('client_id') or ''), sub_id)
    if not d:
        return runtime._share_page('That document was not found.', 404, title='Your signed documents')
    (code, data, _ctype) = runtime._notify_get('/drive/file?path=' + runtime.quote(str(d['path']), safe='/'))
    if code != 200 or not data.startswith(b'%PDF'):
        return runtime._share_page('The file could not be loaded right now. Try again shortly.', 502, title='Your signed documents')
    name = (d['path'].rsplit('/', 1)[-1] or 'document.pdf').replace('"', '')
    return runtime.Response(content=data, media_type='application/pdf', headers={'Content-Disposition': f'inline; filename="{name}"', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

def api_desk_note_templates(id: str=runtime.Query(default='')):
    runtime._require_staff()
    from app.notes.note_templates import get_template, list_templates
    if id:
        spec = get_template(id)
        if not spec:
            raise runtime.HTTPException(404, 'unknown template')
        return {'ok': True, **spec}
    return {'ok': True, 'templates': list_templates()}

def api_desk_note_template_save(req: runtime.DeskNoteTemplateSave):
    runtime._require_staff()
    from app.notes.note_templates import save_template
    try:
        return {'ok': True, **save_template(req.id, req.body, title=req.title, sections=req.sections or None)}
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e

def api_desk_note_template_order(req: runtime.DeskNoteTemplateOrder):
    runtime._require_staff()
    from app.notes.note_templates import save_order
    return {'ok': True, 'ids': save_order(req.ids)}

def api_desk_note_template_delete(id: str=runtime.Query(default='')):
    runtime._require_staff()
    from app.notes.note_templates import delete_template
    try:
        delete_template(id)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    return {'ok': True}

def api_desk_phrases(id: str=runtime.Query(default='')):
    runtime._require_staff()
    from app.notes.dot_phrases import get_phrase, list_phrases
    if id:
        spec = get_phrase(id)
        if not spec:
            raise runtime.HTTPException(404, 'unknown phrase')
        return {'ok': True, **spec}
    return {'ok': True, 'phrases': list_phrases()}

def api_desk_phrase_save(req: runtime.DeskPhraseSave):
    runtime._require_staff()
    from app.notes.dot_phrases import save_phrase
    try:
        return {'ok': True, **save_phrase(req.trigger or req.id, req.body, title=req.title)}
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e

def api_desk_phrase_order(req: runtime.DeskPhraseOrder):
    runtime._require_staff()
    from app.notes.dot_phrases import save_order
    return {'ok': True, 'ids': save_order(req.ids)}

def api_desk_phrase_delete(id: str=runtime.Query(default='')):
    runtime._require_staff()
    from app.notes.dot_phrases import delete_phrase
    try:
        delete_phrase(id)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    return {'ok': True}

def api_desk_note_draft(req: runtime.DeskNoteDraftRequest):
    """SOAP + AVS fields from a scribe note or a transcript. Body never logged. No xAI.

    A scribe-aws note (fixed SOAP + After Visit Summary format) is parsed directly, no model call.
    A raw transcript goes to the scribe-aws note writer (Bedrock Nova Pro, under the AWS BAA)."""
    runtime._require_staff()
    raw = (req.transcript or '').strip()
    if len(raw) < 20:
        raise runtime.HTTPException(400, 'need a bit more transcript')
    from app.notes.note_parse import is_structured, parse_structured
    if is_structured(raw):
        return {'ok': True, 'fields': parse_structured(raw), 'engine': 'scribe-structured'}
    minutes = 0
    try:
        minutes = int(str(req.duration or '0').strip() or 0)
    except ValueError:
        minutes = 0
    body = runtime.json.dumps({'transcript': raw[:400000], 'duration_minutes': minutes}).encode()
    http_req = runtime.UrlRequest(runtime.SCRIBE_NOTE_URL, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with runtime.urlopen(http_req, timeout=150) as resp:
            data = runtime.json.loads(resp.read().decode() or '{}')
    except (runtime.HTTPError, runtime.URLError, TimeoutError, ValueError) as e:
        raise runtime.HTTPException(502, 'note writer unavailable') from e
    note = str(data.get('note') or '')
    if data.get('note_engine') == 'raw-transcript' or not is_structured(note):
        raise runtime.HTTPException(502, 'note writer (Bedrock) unavailable; nothing was sent elsewhere')
    return {'ok': True, 'fields': parse_structured(note), 'engine': str(data.get('note_engine') or '')}

def api_desk_note_scribe_ingest(req: runtime.DeskScribeIngestRequest, request: runtime.Request):
    """Edge-scribe finish → pending blank draft. Body never logged."""
    runtime._scribe_ingest_authorized(request)
    import app.notes.scribe_drafts as scribe_drafts
    try:
        row = scribe_drafts.put(note=req.note, transcript=req.transcript, title=req.title, template=(req.template or 'avs').strip() or 'avs', client_id=req.client_id, email=req.email, name=req.name, duration_minutes=req.duration_minutes, room=req.room, ended_at=req.ended_at, started_at=req.started_at)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e

    def _link() -> None:
        try:
            import app.notes.scribe_drafts as scribe_drafts
            r = next((x for x in scribe_drafts.rows_for_link(False) if x['id'] == row['id']), None)
            if r:
                runtime.link_scribe_row(r)
        except Exception as e:
            print('scribe_link_fail', type(e).__name__, flush=True)
    runtime.threading.Thread(target=_link, name='scribe-link', daemon=True).start()
    return {'ok': True, 'id': row['id']}

def api_desk_note_scribe_pending(request: runtime.Request):
    runtime._require_staff(request=request)
    import app.notes.scribe_drafts as scribe_drafts
    return {'ok': True, 'drafts': scribe_drafts.list_pending()}

def api_desk_note_scribe_pending_one(draft_id: str, request: runtime.Request):
    runtime._require_staff(request=request)
    import app.notes.scribe_drafts as scribe_drafts
    row = scribe_drafts.get(draft_id)
    if not row:
        raise runtime.HTTPException(404, 'draft not found')
    return {'ok': True, 'draft': row}

def api_desk_note_scribe_pending_ack(draft_id: str, request: runtime.Request):
    runtime._require_staff(request=request)
    import app.notes.scribe_drafts as scribe_drafts
    ok = scribe_drafts.ack(draft_id)
    if not ok:
        raise runtime.HTTPException(404, 'draft not found or already claimed')
    return {'ok': True}

def api_cipher_seal_params():
    """Public seal parameters. Public JWK only — the private half never leaves cipher_seal."""
    return runtime.cipher_seal.seal_params(runtime.cipher_mode.enabled())

async def api_cipher_intake(request: runtime.Request):
    """Accept a browser-sealed envelope. Server stores ciphertext only."""
    if not runtime.cipher_mode.enabled():
        raise runtime.HTTPException(404, 'cipher mode off')
    try:
        env = await request.json()
    except Exception:
        raise runtime.HTTPException(400, 'invalid json')
    try:
        runtime.cipher_seal.validate_envelope(env)
    except ValueError as e:
        raise runtime.HTTPException(400, f'bad envelope: {e}')
    row = runtime.cipher_queue.put(env, source='booker')
    return {'ok': True, 'id': row['id'], 'content_hash': row['content_hash']}

def api_desk_cipher_status():
    runtime._require_staff()
    return {'flags': runtime.cipher_mode.flags(), 'pending': runtime.cipher_queue.pending_count()}

def api_desk_cipher_pending():
    runtime._require_staff()
    return {'pending': runtime.cipher_queue.list_pending()}

def api_desk_cipher_drain(req: runtime.CipherDrainRequest):
    """Staff unseal: server opens the envelope; plaintext goes to the desk vault."""
    runtime._require_staff()
    env_id = (req.id or '').strip()
    if not env_id:
        raise runtime.HTTPException(400, 'id required')
    env = runtime.cipher_queue.get_envelope(env_id)
    if not env:
        raise runtime.HTTPException(404, 'envelope not found')
    try:
        plaintext = runtime.cipher_seal.open_envelope(env)
    except ValueError as e:
        raise runtime.HTTPException(400, f'unseal failed: {e}')
    runtime.cipher_queue.mark_drained(env_id)
    return {'ok': True, 'id': env_id, 'plaintext': plaintext}

router.add_api_route('/api/desk/note/sign', api_desk_note_sign, methods=['POST'])
router.add_api_route('/api/desk/note/last-avs', api_desk_note_last_avs, methods=['GET'])
router.add_api_route('/api/desk/note/drive-prior', api_desk_note_drive_prior, methods=['GET'])
router.add_api_route('/api/desk/note/carry', api_desk_note_carry, methods=['GET'])
router.add_api_route('/api/desk/note/update-from-transcript', api_desk_note_update_from_transcript, methods=['POST'])
router.add_api_route('/api/desk/note/preview', api_desk_note_preview, methods=['POST'])
router.add_api_route('/api/desk/note/avs/share', api_desk_note_avs_share, methods=['POST'])
router.add_api_route('/api/desk/note/avs/share/revoke', api_desk_note_avs_revoke, methods=['POST'])
router.add_api_route('/avs/{token}', public_avs_share, methods=['GET'])
router.add_api_route('/docs/{token}', public_intake_copies, methods=['GET'])
router.add_api_route('/docs/{token}/{sub_id}', public_intake_copy_file, methods=['GET'])
router.add_api_route('/api/desk/note/templates', api_desk_note_templates, methods=['GET'])
router.add_api_route('/api/desk/note/templates', api_desk_note_template_save, methods=['POST'])
router.add_api_route('/api/desk/note/templates/order', api_desk_note_template_order, methods=['POST'])
router.add_api_route('/api/desk/note/templates', api_desk_note_template_delete, methods=['DELETE'])
router.add_api_route('/api/desk/phrases', api_desk_phrases, methods=['GET'])
router.add_api_route('/api/desk/phrases', api_desk_phrase_save, methods=['POST'])
router.add_api_route('/api/desk/phrases/order', api_desk_phrase_order, methods=['POST'])
router.add_api_route('/api/desk/phrases', api_desk_phrase_delete, methods=['DELETE'])
router.add_api_route('/api/desk/note/draft', api_desk_note_draft, methods=['POST'])
router.add_api_route('/api/desk/note/scribe-ingest', api_desk_note_scribe_ingest, methods=['POST'])
router.add_api_route('/api/desk/note/scribe-pending', api_desk_note_scribe_pending, methods=['GET'])
router.add_api_route('/api/desk/note/scribe-pending/{draft_id}', api_desk_note_scribe_pending_one, methods=['GET'])
router.add_api_route('/api/desk/note/scribe-pending/{draft_id}/ack', api_desk_note_scribe_pending_ack, methods=['POST'])
router.add_api_route('/api/cipher/seal-params', api_cipher_seal_params, methods=['GET'])
router.add_api_route('/api/cipher/intake', api_cipher_intake, methods=['POST'])
router.add_api_route('/api/desk/cipher/status', api_desk_cipher_status, methods=['GET'])
router.add_api_route('/api/desk/cipher/pending', api_desk_cipher_pending, methods=['GET'])
router.add_api_route('/api/desk/cipher/drain', api_desk_cipher_drain, methods=['POST'])
