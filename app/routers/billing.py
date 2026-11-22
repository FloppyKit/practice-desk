from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def api_public_pay(req: runtime.PublicTokenRequest):
    """Stripe Checkout for the open balance. Saves the card for later."""
    row = runtime._verified_client(req.verify_token)
    import app.infra.clients_db as clients_db
    from app.billing.fees import attach_invoice, dollars, list_open, owed_cents
    from app.billing.stripe_pay import create_balance_checkout, ensure_customer, open_checkout_url, stripe_ready, void_invoice
    if not stripe_ready():
        raise runtime.HTTPException(503, 'card payments are not on')
    cid = str(row.get('id') or '')
    email = str(row.get('email') or '').strip()
    if not email:
        raise runtime.HTTPException(400, 'no email on file for a card')
    open_fees = list_open(cid)
    cents = owed_cents(cid)
    if cents <= 0 or not open_fees:
        return {'ok': True, 'url': '', 'owed_cents': 0, 'owed_pretty': dollars(0)}
    for fee in open_fees:
        sid = str(fee.get('stripe_id') or '')
        url = str(fee.get('pay_url') or '')
        if sid.startswith('cs_') and url.startswith('https://'):
            live = open_checkout_url(sid)
            if live:
                return {'ok': True, 'url': live, 'owed_cents': cents, 'owed_pretty': dollars(cents), 'reused': True}
    for fee in open_fees:
        sid = str(fee.get('stripe_id') or '')
        if sid.startswith('in_'):
            void_invoice(sid)
    cust = ensure_customer(email=email, existing_id=str(row.get('stripe_customer_id') or ''), client_id=cid)
    if cust and cust != row.get('stripe_customer_id'):
        clients_db.upsert({'id': cid, 'stripe_customer_id': cust})
    fee_ids = [str(fee.get('id') or '') for fee in open_fees if fee.get('id')]
    sess = create_balance_checkout(customer_id=cust, amount_cents=cents, client_id=cid, fee_ids=fee_ids, success_url=runtime.PUBLIC_BASE + '/pay/thanks?session_id={CHECKOUT_SESSION_ID}', cancel_url=runtime.SITE_PUBLIC + '/portal')
    attach_invoice(fee_ids, stripe_id=str(sess['id']), pay_url=str(sess['url']))
    return {'ok': True, 'url': sess['url'], 'owed_cents': cents, 'owed_pretty': dollars(cents)}

def pay_thanks(session_id: str=''):
    from app.billing.stripe_pay import finalize_checkout
    title = 'Payment not completed'
    note = 'If you closed the payment page, nothing was charged.'
    if session_id.startswith('cs_'):
        try:
            done = finalize_checkout(session_id)
        except Exception:
            return runtime.HTMLResponse('<!doctype html><title>Payment could not be confirmed</title><p>The payment status could not be read. Nothing here was marked paid.</p>', status_code=502)
        if done.get('ok') and (not done.get('pending')):
            title = 'Payment received'
            note = 'This card is saved for later visits.'
        elif done.get('pending'):
            title = 'Payment still processing'
            note = 'If the bank finishes it, the balance will clear on its own.'
    page = f'<!doctype html>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>{title}</title>\n<body style="margin:0;font-family:Georgia,serif;background:#f6f3ee;color:#1a1a1a;display:flex;min-height:100vh;align-items:center;justify-content:center">\n<main style="max-width:28rem;padding:2rem;text-align:center">\n<h1 style="font-size:1.5rem;font-weight:500">{title}</h1>\n<p style="line-height:1.5">{note}</p>\n<p><a href="{runtime.SITE_PUBLIC}" style="color:#1a1a1a">Back to the practice site</a></p>\n</main>\n'
    return runtime.HTMLResponse(page)

def api_desk_fees(client_id: str=runtime.Query(default='')):
    runtime._require_staff_or_host()
    from app.billing.fees import client_books
    if not client_id:
        raise runtime.HTTPException(400, 'need a client')
    return {'ok': True, **client_books(client_id)}

