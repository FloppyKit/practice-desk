"""Carry the last visit's after-visit summary into a new draft, then update it from a transcript.

carry(client_id) -> {source, dos, fields}
  Last dated visit form in the chart: a signed desk AVS, or an imported
  "After Visit Summary" file, whichever date of service is later.
  AVS sections are also copied into `plan` in the same bullet layout so a SOAP
  note and an AVS open with the same plan.

update_from_transcript(fields, transcript) -> {changed: {id: text}}
  Bedrock (AWS, under BAA); click or scribe-draft open only. Keeps the prior
  bullet format. Visit-specific sections (date, interval history, addendum)
  are never carried. Nothing signs here.
"""
from __future__ import annotations
from app.messaging.email_templates import HOST_SHORT
from app.infra.config import timezone_name
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import app.infra.clients_db as clients_db
NEVER_CARRY = {'dos', 'subjective', 'interval_update', 'addendum', 'body', 'event_id', 'visit', 'name', 'dob', 'physician'}
UPDATABLE = ('subjective', 'objective', 'assessment', 'interval_update', 'medications', 'supplements', 'recommendations', 'return_visit', 'plan')
AVS_KEYS = ('medications', 'supplements', 'recommendations', 'return_visit')
_AVS_LABELS = (('medications', 'Medications'), ('supplements', 'Supplements'), ('recommendations', 'Other recommendations'), ('return_visit', 'Meet again'))
_HEAD = {'medications': 'medications', 'supplements': 'supplements', 'other recommendations': 'recommendations', 'recommendations': 'recommendations', 'meet again': 'return_visit', 'return visit': 'return_visit'}

def _dos(value: str) -> str:
    return (value or '')[:10]

def parse_chart_avs(text: str) -> dict[str, str]:
    """Pull AVS sections out of a chart visit form. Keeps the original bullets."""
    lines = []
    for ln in (text or '').lstrip('\ufeff').splitlines():
        if ln.strip().startswith('![]('):
            continue
        lines.append(ln.rstrip())
    buckets: dict[str, list[str]] = {}
    cur = ''
    for ln in lines:
        s = ln.strip()
        if not s or s.lower() in {'&nbsp;', '&amp;nbsp;'}:
            if cur:
                buckets[cur].append('')
            continue
        hm = re.match('^#{1,3}\\s+\\**\\s*([A-Za-z][^:*]{0,40}?)\\s*:?\\**\\s*$', s)
        bm = re.match('^\\*\\*([^*]{1,40})\\*\\*\\s*:?\\s*(.*)$', s)
        label = ''
        rest = ''
        if hm:
            label = hm.group(1).strip().lower()
        elif bm:
            label = bm.group(1).strip().rstrip(':').lower()
            rest = (bm.group(2) or '').strip()
        key = _HEAD.get(label)
        if key:
            cur = key
            buckets.setdefault(cur, [])
            if rest:
                buckets[cur].append(rest)
            continue
        if bm or (s.startswith('#') and (not key)):
            cur = ''
            continue
        if cur:
            buckets[cur].append(s)
    out: dict[str, str] = {}
    for (key, buf) in buckets.items():
        while buf and (not buf[-1].strip()):
            buf.pop()
        body = '\n'.join(buf).strip()
        if body:
            out[key] = body
    return out

def plan_from_avs(fields: dict[str, str]) -> str:
    """Same bullets as the AVS, with the section headers the SOAP plan uses."""
    blocks = []
    for (key, label) in _AVS_LABELS:
        body = str((fields or {}).get(key) or '').strip()
        if body:
            blocks.append(f'{label}:\n{body}')
    return '\n\n'.join(blocks)

def _has_bullets(text: str) -> bool:
    return bool(re.search('(?m)^\\s*[○•*\\-]\\s+\\S', text or ''))

