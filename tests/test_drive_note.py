"""Drive note pick + PDF text round-trip. Synthetic. No network."""
from __future__ import annotations
from app.notes.drive_note import parse_note_text, pick_latest
from app.notes.note_carry import update_from_transcript
from app.notes.note_pdf import after_visit_pdf
from app.notes.pdf_text import pdf_text

def test_pick_latest_by_date_and_skips_chart_copy_on_a_tie() -> None:
    items = [{'name': '2026-01-01 AB avs.pdf', 'type': 'file'}, {'name': '2026-06-02 AB avs.pdf', 'type': 'file'}, {'name': '2026-06-02 AB avs chart.pdf', 'type': 'file'}, {'name': 'Consent.pdf', 'type': 'file'}, {'name': 'Labs', 'type': 'folder'}]
    assert pick_latest(items)['name'] == '2026-06-02 AB avs.pdf'

def test_pdf_round_trip_keeps_bullets() -> None:
    pdf = after_visit_pdf({'dos': '2026-06-02', 'return_visit': 'In 6 weeks', 'medications': '○ sertraline 50 mg daily', 'supplements': '○ magnesium glycinate', 'recommendations': '○ walk daily'}, title='After Visit Summary', letterhead={'name': 'Example Practice', 'clinician': 'Example Clinician, DO'}, patient=True)
    text = pdf_text(pdf)
    fields = parse_note_text(text)
    assert 'sertraline' in fields['medications']
    assert 'magnesium' in fields['supplements']
    assert 'walk' in fields['recommendations']
    assert '6 weeks' in fields['return_visit']
    assert 'Medications:' in fields['plan']

def test_update_keeps_a_line_the_session_did_not_mention() -> None:
    fields = {'medications': '○ sertraline 50 mg daily\n○ magnesium glycinate', 'plan': 'Medications:\n○ sertraline 50 mg daily\n○ magnesium glycinate'}
    fake = lambda _s, _t: '{"changed": {"medications": "○ sertraline 100 mg daily"}}'
    out = update_from_transcript(fields, 'We increased sertraline to 100 mg today. Nothing else.', call=fake)
    assert '100 mg' in out['medications']
    assert 'magnesium glycinate' in out['medications']