def api_desk_fees_create(req: runtime.DeskFeeRequest):
    """Add an owed line, optionally charge the card on file."""
    runtime._require_staff()
    import app.infra.clients_db as clients_db
    from app.billing.fees import create as fee_create, get as fee_get, set_status
    from app.billing.stripe_pay import charge_off_session, stripe_ready
    cid = (req.client_id or '').strip()
    if not cid:
        raise runtime.HTTPException(400, 'need a client')
    row = clients_db.get_by_id(cid)
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    try:
        cents = runtime._fee_cents(req)
    except ValueError as e:
        raise runtime.HTTPException(400, str(e)) from e
    dos = (req.dos or '')[:10]
    if len(dos) != 10:
        raise runtime.HTTPException(400, 'need a date of service')
    fee = None
    if req.fee_id:
        fee = fee_get(req.fee_id)
        if not fee or fee.get('client_id') != cid:
            raise runtime.HTTPException(404, 'fee not found')
        if fee.get('status') == 'paid':
            return {'ok': True, 'fee': fee, 'charged': False, 'detail': 'already paid'}
    else:
        fee = fee_create(client_id=cid, dos=dos, amount_cents=cents, kind=req.kind or 'visit', note=req.note or '', event_id=req.event_id or '')
    charged = False
    detail = ''
    if req.charge:
        if not stripe_ready():
            raise runtime.HTTPException(503, 'Stripe is not on the booker')
        cust = str(row.get('stripe_customer_id') or '')
        pm = str(row.get('stripe_payment_method_id') or '')
        if not (cust and pm):
            return {'ok': True, 'fee': fee, 'charged': False, 'detail': 'On the books. No card on file — send the pay link.'}
        try:
            result = charge_off_session(customer_id=cust, payment_method_id=pm, amount_cents=int(fee['amount_cents']), dos=str(fee.get('dos') or dos), fee_id=str(fee['id']))
        except RuntimeError as e:
            fee = set_status(str(fee['id']), status='failed')
            raise runtime.HTTPException(402, str(e)[:200]) from e
        fee = set_status(str(fee['id']), status='paid' if result.get('ok') else 'failed', stripe_id=str(result.get('stripe_id') or ''))
        charged = bool(result.get('ok'))
        detail = '' if charged else str(result.get('error') or 'charge failed')
        if not charged:
            raise runtime.HTTPException(402, detail)
    return {'ok': True, 'fee': fee, 'charged': charged, 'detail': detail}

def api_desk_fees_waive(req: runtime.DeskFeeRequest):
    runtime._require_staff()
    from app.billing.fees import get as fee_get, set_status
    fee = fee_get(req.fee_id)
    if not fee:
        raise runtime.HTTPException(404, 'fee not found')
    if fee.get('status') == 'paid':
        raise runtime.HTTPException(400, 'already paid')
    return {'ok': True, 'fee': set_status(str(fee['id']), status='waived')}

def api_desk_fees_no_show(req: runtime.DeskFeeRequest):
    """Mark a past visit as no-show: ledger row at the posted fee. Charge/invoice separately."""
    runtime._require_staff()
    import app.infra.clients_db as clients_db
    from app.billing.fees import create as fee_create, find_no_show, suggest_amount_cents
    from app.messaging.notify import send_named_email
    from app.infra.practice import load as load_practice
    cid = (req.client_id or '').strip()
    if not cid:
        raise runtime.HTTPException(400, 'need a client')
    row = clients_db.get_by_id(cid)
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    if req.event_id:
        existing = find_no_show(req.event_id)
        if existing:
            return {'ok': True, 'fee': existing, 'created': False, 'detail': 'already on the books'}
    dos = (req.dos or '')[:10]
    if len(dos) != 10:
        raise runtime.HTTPException(400, 'need the visit date')
    role = str((load_practice() or {}).get('billing_role') or 'psychiatrist')
    try:
        cents = runtime._fee_cents(req) if req.amount or req.amount_cents is not None else suggest_amount_cents(req.duration_minutes, role)
    except ValueError:
        cents = suggest_amount_cents(req.duration_minutes, role)
    if cents <= 0:
        raise runtime.HTTPException(400, 'no posted fee for this visit length')
    fee = fee_create(client_id=cid, dos=dos, amount_cents=cents, kind='no_show', note='no-show', event_id=req.event_id or '')
    mailed = False
    if req.notify and row.get('email'):
        mail = send_named_email(kind='no_show', to=str(row.get('email') or ''), fields={'name': row.get('display_name') or row.get('email') or '', 'email': row.get('email') or '', 'title': 'Visit', 'start_iso': dos, 'duration_minutes': req.duration_minutes or 30, 'rebook_url': runtime._book_link(str(row['id']))['url']})
        mailed = bool(mail.get('ok'))
    return {'ok': True, 'fee': fee, 'created': True, 'mailed': mailed}