def _split_medications(changed: dict[str, str], fields: dict[str, str]) -> dict[str, str]:
    """The note agent must leave one medication per bullet, even if the model glued two onto one line."""
    src = changed.get('medications', fields.get('medications') or '')
    if not str(src).strip():
        return changed
    from app.notes.note_pdf import one_med_per_line
    split = one_med_per_line(str(src))
    if split.strip() == str(fields.get('medications') or '').strip() and 'medications' not in changed:
        return changed
    if split.strip() == str(changed.get('medications') or '').strip() and 'medications' in changed:
        return changed
    out = dict(changed)
    out['medications'] = split
    return out

def _with_plan(changed: dict[str, str], fields: dict[str, str]) -> dict[str, str]:
    """If an AVS section moved, keep SOAP plan in the same bullet layout."""
    if not any((k in changed for k in AVS_KEYS)):
        return changed
    merged = dict(fields)
    merged.update(changed)
    if not any((_has_bullets(str(merged.get(k) or '')) for k in AVS_KEYS)):
        return changed
    plan = plan_from_avs(merged)
    if plan and plan != str(fields.get('plan') or '').strip():
        changed = dict(changed)
        changed['plan'] = plan
    return changed

def _signed_avs(client_id: str) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        try:
            rows = con.execute('SELECT dos, fields_json, signed_at FROM signed_notes WHERE client_id=? ORDER BY dos DESC, signed_at DESC, rowid DESC LIMIT 20', (client_id,)).fetchall()
        except Exception:
            return None
    for r in rows:
        try:
            raw = json.loads(r['fields_json'] or '{}')
        except json.JSONDecodeError:
            continue
        fields = {k: str(v) for (k, v) in raw.items() if k not in NEVER_CARRY and str(v or '').strip()}
        if any((fields.get(k) for k in AVS_KEYS)) or fields.get('plan'):
            if not any((fields.get(k) for k in AVS_KEYS)) and fields.get('plan'):
                pass
            elif any((fields.get(k) for k in AVS_KEYS)):
                fields['plan'] = plan_from_avs(fields) or fields.get('plan') or ''
                if not fields['plan']:
                    fields.pop('plan', None)
            return {'source': 'signed', 'dos': _dos(r['dos'] or ''), 'fields': fields}
    return None

def _imported_avs(client_id: str) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        try:
            r = con.execute("SELECT dos, content FROM imported_notes WHERE client_id=? AND source_file LIKE '%/After Visit Summary - %' ORDER BY dos DESC, rowid DESC LIMIT 1", (client_id,)).fetchone()
        except Exception:
            return None
    if not r:
        return None
    parsed = parse_chart_avs(r['content'] or '')
    fields = {k: v for (k, v) in parsed.items() if k not in NEVER_CARRY and v.strip()}
    plan = plan_from_avs(fields)
    if plan:
        fields['plan'] = plan
    if not fields:
        return None
    return {'source': 'imported', 'dos': _dos(r['dos'] or ''), 'fields': fields}

def carry(client_id: str) -> dict[str, Any] | None:
    """Last dated visit AVS in the chart. Signed wins a date tie (richer fields)."""
    if not client_id:
        return None
    signed = _signed_avs(client_id)
    imported = _imported_avs(client_id)
    if signed and imported:
        return signed if signed['dos'] >= imported['dos'] else imported
    return signed or imported

def transcript_for(client_id: str, event_id: str='', transcript_id: str='') -> tuple[str, str]:
    """-> (text, source). source: picked | linked | recent (newest within 48 h) | none."""
    import app.notes.scribe_drafts as scribe_drafts
    with scribe_drafts._connect() as con:
        if transcript_id:
            r = con.execute("SELECT transcript FROM scribe_drafts WHERE id=? AND transcript!='' AND link_status!='multi' AND (client_id=? OR client_id='')", (transcript_id, client_id)).fetchone()
            if r:
                return (r[0], 'picked')
        if event_id:
            r = con.execute("SELECT transcript FROM scribe_drafts WHERE event_id=? AND transcript!='' AND link_status!='multi' ORDER BY created DESC LIMIT 1", (event_id,)).fetchone()
            if r:
                return (r[0], 'linked')
    t = latest_transcript(client_id)
    return (t, 'recent') if t else ('', 'none')

