"""Billing helpers. Routes call these as runtime.<name>.
Drive and send helpers are read from app.runtime so a test can replace them.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

import app.runtime as runtime
from app.messaging.email_templates import HOST_SHORT
from app.messaging.notify import send_named_email
from app.http.shapes import DeskFeeRequest
from app.portal.patients import client_by_id

def _fee_cents(req: DeskFeeRequest) -> int:
    from app.billing.fees import parse_amount
    if req.amount_cents is not None:
        return int(req.amount_cents)
    return parse_amount(req.amount)

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
            billing = resolve_client_paths(view, runtime._drive_list).get('billing') or ''
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
    (code, saved) = runtime._notify_post('/drive/save', {'path': dest, 'b64': base64.b64encode(data).decode('ascii')})
    if code >= 400 or not saved.get('ok'):
        raise HTTPException(502, 'drive save failed')

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
    url = f"{runtime.PUBLIC_BASE}/sb/{link['token']}"
    inv = rec.get('invoice') or {}
    first = (str(view.get('name') or 'there').split() or ['there'])[0]
    sent = runtime._bill_send(kind='superbill', view=view, fields={'name': view.get('name') or '', 'title': inv.get('label') or 'Visit', 'amount': inv.get('amount_pretty') or '', 'url': url}, sms_body=f'Hi {first}, your superbill is ready (link good for 30 days): {url} — {HOST_SHORT}', send_email=send_email, send_sms=send_sms, dry=dry, key=f"sb-{link['id']}")
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
        folder = runtime._bill_billing_folder(view)
        if not folder:
            msg = 'Superbill not saved: no billing folder for this client.'
            billing.update_billing(rec['id'], superbill_status='failed', superbill_detail=msg)
            return {'ok': False, 'status': 'failed', 'detail': msg}
        runtime._drive_mkdir(folder)
        path = folder.rstrip('/') + '/' + fname
        runtime._bill_drive_save(path, pdf)
        source = 'drive'
    billing.update_billing(rec['id'], superbill_status='saved', superbill_source=source, superbill_path=path, superbill_detail='specimen (dry run)' if missing else '', statement=statement)
    return {'ok': True, 'status': 'saved', 'source': source, 'path': path, 'name': fname, 'statement': statement, 'bytes': len(pdf), 'specimen': bool(missing)}