def api_desk_fees_invoice(req: runtime.DeskFeeRequest):
    """One Stripe invoice for every owed line. Superbill stays per-DOS."""
    runtime._require_staff()
    import app.infra.clients_db as clients_db
    from app.billing.fees import attach_invoice, dollars, list_open
    from app.messaging.notify import send_named_email
    from app.billing.stripe_pay import create_invoice, ensure_customer, stripe_ready, void_invoice
    cid = (req.client_id or '').strip()
    if not cid:
        raise runtime.HTTPException(400, 'need a client')
    row = clients_db.get_by_id(cid)
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    if not stripe_ready():
        raise runtime.HTTPException(503, 'Stripe is not on the booker')
    open_fees = list_open(cid)
    if not open_fees:
        raise runtime.HTTPException(400, 'nothing owed')
    existing = {str(f.get('stripe_id') or '') for f in open_fees}
    existing.discard('')
    urls = {str(f.get('pay_url') or '') for f in open_fees}
    urls.discard('')
    if len(existing) == 1 and len(urls) == 1 and next(iter(existing)).startswith('in_'):
        url = next(iter(urls))
        return {'ok': True, 'pay_url': url, 'stripe_id': next(iter(existing)), 'count': len(open_fees), 'amount_pretty': dollars(sum((int(f['amount_cents']) for f in open_fees))), 'reused': True}
    for sid in existing:
        if sid.startswith('in_'):
            void_invoice(sid)
    try:
        cust = ensure_customer(email=str(row.get('email') or ''), existing_id=str(row.get('stripe_customer_id') or ''), client_id=cid)
    except RuntimeError as e:
        raise runtime.HTTPException(400, str(e)[:200]) from e
    if cust and cust != row.get('stripe_customer_id'):
        clients_db.upsert({'id': cid, 'stripe_customer_id': cust})
    try:
        inv = create_invoice(customer_id=cust, lines=open_fees, client_id=cid)
    except RuntimeError as e:
        raise runtime.HTTPException(502, str(e)[:200]) from e
    attach_invoice([str(f['id']) for f in open_fees], stripe_id=str(inv['stripe_id']), pay_url=str(inv.get('pay_url') or ''))
    url = str(inv.get('pay_url') or '')
    if row.get('email') and url:
        send_named_email(kind='payment_due', to=str(row.get('email') or ''), fields={'name': row.get('display_name') or row.get('email') or '', 'email': row.get('email') or '', 'amount': dollars(int(inv.get('amount_cents') or 0)), 'title': f'{len(open_fees)} visit' + ('s' if len(open_fees) != 1 else ''), 'url': url})
    return {'ok': True, 'pay_url': url, 'stripe_id': inv.get('stripe_id'), 'count': len(open_fees), 'amount_pretty': dollars(int(inv.get('amount_cents') or 0)), 'reused': False}

async def api_stripe_webhook(request: runtime.Request):
    """Mark ledger paid when a Stripe invoice is paid. No staff secret."""
    from app.billing.fees import mark_paid_by_stripe
    from app.billing.stripe_pay import verify_webhook
    raw = await request.body()
    sig = request.headers.get('Stripe-Signature') or ''
    try:
        event = verify_webhook(raw, sig)
    except RuntimeError as e:
        raise runtime.HTTPException(400, str(e)[:160]) from e
    et = str(event.get('type') or '')
    obj = (event.get('data') or {}).get('object') or {}
    if et in ('invoice.paid', 'invoice.payment_succeeded'):
        iid = str(obj.get('id') or '')
        n = mark_paid_by_stripe(iid)
        return {'ok': True, 'marked': n, 'type': et}
    if et == 'checkout.session.completed':
        from app.billing.stripe_pay import finalize_checkout
        sid = str(obj.get('id') or '')
        try:
            done = finalize_checkout(sid)
        except RuntimeError as e:
            raise runtime.HTTPException(502, str(e)[:160]) from e
        return {'ok': True, 'type': et, **done}
    return {'ok': True, 'ignored': et}

def api_desk_superbill_visits(client_id: str=runtime.Query(default=''), start: str=runtime.Query(default=''), end: str=runtime.Query(default='')):
    """Superbill v2 picker: past visits, imported visits, and standalone fees, split unlinked / linked."""
    import app.billing.superbills as superbills
    runtime._require_staff()
    row = runtime.client_by_id(client_id) if client_id else None
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    tz = runtime.ZoneInfo(runtime.load_config().get('timezone', runtime.timezone_name()))
    try:
        cal = runtime._visits_for_client(row, tz)
    except runtime.HTTPException:
        raise
    except Exception as e:
        raise runtime.HTTPException(502, 'Could not read the calendar') from e
    start = start[:10] if runtime.re.match('^\\d{4}-\\d{2}-\\d{2}', start or '') else ''
    end = end[:10] if runtime.re.match('^\\d{4}-\\d{2}-\\d{2}', end or '') else ''
    out = superbills.build_list(str(row.get('id') or client_id), cal, start=start, end=end)
    return {'ok': True, **out}