def is_multi(transcript_id: str) -> bool:
    import app.notes.scribe_drafts as scribe_drafts
    with scribe_drafts._connect() as con:
        r = con.execute('SELECT link_status FROM scribe_drafts WHERE id=?', (transcript_id,)).fetchone()
    return bool(r) and r[0] == 'multi'

def unlinked_on_day(client_id: str, day_start_utc: datetime, day_end_utc: datetime) -> list[dict[str, Any]]:
    """This client's (or client-less) unlinked transcripts recorded on the visit's CT day. No text."""
    import app.notes.scribe_drafts as scribe_drafts
    from app.booking.visit_link import recording_window
    out = []
    with scribe_drafts._connect() as con:
        rows = [dict(r) for r in con.execute("SELECT id, created, ended_at, duration_minutes, client_id, link_status FROM scribe_drafts WHERE event_id='' AND transcript!='' AND link_status!='multi' AND (client_id=? OR client_id='')", (client_id,))]
    for r in rows:
        (st, en) = recording_window(r)
        if en and day_start_utc <= en < day_end_utc:
            out.append({'id': r['id'], 'end_utc': en.isoformat(), 'minutes': int(r['duration_minutes'] or 0), 'status': r['link_status'] or 'unlinked'})
    return sorted(out, key=lambda x: x['end_utc'])

def latest_transcript(client_id: str, *, hours: int=48) -> str:
    if not client_id:
        return ''
    import app.notes.scribe_drafts as scribe_drafts
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with scribe_drafts._connect() as con:
            r = con.execute("SELECT transcript FROM scribe_drafts WHERE client_id=? AND created>=? AND transcript!='' AND link_status!='multi' ORDER BY created DESC LIMIT 1", (client_id, since)).fetchone()
    except Exception:
        return ''
    return (r[0] if r else '') or ''

def coverage(transcript: str, *, visit_minutes: int=0) -> dict[str, Any]:
    """Whether this audio can support a full update. No transcript text in the result."""
    text = (transcript or '').strip()
    words = re.findall("[A-Za-z0-9']+", text)
    flags: list[str] = []
    if len(text) < 400 or len(words) < 80:
        flags.append('thin')
    if visit_minutes >= 15 and len(words) < visit_minutes * 25:
        flags.append('shorter_than_visit')
    tail = text[-800:].lower()
    closers = ('see you', 'next time', 'bye', 'take care', 'follow up', 'follow-up', "we'll meet", 'meet again')
    if len(words) >= 80 and (not any((c in tail for c in closers))):
        flags.append('ended_early')
    if 'thin' in flags:
        note = 'Transcript is too short to cover this visit. The prior note is loaded; do not treat this as a full hearing.'
    elif 'ended_early' in flags:
        note = 'Transcript does not run to a close. Anything after the audio stopped was not heard.'
    elif 'shorter_than_visit' in flags:
        note = 'Transcript is shorter than the visit. Part of the visit was not heard.'
    else:
        note = ''
    return {'heard': 'full' if not flags else 'partial', 'flags': flags, 'chars': len(text), 'words': len(words), 'note': note}

def _keep_unmentioned(changed: dict[str, str], fields: dict[str, str], transcript: str) -> dict[str, str]:
    """A line from the prior note that this session did not mention stays, even if the model dropped it."""
    spoken = re.sub('[^a-z0-9]+', ' ', (transcript or '').lower())
    out = dict(changed)
    for (key, new) in list(changed.items()):
        old = str(fields.get(key) or '')
        if not old.strip():
            continue
        extra = []
        for line in old.splitlines():
            s = line.strip()
            if not s or s.endswith(':'):
                continue
            words = [w for w in re.findall('[a-z0-9]+', s.lower()) if len(w) > 3]
            if not words:
                continue
            if any((w in spoken for w in words[:4])):
                continue
            if s.lower() in new.lower():
                continue
            extra.append(line.rstrip())
        if extra:
            out[key] = new.rstrip() + '\n' + '\n'.join(extra)
    return out
