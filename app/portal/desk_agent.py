"""Pa, the staff assistant on the practice desk — tools only, never the public chat.
Writer: AWS Bedrock (Nova Pro, BAA-covered), same converse + bearer-token setup as scribe-aws. No xAI."""
from __future__ import annotations
from app.messaging.email_templates import HOST_SHORT
from app.booking.practice_links import book_host
from app.infra.config import timezone_name
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from app.portal.patients import client_by_id, create_client, search_clients
BEDROCK_TOKEN = (os.environ.get('AWS_BEARER_TOKEN_BEDROCK') or '').strip()
BEDROCK_REGION = os.environ.get('BEDROCK_REGION') or os.environ.get('AWS_REGION') or 'us-east-1'
BEDROCK_MODEL = os.environ.get('DESK_AGENT_MODEL') or os.environ.get('BEDROCK_MODEL_ID') or 'amazon.nova-pro-v1:0'
BEDROCK_URL = f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com/model/{urllib.parse.quote(BEDROCK_MODEL, safe='')}/converse"
SYSTEM = 'You are Pa, the practice assistant on ' + HOST_SHORT + "'s private desk.\nYou are not a dot-phrase bot and not a note-only scribe. Dot phrases are one tool. Notes are another.\nIf he asks for anything else, call the matching tool. Never answer that you only insert short phrases into notes.\nHe is the doctor. Look up patients, their chart folder and file names, visits, balance, the refill queue,\nthis week's calendar, booking links, forms, email, calls, note templates, and About the practice.\nNote templates include SOAP, AVS, blank, college withdrawal, school accommodations, lab request, and ESA letter.\nYou may save a new template with save_note_template. You do not sign notes.\nRefill cue: call cue_refill. A cue is not a fill. " + HOST_SHORT + ' signs and sends the order. Do not tell a patient it was filled.\nInvoice: call draft_invoice only when he asks. That creates an unsent Stripe draft. Do not send it.\nRules:\n- Never invent a patient, phone, balance, file, or visit. If a tool errors, say the error.\n- If no tool can do the ask, say that in one sentence and name the closest tool. Then stop.\n- Confirm before sending mail, booking, charging, or placing a call.\n- Booking links: ALWAYS call booking_link fresh. Give exactly the url it returns\n  (short form ' + book_host() + '/b/XXXXXXXX). Never reuse or retype a link from earlier messages.\n- To call someone, use place_call. That only prepares the call. Do not put a reason for the call in the chat.\n- Keep replies short. Prefer the tool result over a guess.'
TOOLS = [{'type': 'function', 'function': {'name': 'client_visits', 'description': 'Next and recent visits for one patient (from the practice calendar). Pass client_id from search_clients, or a name.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}}}}}, {'type': 'function', 'function': {'name': 'search_clients', 'description': 'Look up patients in the practice directory.', 'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']}}}, {'type': 'function', 'function': {'name': 'create_client', 'description': 'Add a new patient to the directory.', 'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'email': {'type': 'string'}, 'phone': {'type': 'string'}}, 'required': ['name', 'email']}}}, {'type': 'function', 'function': {'name': 'send_forms', 'description': 'Email or mint form links. items: intake, personal, consents, card, roi, pay.', 'parameters': {'type': 'object', 'properties': {'email': {'type': 'string'}, 'name': {'type': 'string'}, 'items': {'type': 'array', 'items': {'type': 'string'}}, 'send_email': {'type': 'boolean'}}, 'required': ['email', 'items']}}}, {'type': 'function', 'function': {'name': 'send_email', 'description': 'Send a branded practice email. kind: form_share, intake, book_link, payment_due, document.', 'parameters': {'type': 'object', 'properties': {'to': {'type': 'string'}, 'kind': {'type': 'string'}, 'name': {'type': 'string'}, 'notes': {'type': 'string'}, 'url': {'type': 'string'}, 'title': {'type': 'string'}}, 'required': ['to', 'kind']}}}, {'type': 'function', 'function': {'name': 'booking_link', 'description': "Get a client's personal booking link (" + book_host() + '/b/..., never expires; prefills their details). Give the url to ' + HOST_SHORT + '. send_email=true also emails it to the client. Find client_id with search_clients first.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'send_email': {'type': 'boolean'}}, 'required': ['client_id']}}}, {'type': 'function', 'function': {'name': 'book_visit', 'description': 'Book a follow-up on the calendar. start is ISO datetime.', 'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'email': {'type': 'string'}, 'phone': {'type': 'string'}, 'start': {'type': 'string'}, 'duration_minutes': {'type': 'integer'}, 'notes': {'type': 'string'}}, 'required': ['name', 'email', 'start']}}}, {'type': 'function', 'function': {'name': 'list_note_templates', 'description': 'List after-visit / DAP / addendum note templates.', 'parameters': {'type': 'object', 'properties': {}}}}, {'type': 'function', 'function': {'name': 'get_note_template', 'description': 'Read one note template markdown (id: avs, dap, addendum, or a custom id).', 'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}}}, {'type': 'function', 'function': {'name': 'save_note_template', 'description': 'Create or overwrite a note template. body is markdown with {{field}} placeholders.', 'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'body': {'type': 'string'}}, 'required': ['id', 'body']}}}, {'type': 'function', 'function': {'name': 'place_call', 'description': 'Prepare an outbound call from the office DID. Does not dial until the doctor confirms. Pass client_id, a directory name, or e164.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'name': {'type': 'string'}, 'e164': {'type': 'string', 'description': 'Destination phone, any common format.'}}}}}, {'type': 'function', 'function': {'name': 'desk_week', 'description': "This week's calendar (or a start date). Names and times only. days 1-14.", 'parameters': {'type': 'object', 'properties': {'start': {'type': 'string', 'description': 'YYYY-MM-DD. Empty means this Monday.'}, 'days': {'type': 'integer'}}}}}, {'type': 'function', 'function': {'name': 'client_files', 'description': "One patient's directory row plus chart/billing folder links and file names. Not the note body.", 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}}}}}, {'type': 'function', 'function': {'name': 'client_balance', 'description': 'Open fees and amount owed for one patient. Does not charge.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}}}}}, {'type': 'function', 'function': {'name': 'refill_queue', 'description': "Refill queue summary, or one patient's recent refill rows if q or client_id is set.", 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}}}}}, {'type': 'function', 'function': {'name': 'list_dot_phrases', 'description': 'List dot-phrase triggers. Use only when he asks for a phrase or expander, not for other work.', 'parameters': {'type': 'object', 'properties': {}}}}, {'type': 'function', 'function': {'name': 'cue_refill', 'description': 'Decide whether a refill can be cued for the doctor to sign. Does not send a prescription or tell the patient it was filled. controlled: none, C2, C3, C4, C5, or unknown.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}, 'controlled': {'type': 'string'}}}}}, {'type': 'function', 'function': {'name': 'draft_invoice', 'description': 'Create an unsent Stripe draft for one patient. Does not email or finalize. amount_cents is integer cents.', 'parameters': {'type': 'object', 'properties': {'client_id': {'type': 'string'}, 'q': {'type': 'string'}, 'amount_cents': {'type': 'integer'}, 'description': {'type': 'string'}}, 'required': ['amount_cents']}}}, {'type': 'function', 'function': {'name': 'get_practice', 'description': 'Read About the practice: name, legal, clinician, address, phone, fax, email, EIN, NPI.', 'parameters': {'type': 'object', 'properties': {}}}}, {'type': 'function', 'function': {'name': 'save_practice', 'description': 'Update About the practice letterhead fields. Omit keys you are not changing.', 'parameters': {'type': 'object', 'properties': {'display_name': {'type': 'string'}, 'legal_name': {'type': 'string'}, 'clinician_name': {'type': 'string'}, 'clinician_title': {'type': 'string'}, 'address': {'type': 'string'}, 'phone': {'type': 'string'}, 'fax': {'type': 'string'}, 'email': {'type': 'string'}, 'website': {'type': 'string'}, 'meet': {'type': 'string'}, 'ein': {'type': 'string'}, 'npi': {'type': 'string'}, 'billing_role': {'type': 'string', 'description': 'psychiatrist or therapist'}, 'theme': {'type': 'object', 'description': 'mode light|dark|custom, allow_toggle, tokens hex map'}, 'page_titles': {'type': 'object', 'description': 'Titles for the two focus pages. Keys: ocd, anxiety.'}, 'licenses': {'type': 'array', 'items': {'type': 'object', 'properties': {'state': {'type': 'string'}, 'number': {'type': 'string'}}}}}}}}]
_THINK = re.compile('<thinking>.*?</thinking>', re.S)

def _tool_config() -> dict[str, Any]:
    specs = []
    for t in TOOLS:
        fn = t.get('function') or {}
        specs.append({'toolSpec': {'name': fn.get('name') or '', 'description': fn.get('description') or fn.get('name') or '', 'inputSchema': {'json': fn.get('parameters') or {'type': 'object', 'properties': {}}}}})
    return {'tools': specs}

def _bedrock(system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One Bedrock converse turn (Nova Pro). Messages are Bedrock-shaped content blocks."""
    if not BEDROCK_TOKEN:
        raise RuntimeError('AWS_BEARER_TOKEN_BEDROCK unset (desk agent runs on Bedrock only)')
    payload = json.dumps({'system': [{'text': system}], 'messages': messages, 'toolConfig': _tool_config(), 'inferenceConfig': {'temperature': 0.2, 'maxTokens': 2500}}).encode()
    req = urllib.request.Request(BEDROCK_URL, data=payload, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + BEDROCK_TOKEN}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'bedrock {e.code}: {e.read()[:200]!r}') from e

def _client_visits(args: dict[str, Any]) -> dict[str, Any]:
    from zoneinfo import ZoneInfo
    from app.infra.config import load_config
    from app.main import _visits_for_client
    cid = str(args.get('client_id') or '').strip()
    row = client_by_id(cid) if cid else None
    if not row:
        hits = search_clients(str(args.get('q') or cid or ''))
        hits = hits.get('clients') if isinstance(hits, dict) else hits
        hits = [h for h in hits or [] if isinstance(h, dict)]
        if len(hits) != 1:
            return {'error': 'pick one patient first', 'matches': [{'id': h.get('id'), 'name': h.get('name')} for h in hits[:8]]}
        row = client_by_id(str(hits[0].get('id') or '')) or hits[0]
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    visits = _visits_for_client(row, tz)
    keep = ('title', 'when_label', 'duration_minutes', 'start_iso')
    up = sorted([v for v in visits if v.get('upcoming')], key=lambda v: v.get('start_iso') or '')
    past = [v for v in visits if not v.get('upcoming')][:3]
    return {'name': row.get('name') or '', 'next': {k: up[0].get(k) for k in keep} if up else None, 'upcoming': [{k: v.get(k) for k in keep} for v in up[:6]], 'recent': [{k: v.get(k) for k in keep} for v in past]}

def _resolve_client(args: dict[str, Any]) -> dict[str, Any]:
    cid = str(args.get('client_id') or '').strip()
    row = client_by_id(cid) if cid else None
    if row:
        return row
    hits = search_clients(str(args.get('q') or cid or ''))
    hits = hits.get('clients') if isinstance(hits, dict) else hits
    hits = [h for h in hits or [] if isinstance(h, dict)]
    if len(hits) != 1:
        return {'error': 'pick one patient first', 'matches': [{'id': h.get('id'), 'name': h.get('name')} for h in hits[:8]]}
    return client_by_id(str(hits[0].get('id') or '')) or hits[0]

def _file_names(listing: dict[str, Any]) -> list[str]:
    raw = listing.get('entries') or listing.get('files') or listing.get('children') or listing.get('items') or []
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw[:40]:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(str(item.get('name') or item.get('path') or item.get('title') or ''))
    return [n for n in names if n][:40]

def _desk_week(args: dict[str, Any]) -> dict[str, Any]:
    from app.main import _desk_week_payload
    start = str(args.get('start') or '')[:10]
    try:
        days = int(args.get('days') or 7)
    except (TypeError, ValueError):
        days = 7
    payload = _desk_week_payload(start, days)
    visits = payload.get('visits') if isinstance(payload, dict) else None
    if visits is None and isinstance(payload, dict):
        visits = payload.get('events') or []
    compact = []
    for v in visits or []:
        if not isinstance(v, dict) or v.get('ok') is False:
            continue
        compact.append({k: v.get(k) for k in ('when_label', 'title', 'name', 'duration_minutes', 'event_type', 'all_day', 'start_iso')})
    return {'count': len(compact), 'events': compact[:40]}

def _client_files(args: dict[str, Any]) -> dict[str, Any]:
    row = _resolve_client(args)
    if row.get('error'):
        return row
    path = str(row.get('drive_folder_path') or '')
    names: list[str] = []
    if path:
        from app.main import _drive_list
        names = _file_names(_drive_list(path) or {})
    return {'id': row.get('id') or '', 'name': row.get('name') or '', 'email': row.get('email') or '', 'phone': row.get('phone') or '', 'dob': row.get('dob') or '', 'status': row.get('status') or '', 'chart_url': row.get('chart_url') or '', 'billing_url': row.get('billing_url') or '', 'drive_folder_path': path, 'files': names}

def _client_balance(args: dict[str, Any]) -> dict[str, Any]:
    import app.billing.fees as fees
    row = _resolve_client(args)
    if row.get('error'):
        return row
    cid = str(row.get('id') or '')
    owed = fees.owed_cents(cid)
    open_rows = fees.list_open(cid)
    return {'id': cid, 'name': row.get('name') or '', 'owed_cents': owed, 'owed': fees.dollars(owed), 'open': [fees.public_line(r) for r in open_rows[:8]]}

def _refill_queue(args: dict[str, Any]) -> dict[str, Any]:
    from app.portal.refills import list_requests, summary
    out: dict[str, Any] = {'summary': summary()}
    q = str(args.get('q') or '').strip().lower()
    cid = str(args.get('client_id') or '').strip()
    if not q and (not cid):
        return out
    keep = ('id', 'status', 'recommendation', 'client_id', 'display_name', 'name', 'medication', 'created_at')
    rows = []
    for row in list_requests(limit=40):
        if not isinstance(row, dict):
            continue
        if cid and str(row.get('client_id') or '') != cid:
            continue
        blob = ' '.join((str(row.get(k) or '') for k in ('display_name', 'name', 'medication', 'email'))).lower()
        if q and q not in blob:
            continue
        rows.append({k: row.get(k) for k in keep if k in row or row.get(k)})
    out['rows'] = rows[:12]
    return out

def _cue_refill(args: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.infra.config import load_config
    from app.main import _visits_for_client
    from app.portal.refills import fill_cue
    row = _resolve_client(args)
    if row.get('error'):
        return row
    tz = ZoneInfo(load_config().get('timezone', timezone_name()))
    today = datetime.now(tz).date()
    visits = _visits_for_client(row, tz)
    past = [v.get('start_iso') or '' for v in visits if not v.get('upcoming') and v.get('start_iso')]
    fut = sorted((v.get('start_iso') or '' for v in visits if v.get('upcoming') and v.get('start_iso')))
    last = datetime.fromisoformat(max(past)).date() if past else None
    nxt = datetime.fromisoformat(fut[0]).date() if fut else None
    age = None
    dob = str(row.get('dob') or '')[:10]
    if dob:
        try:
            born = datetime.fromisoformat(dob).date()
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except ValueError:
            age = None
    plan = fill_cue(controlled=str(args.get('controlled') or 'unknown'), age_years=age, last_visit=last, next_visit=nxt, today=today)
    plan['name'] = row.get('name') or ''
    plan['id'] = row.get('id') or ''
    return plan

def _draft_invoice(args: dict[str, Any]) -> dict[str, Any]:
    from app.billing.stripe_pay import draft_invoice, ensure_customer, stripe_ready
    row = _resolve_client(args)
    if row.get('error'):
        return row
    if not stripe_ready():
        return {'error': 'Stripe is not on the booker', 'sent': False}
    try:
        cents = int(args.get('amount_cents') or 0)
    except (TypeError, ValueError):
        return {'error': 'amount_cents must be an integer', 'sent': False}
    cus = ensure_customer(email=str(row.get('email') or ''), client_id=str(row.get('id') or ''))
    draft = draft_invoice(customer_id=cus, amount_cents=cents, description=str(args.get('description') or 'Visit'), client_id=str(row.get('id') or ''))
    draft['name'] = row.get('name') or ''
    draft['sent'] = False
    return draft

def _list_dot_phrases() -> dict[str, Any]:
    from app.notes.dot_phrases import list_phrases
    phrases = []
    for row in list_phrases()[:40]:
        if not isinstance(row, dict):
            continue
        phrases.append({'id': row.get('id') or '', 'title': row.get('title') or '', 'trigger': row.get('trigger') or row.get('id') or ''})
    return {'phrases': phrases}

def _run_tool(name: str, args: dict[str, Any], book_fn, send_fn, mail_fn, link_fn=None) -> Any:
    if name == 'booking_link':
        return link_fn(args) if link_fn else {'error': 'booking links unavailable'}
    if name == 'client_visits':
        return _client_visits(args)
    if name == 'desk_week':
        return _desk_week(args)
    if name == 'client_files':
        return _client_files(args)
    if name == 'client_balance':
        return _client_balance(args)
    if name == 'refill_queue':
        return _refill_queue(args)
    if name == 'list_dot_phrases':
        return _list_dot_phrases()
    if name == 'cue_refill':
        return _cue_refill(args)
    if name == 'draft_invoice':
        return _draft_invoice(args)
    if name == 'search_clients':
        return search_clients(str(args.get('q') or ''))
    if name == 'create_client':
        return create_client(name=str(args.get('name') or ''), email=str(args.get('email') or ''), phone=str(args.get('phone') or ''))
    if name == 'send_forms':
        return send_fn(args)
    if name == 'send_email':
        return mail_fn(args)
    if name == 'book_visit':
        return book_fn(args)
    if name == 'list_note_templates':
        from app.notes.note_templates import list_templates
        return {'templates': list_templates()}
    if name == 'get_note_template':
        from app.notes.note_templates import get_template
        spec = get_template(str(args.get('id') or ''))
        return spec or {'error': 'unknown template'}
    if name == 'save_note_template':
        from app.notes.note_templates import save_template
        try:
            return save_template(str(args.get('id') or ''), str(args.get('body') or ''))
        except ValueError as e:
            return {'error': str(e)}
    if name == 'place_call':
        from app.portal.desk_call import preview_call
        return preview_call(client_id=str(args.get('client_id') or ''), name=str(args.get('name') or ''), e164=str(args.get('e164') or args.get('phone') or ''))
    if name == 'get_practice':
        from app.infra.practice import load as load_practice, public as public_practice
        from app.portal.desk_call import config as call_config
        p = load_practice()
        pub = public_practice()
        call = call_config()
        return {k: p.get(k) for k in ('display_name', 'legal_name', 'clinician_name', 'clinician_title', 'address', 'phone', 'fax', 'email', 'website', 'meet', 'ein', 'npi', 'licenses', 'billing_role', 'theme', 'page_titles')} | {'has_logo': pub.get('has_logo'), 'has_photo': pub.get('has_photo'), 'has_callback': bool(call.get('has_callback')), 'outbound_did': call.get('did_pretty') or call.get('did') or ''}
    if name == 'save_practice':
        from app.infra.practice import save as save_practice
        keys = ('display_name', 'legal_name', 'clinician_name', 'clinician_title', 'address', 'phone', 'fax', 'email', 'website', 'meet', 'ein', 'npi', 'licenses', 'billing_role', 'theme', 'page_titles')
        payload = {k: args[k] for k in keys if k in args and args[k] is not None}
        return save_practice(payload)
    return {'error': 'unknown tool'}
_BOOK_LINK = re.compile('https?://[^\\s)>\\]\\"\']+/b/[^\\s)>\\]\\"\']+')

def _scrub_links(text: str, keep: set[str] | None=None) -> str:
    """Drop booking links Pa did not get from booking_link this turn (old long links in chat history)."""
    return _BOOK_LINK.sub(lambda m: m.group(0) if keep and m.group(0).rstrip('.,;:!') in keep else '[booking link: ask Pa for a fresh one]', text)

def _history(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Desk chat history -> Bedrock messages: starts with user, roles alternate (same-role turns merged)."""
    out: list[dict[str, Any]] = []
    for m in history[-16:]:
        role = m.get('role') or 'user'
        content = _scrub_links((m.get('content') or '').strip()[:4000])
        if role not in ('user', 'assistant') or not content:
            continue
        if not out and role != 'user':
            continue
        if out and out[-1]['role'] == role:
            out[-1]['content'][0]['text'] += '\n\n' + content
        else:
            out.append({'role': role, 'content': [{'text': content}]})
    while out and out[-1]['role'] != 'user':
        out.pop()
    return out

def run_desk_agent(history: list[dict[str, str]], *, book_fn, send_fn, mail_fn, link_fn=None) -> dict[str, Any]:
    messages = _history(history)
    if not messages:
        return {'ok': False, 'error': 'empty'}
    used: list[str] = []
    links: set[str] = set()
    action: dict[str, Any] | None = None
    for _ in range(8):
        data = _bedrock(SYSTEM, messages)
        msg = (data.get('output') or {}).get('message') or {}
        blocks = [b for b in msg.get('content') or [] if isinstance(b, dict)]
        calls = [b['toolUse'] for b in blocks if isinstance(b.get('toolUse'), dict)]
        text = _THINK.sub('', ''.join((str(b.get('text') or '') for b in blocks))).strip()
        if not calls:
            text = _scrub_links(text, links)
            out = {'ok': True, 'reply': text or '(no reply)', 'model': 'bedrock-' + BEDROCK_MODEL, 'tools': used}
            if action:
                out['action'] = action
            return out
        messages.append({'role': 'assistant', 'content': blocks})
        results = []
        for call in calls:
            name = str(call.get('name') or '')
            args = call.get('input') if isinstance(call.get('input'), dict) else {}
            used.append(name)
            try:
                result = _run_tool(name, args, book_fn, send_fn, mail_fn, link_fn)
            except Exception as e:
                result = {'error': str(e)[:200]}
            if name == 'booking_link' and isinstance(result, dict) and result.get('url'):
                links.add(str(result['url']))
            if isinstance(result, dict) and result.get('action') == 'confirm_call':
                action = {k: result[k] for k in result if k != 'pending'}
            results.append({'toolResult': {'toolUseId': call.get('toolUseId') or name, 'content': [{'text': json.dumps(result, default=str)[:12000]}]}})
        messages.append({'role': 'user', 'content': results})
    return {'ok': False, 'error': 'too many tool rounds', 'tools': used}