def api_desk_superbill(req: runtime.DeskSuperbillRequest):
    """Write a Superbill PDF to Billing. Body never logged."""
    import base64
    from app.infra.drive_paths import BILLING_CHILD, LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths
    from app.infra.practice import asset_bytes, format_licenses, load as load_practice, next_statement
    from app.billing.superbill_pdf import superbill_pdf
    runtime._require_staff()
    from app.billing.billing import letterhead_missing, refusal_message
    missing = letterhead_missing(load_practice())
    if missing:
        raise runtime.HTTPException(409, refusal_message(missing))
    row = runtime.client_by_id(req.client_id) if req.client_id else None
    if not row and req.email:
        row = runtime.client_by_email(req.email)
    if not row:
        raise runtime.HTTPException(404, 'client not found')
    stored = paths_from_row(row)
    billing = stored.get('billing') or ''
    if not billing:
        try:
            billing = resolve_client_paths(row, runtime._drive_list).get('billing') or ''
        except runtime.HTTPException:
            raise
        except Exception as e:
            raise runtime.HTTPException(502, 'Could not read the billing folder') from e
    if not billing:
        dest = person_folder_name(row)
        if dest:
            billing = f'{LIVING_ROOT}/{dest}/{BILLING_CHILD}'
    if not billing and (not req.preview):
        raise runtime.HTTPException(400, 'no billing folder for this client')
    if billing and (not req.preview):
        runtime._drive_mkdir(billing)
    dos = (req.dos or '').strip()[:10]
    if len(dos) != 10:
        dos = runtime.datetime.now().date().isoformat()
    prac = load_practice()
    statement = 'PREVIEW' if req.preview else next_statement()
    diagnoses = []
    for item in (req.diagnoses or [])[:12]:
        if not isinstance(item, dict):
            continue
        code = str(item.get('code') or '').strip()[:16]
        label = str(item.get('label') or '').strip()[:160]
        if code or label:
            diagnoses.append({'code': code, 'label': label})
    lines = []
    dos_by_key: dict[str, str] = {}
    for item in (req.lines or [])[:80]:
        if not isinstance(item, dict):
            continue
        ik = str(item.get('item') or '')
        if ik and ik not in dos_by_key:
            dos_by_key[ik] = str(item.get('dos') or dos)[:10]
        lines.append({'dos': str(item.get('dos') or dos)[:10], 'pos': str(item.get('pos') or req.pos or '10')[:8], 'service': str(item.get('service') or '')[:80], 'code': str(item.get('code') or '')[:16], 'modifier': (str(item.get('modifier') or '') if ik.startswith('fee:') else str(item.get('modifier') or '95'))[:8], 'dx': str(item.get('dx') or '')[:20], 'units': str(item.get('units') or '1')[:6], 'cost': str(item.get('cost') or '')[:16], 'paid': str(item.get('paid') or '')[:16]})
    if not lines:
        raise runtime.HTTPException(400, 'add at least one service line')
    import app.billing.superbills as sb2
    from app.billing.superbill_pdf import superbill_v2_filename
    all_dos = [ln['dos'] for ln in lines if len(ln['dos']) == 10]
    if all_dos:
        dos = min(all_dos)
    custom_fields = []
    for f in (req.custom_fields or [])[:12]:
        if isinstance(f, dict):
            lab = str(f.get('label') or '').strip()[:60]
            val = str(f.get('value') or '').strip()[:300]
            if lab or val:
                custom_fields.append({'label': lab, 'value': val})
    keys = [k for k in dict.fromkeys(req.items or []) if sb2.valid_key(k)][:80]
    fname = superbill_v2_filename(statement, all_dos or [dos])
    dest = billing.rstrip('/') + '/' + fname
    pdf = superbill_pdf({'statement': statement, 'issued': runtime.datetime.now().date().isoformat(), 'dos': dos, 'practice': {'name': prac.get('display_name') or '', 'address': prac.get('address') or '', 'ein': prac.get('ein') or '', 'npi': prac.get('npi') or ''}, 'provider': {'name': prac.get('clinician_name') or '', 'email': prac.get('email') or '', 'phone': prac.get('phone') or '', 'npi': prac.get('npi') or ''}, 'client': {'name': row.get('name') or '', 'email': row.get('email') or '', 'phone': row.get('phone') or '', 'address': (req.address or '').strip()[:200], 'dob': row.get('dob') or ''}, 'licenses': format_licenses(prac.get('licenses') or []), 'letterhead': {'name': prac.get('legal_name') or prac.get('display_name') or '', 'address': prac.get('address') or '', 'phone': prac.get('phone') or '', 'email': prac.get('email') or '', 'ein': prac.get('ein') or '', 'npi': prac.get('npi') or '', 'licenses': format_licenses(prac.get('licenses') or [])}, 'logo': asset_bytes('logo'), 'diagnoses': diagnoses, 'lines': lines, 'custom_fields': custom_fields})
    if req.preview:
        return runtime.Response(content=pdf, media_type='application/pdf', headers={'Content-Disposition': 'inline; filename=superbill-preview.pdf', 'Cache-Control': 'no-store'})
    (code, saved) = runtime._notify_post('/drive/save', {'path': dest, 'b64': base64.b64encode(pdf).decode('ascii')})
    if code >= 400:
        raise runtime.HTTPException(code, str((saved or {}).get('detail') or 'drive save failed')[:200])
    if not (saved or {}).get('ok'):
        raise runtime.HTTPException(502, 'drive save failed')
    sid = sb2.record(str(row.get('id') or ''), statement=statement, name=fname, path=dest, keys=keys, dos_by_key=dos_by_key, all_dos=all_dos)
    return {'ok': True, 'path': dest, 'name': fname, 'statement': statement, 'bytes': len(pdf), 'superbill_id': sid, 'linked': len(keys)}