_SYSTEM = 'You update a psychiatrist\'s draft visit note from today\'s visit transcript. Input is JSON: {"draft": {section_id: text}, "transcript": "..."}. The after-visit sections already on the draft are the last visit\'s summary, in ' + HOST_SHORT + '\'s bullet format. Update ONLY what the transcript supports. Keep every bullet that was not discussed, with the same bullet character (○, •, *, or -) and the same line breaks. Do not rewrite bullets into prose. If a medication, supplement, or recommendation line was not discussed this session, copy that line through unchanged. If a dose, stop, start, or new item was said, edit that bullet or add one in the same format. Medications: one medication per line, each line starting with a bullet. A new action such as Start, Stop, Continue, Increase, or Reduce begins its own bullet. Never put two medications on the same line. Reply with JSON only: {"changed": {section_id: "full new text"}} listing only sections you changed. Use only section ids that appear in the draft. If interval_update is in the draft, put a short narrative of today\'s visit there. That narrative is not a substitute for the plan. If the transcript changes a medication, supplement, recommendation, or follow-up, edit that bullet in its own section. Do not leave last visit\'s plan unchanged when the transcript stated a change, and do not hide the only copy of a change inside interval_update. If return_visit is in the draft, set it to the follow-up interval said in the visit, such as In 12 weeks. Do not return subjective, objective, assessment, or plan unless that id is in the draft.'
_INTERVAL = re.compile('\\b(\\d{1,2})\\s*(week|weeks|month|months)\\b', re.I)

def _day(value: str):
    s = (value or '').strip()[:10]
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None

def spoken_interval(transcript: str) -> str:
    """Last follow-up interval said in the visit, as 'In N weeks'. Empty if none."""
    hits = list(_INTERVAL.finditer(transcript or ''))
    if not hits:
        return ''
    n = int(hits[-1].group(1))
    unit = hits[-1].group(2).lower()
    if n < 1 or n > 52:
        return ''
    if unit.startswith('month'):
        word = 'month' if n == 1 else 'months'
        days = n * 30
    else:
        word = 'week' if n == 1 else 'weeks'
        days = n * 7
    return (f'In {n} {word}', days)

def format_meet_when(raw: str, minutes: int=0) -> str:
    """Date, plus the clock time and the booked length when the next visit has them.

    "December 8, 2026, 11:00 AM - 30 min". The AVS should say how long the next visit is.
    """
    s = (raw or '').strip()
    if len(s) > 10:
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            if dt.tzinfo:
                dt = dt.astimezone(ZoneInfo(timezone_name()))
            clock = dt.strftime('%I:%M %p').lstrip('0')
            out = f"{dt.strftime('%B')} {dt.day}, {dt.year}, {clock}"
            if int(minutes or 0) >= 5:
                out += f' - {int(minutes)} min'
            return out
        except ValueError:
            pass
    nxt = _day(s)
    if not nxt:
        return ''
    return f"{nxt.strftime('%B')} {nxt.day}, {nxt.year}"

def meet_again_line(visit_day: str, next_day: str, transcript: str='', next_minutes: int=0) -> tuple[str, str]:
    """Meet again from the next booked visit, confirmed by the transcript when they agree.

    Returns (line, source). source is booked, booked_confirmed, transcript, or empty.
    The booked line includes the visit time (and length) when next_day is a full timestamp.
    """
    spoken = spoken_interval(transcript)
    (spoken_line, spoken_days) = spoken if spoken else ('', 0)
    visit = _day(visit_day)
    nxt = _day(next_day)
    if nxt and visit and (nxt > visit):
        days = (nxt - visit).days
        booked = format_meet_when(next_day, next_minutes)
        if spoken_line and abs(spoken_days - days) <= 10:
            return (booked, 'booked_confirmed')
        return (booked, 'booked')
    if spoken_line:
        return (spoken_line, 'transcript')
    return ('', '')