def api_desk_superbill_send(req: runtime.SuperbillSendRequest):
    """Email and text a saved superbill link. Same send as the after-visit summary."""
    runtime._require_staff()
    import app.billing.billing as billing
    import app.billing.superbills as sb2
    from app.billing.superbill_pdf import pretty_date
    rec = sb2.get(req.superbill_id.strip())
    if not rec or not rec.get('path'):
        raise runtime.HTTPException(404, 'saved superbill not found')
    view = dict(runtime.client_by_id(rec['client_id']) or {})
    if not view:
        raise runtime.HTTPException(404, 'client not found')
    revoked = billing.revoke_shares(rec['id'])
    link = billing.mint_share(billing_id=rec['id'], client_id=rec['client_id'], source='superbill', path=rec['path'], name=rec.get('name') or 'Superbill.pdf', kind='sb')
    url = f"{runtime.PUBLIC_BASE}/sb/{link['token']}"
    first = (str(view.get('name') or 'there').split() or ['there'])[0]
    dos = rec.get('first_dos') or ''
    sent = runtime._bill_send(kind='superbill', view=view, fields={'name': view.get('name') or '', 'title': 'Superbill', 'url': url, 'dos_label': pretty_date(dos) if dos else ''}, sms_body=f'Hi {first}, your superbill is ready (link good for 30 days): {url} — {runtime.HOST_SHORT}', send_email=req.send_email, send_sms=req.send_sms, dry=False, key=f"sb-{link['id']}")
    return {'ok': True, 'url': url, 'share_id': link['id'], 'expires_iso': link['expires_iso'], 'replaced': revoked, **sent}

def api_desk_bill_preview(client_id: str=runtime.Query(default=''), event_id: str=runtime.Query(default=''), dos: str=runtime.Query(default=''), event_type: str=runtime.Query(default=''), duration_minutes: int | None=runtime.Query(default=None)):
    """Confirm-sheet data: guessed code, CPT lines, standing dx, card, letterhead gaps."""
    runtime._require_staff()
    import app.billing.billing as billing
    from app.infra.practice import load as load_practice
    (raw, view) = runtime._bill_client(client_id)
    codes = billing.load_codes()
    day = (dos or '')[:10] or runtime.datetime.now().date().isoformat()
    key = billing.event_key(event_id, raw['id'], day)
    existing = billing.find_billing(key, dry_run=False) or billing.find_billing(key, dry_run=True)
    missing = billing.letterhead_missing(load_practice())
    return {'ok': True, 'client': {'id': raw['id'], 'name': view.get('name') or '', 'email': view.get('email') or '', 'has_phone': bool(view.get('phone')), 'dob': view.get('dob') or ''}, 'event_id': event_id, 'dos': day, 'guess': billing.guess_code(event_type, duration_minutes), 'pos': codes['pos'], 'codes': list(codes['visit_types'].values()), 'standing_dx': billing.standing_dx(raw), 'card': {'on_file': bool(raw.get('stripe_customer_id') and raw.get('stripe_payment_method_id')), 'brand': raw.get('stripe_card_brand') or '', 'last4': raw.get('stripe_card_last4') or ''}, 'live': billing.live_enabled(), 'sms': billing.sms_enabled(), 'letterhead_missing': missing, 'letterhead_message': billing.refusal_message(missing) if missing else '', 'existing': billing.public_billing(existing)}

def api_desk_bill_confirm(req: runtime.BillConfirmRequest):
    """Bill visit: invoice + payment (card or link) + superbill, after the host confirms."""
    runtime._require_staff()
    import app.billing.billing as billing
    from app.billing.fees import attach_invoice, create as fee_create, set_status
    from app.billing.stripe_pay import charge_off_session, create_invoice, ensure_customer, stripe_ready
    (raw, view) = runtime._bill_client(req.client_id)
    cid = raw['id']
    codes = billing.load_codes()
    spec = codes['visit_types'].get((req.visit_code or '').strip())
    if not spec:
        raise runtime.HTTPException(400, 'Pick a visit code.')
    if (req.confirm_code or '').strip() != spec['id']:
        raise runtime.HTTPException(400, 'Confirm the visit code first. Nothing was charged or sent.')
    dos = (req.dos or '').strip()[:10]
    try:
        runtime.datetime.strptime(dos, '%Y-%m-%d')
    except ValueError:
        raise runtime.HTTPException(400, 'need a date of service (YYYY-MM-DD)')
    payment = (req.payment or '').strip().lower()
    if payment not in billing.PAYMENTS:
        raise runtime.HTTPException(400, 'Pick charge card on file or send payment link.')
    card_ready = bool(raw.get('stripe_customer_id') and raw.get('stripe_payment_method_id'))
    if payment == 'card':
        if not req.confirm_charge:
            raise runtime.HTTPException(400, 'Tick the charge confirmation to charge the card on file.')
        if not card_ready:
            raise runtime.HTTPException(409, 'No card on file. Choose send payment link instead.')
    live = billing.live_enabled()
    dry = bool(req.dry_run) or not live
    if req.specimen and (not dry):
        raise runtime.HTTPException(400, 'specimen is dry-run only')
    if req.share_ttl_seconds is not None and (not dry):
        raise runtime.HTTPException(400, 'share_ttl_seconds is dry-run only')
    if not dry and (not stripe_ready()):
        raise runtime.HTTPException(503, 'Stripe is not on the booker')
    dx = billing.clean_dx(req.diagnoses)
    if req.save_dx and dx:
        billing.save_standing_dx(cid, dx)
    key = billing.event_key(req.event_id, cid, dos)
    invoice = billing.build_invoice(spec, codes['pos'], dos)
    rec = billing.claim_billing(client_id=cid, key=key, dos=dos, invoice=invoice, dx=dx, payment=payment, dry_run=dry)
    if not rec:
        existing = billing.find_billing(key, dry_run=dry)
        if existing and (not dry) and (payment == 'card') and (str(existing.get('payment_status') or '') == 'failed') and raw.get('stripe_customer_id') and raw.get('stripe_payment_method_id'):
            fee_id = str(existing.get('fee_id') or '')
            if not fee_id:
                fee = fee_create(client_id=cid, dos=dos, amount_cents=int(invoice['amount_cents']), kind='visit', note=invoice['invoice_code'], event_id=req.event_id or '')
                fee_id = str(fee['id'])
                existing = billing.update_billing(existing['id'], fee_id=fee_id) or existing
            try:
                result = charge_off_session(customer_id=str(raw['stripe_customer_id']), payment_method_id=str(raw['stripe_payment_method_id']), amount_cents=int(invoice['amount_cents']), dos=dos, fee_id=fee_id)
            except RuntimeError as e:
                result = {'ok': False, 'error': str(e)[:200], 'stripe_id': ''}
            set_status(fee_id, status='paid' if result.get('ok') else 'failed', stripe_id=str(result.get('stripe_id') or ''))
            charged = bool(result.get('ok'))
            err = '' if charged else str(result.get('error') or 'charge failed')
            existing = billing.update_billing(existing['id'], payment_status='paid' if charged else 'failed', stripe_id=str(result.get('stripe_id') or '')) or existing
            return {'ok': True, 'created': False, 'retried': True, 'detail': 'Card charged.' if charged else err, 'payment': {'charged': charged, 'detail': err}, 'billing': billing.public_billing(existing)}
        return {'ok': True, 'created': False, 'detail': 'This visit is already billed.', 'billing': billing.public_billing(existing)}
    pay: dict[str, runtime.Any] = {'mode': payment, 'dry_run': dry, 'charged': False}
    amount = int(invoice['amount_cents'])
    if dry:
        pay['detail'] = f"Dry run: would charge {invoice['amount_pretty']} to the card on file." if payment == 'card' else f"Dry run: would create a {invoice['amount_pretty']} Stripe invoice and send the pay link."
        rec = billing.update_billing(rec['id'], payment_status='dry_run')
    else:
        fee = fee_create(client_id=cid, dos=dos, amount_cents=amount, kind='visit', note=invoice['invoice_code'], event_id=req.event_id or '')
        rec = billing.update_billing(rec['id'], fee_id=str(fee['id']))
        if payment == 'card':
            try:
                result = charge_off_session(customer_id=str(raw['stripe_customer_id']), payment_method_id=str(raw['stripe_payment_method_id']), amount_cents=amount, dos=dos, fee_id=str(fee['id']))
            except RuntimeError as e:
                result = {'ok': False, 'error': str(e)[:200], 'stripe_id': ''}
            set_status(str(fee['id']), status='paid' if result.get('ok') else 'failed', stripe_id=str(result.get('stripe_id') or ''))
            pay['charged'] = bool(result.get('ok'))
            pay['detail'] = '' if pay['charged'] else str(result.get('error') or 'charge failed')
            rec = billing.update_billing(rec['id'], payment_status='paid' if pay['charged'] else 'failed', stripe_id=str(result.get('stripe_id') or ''))
        else:
            try:
                cust = ensure_customer(email=str(raw.get('email') or ''), existing_id=str(raw.get('stripe_customer_id') or ''), client_id=cid)
                if cust and cust != raw.get('stripe_customer_id'):
                    import app.infra.clients_db as clients_db
                    clients_db.upsert({'id': cid, 'stripe_customer_id': cust})
                inv = create_invoice(customer_id=cust, lines=[fee], client_id=cid)
            except RuntimeError as e:
                rec = billing.update_billing(rec['id'], payment_status='failed')
                raise runtime.HTTPException(502, 'Invoice not created: ' + str(e)[:160]) from e
            attach_invoice([str(fee['id'])], stripe_id=str(inv['stripe_id']), pay_url=str(inv.get('pay_url') or ''))
            link = billing.mint_share(billing_id=rec['id'], client_id=cid, source='pay', kind='pay', path=str(inv['stripe_id']), name=invoice['invoice_code'])
            pay['pay_url'] = f"{runtime.PUBLIC_BASE}/pay/{link['token']}"
            rec = billing.update_billing(rec['id'], payment_status='sent', stripe_id=str(inv['stripe_id']), pay_url=pay['pay_url'])
    if payment == 'link':
        first = (str(view.get('name') or 'there').split() or ['there'])[0]
        url = pay.get('pay_url') or f'{runtime.SITE_PUBLIC}/pay?' + runtime.urlencode({'email': view.get('email') or '', 'name': view.get('name') or ''})
        pay['notify'] = runtime._bill_send(kind='payment_due', view=view, fields={'name': view.get('name') or '', 'amount': invoice['amount_pretty'], 'title': invoice['label'], 'url': url}, sms_body=f"Hi {first}, your visit balance of {invoice['amount_pretty']} is ready: {url} — {runtime.HOST_SHORT}", send_email=req.send_email, send_sms=req.send_sms, dry=dry, key=f"pay-{rec['id']}")
    sb = runtime._bill_superbill(rec, raw, view, specimen=req.specimen)
    rec = billing.get_billing(rec['id']) or rec
    share: dict[str, runtime.Any] = {'skipped': True}
    if sb.get('ok') and req.share_superbill:
        share = runtime._bill_share(rec, view, send_email=req.send_email, send_sms=req.send_sms, ttl=req.share_ttl_seconds)
    return {'ok': True, 'created': True, 'dry_run': dry, 'forced_dry_run': dry and (not req.dry_run), 'invoice': invoice, 'payment': pay, 'superbill': sb, 'share': share, 'billing': billing.public_billing(billing.get_billing(rec['id']))}