def _absorb_model(fields: dict[str, str], ch: dict) -> dict[str, str]:
    """Keep model edits that match the open form. A SOAP spill still fills those fields."""
    form = {k for k in fields} - {'dos', 'addendum', 'body', 'name', 'dob', 'physician'}
    changed: dict[str, str] = {}
    for (k, v) in ch.items():
        text = str(v or '').strip()
        if k in form and text and (text != str(fields.get(k) or '').strip()):
            changed[k] = text[:8000]
    plan = str(ch.get('plan') or '')
    if plan:
        from app.notes.drive_note import parse_note_text
        parsed = parse_note_text(plan)
        for k in ('medications', 'supplements', 'recommendations', 'return_visit', 'interval_update'):
            text = str(parsed.get(k) or '').strip()
            if k in form and k not in changed and text and (text != str(fields.get(k) or '').strip()):
                changed[k] = text[:8000]
    narrative = str(ch.get('subjective') or '').strip()
    if narrative and 'interval_update' in form and ('interval_update' not in changed):
        if narrative != str(fields.get('interval_update') or '').strip():
            changed['interval_update'] = narrative[:8000]
    return changed

def _system_prompt() -> str:
    extra = ''
    try:
        from app.notes.note_prefs import prompt_block
        extra = prompt_block()
    except Exception:
        extra = ''
    return _SYSTEM + ('\n' + extra if extra else '')

def update_from_transcript(fields: dict[str, str], transcript: str, *, call: Callable[[str, str], str] | None=None, client_id: str='', event_id: str='', visit_day: str='', next_visit_day: str='', next_visit_minutes: int=0) -> dict[str, str]:
    allowed = {k for k in fields} | set(UPDATABLE)
    allowed -= {'dos', 'addendum', 'body', 'name', 'dob', 'physician'}
    spoken = transcript or ''
    if len(spoken) > 18000:
        spoken = spoken[:6000] + '\n\n[...middle of transcript omitted...]\n\n' + spoken[-12000:]
    payload = json.dumps({'draft': {k: v for (k, v) in fields.items() if k in allowed}, 'transcript': spoken})
    if call is None:
        from app.portal.refills import _bedrock_text

        def call(sys_: str, txt: str) -> str:
            return _bedrock_text(sys_, txt, max_tokens=1600)
    raw = call(_system_prompt(), payload) or ''
    m = re.search('\\{.*\\}', raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    ch = data.get('changed') if isinstance(data.get('changed'), dict) else {}
    changed = _absorb_model(fields, ch)
    changed = _keep_unmentioned(changed, fields, transcript)
    changed = _split_medications(changed, fields)
    changed = _with_plan(changed, fields)
    (line, _src) = meet_again_line(visit_day or str(fields.get('dos') or ''), next_visit_day, transcript, next_minutes=next_visit_minutes)
    if line and 'return_visit' in fields and (line.strip() != str(fields.get('return_visit') or '').strip()):
        changed['return_visit'] = line
    try:
        from app.notes.note_prefs import apply_known, save_snapshot
        merged = {k: str(v) for (k, v) in fields.items() if k in allowed}
        merged.update(changed)
        applied = apply_known(merged, transcript)
        for (k, v) in applied.items():
            if k in allowed and str(v).strip() != str(fields.get(k, '')).strip():
                changed[k] = v
        if client_id or event_id:
            snap = dict(merged)
            snap.update(changed)
            save_snapshot(client_id, event_id, snap, dos=str(fields.get('dos') or '')[:10])
    except Exception:
        pass
    return changed