def api_desk_bill_superbill(req: runtime.BillSuperbillRequest):
    """Build the superbill for an already-billed visit (e.g. once the letterhead is filled)."""
    runtime._require_staff()
    import app.billing.billing as billing
    rec = billing.get_billing(req.billing_id)
    if not rec:
        raise runtime.HTTPException(404, 'billing not found')
    if req.specimen and (not rec.get('dry_run')):
        raise runtime.HTTPException(400, 'specimen is dry-run only')
    if req.share_ttl_seconds is not None and (not rec.get('dry_run')):
        raise runtime.HTTPException(400, 'share_ttl_seconds is dry-run only')
    (raw, view) = runtime._bill_client(rec['client_id'])
    redo_specimen = bool(rec.get('dry_run')) and str(rec.get('superbill_detail') or '').startswith('specimen')
    if rec.get('superbill_status') == 'saved' and (not (redo_specimen and (not req.specimen))):
        return {'ok': True, 'created': False, 'detail': 'Superbill already saved.', 'billing': billing.public_billing(rec)}
    dx = billing.clean_dx(req.diagnoses) or rec.get('dx') or []
    if req.save_dx and dx:
        billing.save_standing_dx(raw['id'], dx)
    rec = billing.update_billing(rec['id'], dx_json=runtime.json.dumps(dx))
    sb = runtime._bill_superbill(rec, raw, view, specimen=req.specimen)
    share: dict[str, runtime.Any] = {'skipped': True}
    if sb.get('ok') and req.share_superbill:
        share = runtime._bill_share(billing.get_billing(rec['id']) or rec, view, send_email=req.send_email, send_sms=req.send_sms, ttl=req.share_ttl_seconds)
    return {'ok': True, 'created': bool(sb.get('ok')), 'superbill': sb, 'share': share, 'billing': billing.public_billing(billing.get_billing(rec['id']))}

def api_desk_bill_share(req: runtime.BillShareRequest):
    """Rotate: turn off the old branded link and mint a fresh 30-day one (optionally send it)."""
    runtime._require_staff()
    import app.billing.billing as billing
    rec = billing.get_billing(req.billing_id)
    if not rec:
        raise runtime.HTTPException(404, 'billing not found')
    if req.share_ttl_seconds is not None and (not rec.get('dry_run')):
        raise runtime.HTTPException(400, 'share_ttl_seconds is dry-run only')
    (_raw, view) = runtime._bill_client(rec['client_id'])
    return runtime._bill_share(rec, view, send_email=req.send_email, send_sms=req.send_sms, ttl=req.share_ttl_seconds)

def api_desk_bill_share_revoke(req: runtime.BillShareRequest):
    runtime._require_staff()
    import app.billing.billing as billing
    if not billing.get_billing(req.billing_id):
        raise runtime.HTTPException(404, 'billing not found')
    return {'ok': True, 'revoked': billing.revoke_shares(req.billing_id, 'pay' if req.pay_link else 'sb')}

def public_pay_link(token: str):
    """Branded pay link (signed, 30-day, revocable): 302 to the Stripe hosted invoice while it is open."""
    import app.billing.billing as billing
    import app.billing.stripe_pay as stripe_pay
    try:
        row = billing.verify_share(token, kind='pay')
    except billing.ShareError as e:
        return runtime._share_page(e.message, e.status, title='Payment link')
    try:
        inv = stripe_pay._get('/invoices/' + runtime.quote(str(row.get('path') or ''), safe=''))
    except RuntimeError:
        return runtime._share_page('The invoice could not be loaded right now. Try again shortly.', 502, title='Payment link')
    status = str(inv.get('status') or '')
    if status in runtime._PAY_CLOSED:
        return runtime._share_page(runtime._PAY_CLOSED[status], 200, title='Payment link')
    url = str(inv.get('hosted_invoice_url') or '')
    if status != 'open' or not url.startswith('https://invoice.stripe.com/'):
        return runtime._share_page('This invoice is not available. Contact the practice.', 404, title='Payment link')
    billing.note_hit(row['id'])
    return runtime.RedirectResponse(url, status_code=302, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

def public_superbill_share(token: str):
    """Branded, signed, expiring superbill link (30 days; rotate or revoke from the desk)."""
    import app.billing.billing as billing
    try:
        row = billing.verify_share(token)
    except billing.ShareError as e:
        return runtime._share_page(e.message, e.status)
    name = (row.get('name') or 'Superbill.pdf').replace('"', '')
    if row.get('source') == 'local':
        local = billing.dryrun_dir() / runtime.Path(str(row.get('path') or '')).name
        if not local.is_file():
            return runtime._share_page('This file is no longer available.', 404)
        data = local.read_bytes()
    else:
        (code, data, _ctype) = runtime._notify_get('/drive/file?path=' + runtime.quote(str(row.get('path') or ''), safe='/'))
        if code != 200:
            return runtime._share_page('The file could not be loaded right now. Try again shortly.', 502)
    billing.note_hit(row['id'])
    return runtime.Response(content=data, media_type='application/pdf', headers={'Content-Disposition': f'inline; filename="{name}"', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

router.add_api_route('/api/public/pay', api_public_pay, methods=['POST'])
router.add_api_route('/pay/thanks', pay_thanks, methods=['GET'], response_class=runtime.HTMLResponse)
router.add_api_route('/api/desk/fees', api_desk_fees, methods=['GET'])
router.add_api_route('/api/desk/fees', api_desk_fees_create, methods=['POST'])
router.add_api_route('/api/desk/fees/waive', api_desk_fees_waive, methods=['POST'])
router.add_api_route('/api/desk/fees/no-show', api_desk_fees_no_show, methods=['POST'])
router.add_api_route('/api/desk/fees/invoice', api_desk_fees_invoice, methods=['POST'])
router.add_api_route('/api/stripe/webhook', api_stripe_webhook, methods=['POST'])
router.add_api_route('/api/desk/superbill/visits', api_desk_superbill_visits, methods=['GET'])
router.add_api_route('/api/desk/superbill', api_desk_superbill, methods=['POST'])
router.add_api_route('/api/desk/superbill/send', api_desk_superbill_send, methods=['POST'])
router.add_api_route('/api/desk/bill', api_desk_bill_preview, methods=['GET'])
router.add_api_route('/api/desk/bill', api_desk_bill_confirm, methods=['POST'])
router.add_api_route('/api/desk/bill/superbill', api_desk_bill_superbill, methods=['POST'])
router.add_api_route('/api/desk/bill/share', api_desk_bill_share, methods=['POST'])
router.add_api_route('/api/desk/bill/share/revoke', api_desk_bill_share_revoke, methods=['POST'])
router.add_api_route('/pay/{token}', public_pay_link, methods=['GET'])
router.add_api_route('/sb/{token}', public_superbill_share, methods=['GET'])
