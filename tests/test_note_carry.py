"""Note carry-forward + transcript update. Synthetic; no network."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
from app.infra import clients_db
from app.notes import note_carry
AVS = '## **After Visit Summary:**\n\n**Medications:**\n○ sertraline 50 mg daily\n\n**Supplements:**\n○ magnesium glycinate\n\n**Other recommendations:**\n○ walk daily\n\n## Return Visit:\nIn 6 weeks\n'

def test_parse_chart_avs_keeps_bullets_and_builds_plan() -> None:
    fields = note_carry.parse_chart_avs(AVS)
    assert fields['medications'].startswith('○ sertraline')
    assert 'magnesium' in fields['supplements']
    assert fields['return_visit'] == 'In 6 weeks'
    plan = note_carry.plan_from_avs(fields)
    assert plan.startswith('Medications:\n○ sertraline')
    assert 'Meet again:\nIn 6 weeks' in plan

def test_carry_uses_last_dated_visit_form_not_a_later_consent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clients_db.DB_PATH = Path(tmp) / 'c.sqlite'
        with clients_db.connect() as con:
            con.execute('CREATE TABLE signed_notes (id TEXT, client_id TEXT, dos TEXT, template TEXT, fields_json TEXT, avs_path TEXT, avs_name TEXT, chart_path TEXT, signed_at TEXT)')
            con.execute('CREATE TABLE imported_notes (id TEXT, client_id TEXT, dos TEXT, source_file TEXT, content TEXT)')
            con.execute('INSERT INTO imported_notes VALUES (?,?,?,?,?)', ('i1', 'c1', '2026-01-01', 'Person/After Visit Summary - 2026-01-01.md', AVS))
            con.execute('INSERT INTO imported_notes VALUES (?,?,?,?,?)', ('i2', 'c1', '2026-08-01', 'Person/Consent for Telehealth Consultation - 2026-08-01.md', 'not a visit'))
        got = note_carry.carry('c1')
        assert got['source'] == 'imported' and got['dos'] == '2026-01-01'
        assert got['fields']['medications'].startswith('○ sertraline')
        assert 'Medications:' in got['fields']['plan']
        assert 'subjective' not in got['fields']
        with clients_db.connect() as con:
            con.execute("INSERT INTO signed_notes VALUES ('n1','c1','2026-09-01','avs',?,'','','','2026-09-01T10:00:00')", (json.dumps({'dos': '2026-09-01', 'subjective': 'skip me', 'medications': '○ synthetic med 1', 'plan': 'old prose'}),))
        signed = note_carry.carry('c1')
        assert signed['source'] == 'signed' and signed['dos'] == '2026-09-01'
        assert signed['fields']['medications'] == '○ synthetic med 1'
        assert signed['fields']['plan'].startswith('Medications:\n○ synthetic med 1')
        assert 'subjective' not in signed['fields']
        assert note_carry.carry('nobody') is None

def test_newer_imported_visit_beats_older_signed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clients_db.DB_PATH = Path(tmp) / 'c.sqlite'
        with clients_db.connect() as con:
            con.execute('CREATE TABLE signed_notes (id TEXT, client_id TEXT, dos TEXT, template TEXT, fields_json TEXT, avs_path TEXT, avs_name TEXT, chart_path TEXT, signed_at TEXT)')
            con.execute('CREATE TABLE imported_notes (id TEXT, client_id TEXT, dos TEXT, source_file TEXT, content TEXT)')
            con.execute("INSERT INTO signed_notes VALUES ('n1','c1','2026-01-01','avs',?,'','','','2026-01-01T10:00:00')", (json.dumps({'medications': '○ old med'}),))
            con.execute('INSERT INTO imported_notes VALUES (?,?,?,?,?)', ('i1', 'c1', '2026-06-01', 'Person/After Visit Summary - 2026-06-01.md', AVS))
        got = note_carry.carry('c1')
        assert got['source'] == 'imported' and 'sertraline' in got['fields']['plan']

def test_update_keeps_unchanged_and_refreshes_plan_bullets() -> None:
    fake = lambda _s, _t: json.dumps({'changed': {'medications': '○ sertraline 100 mg daily', 'supplements': '○ magnesium glycinate', 'dos': '2020-01-01', 'made_up': 'z'}})
    fields = {'plan': 'Medications:\n○ sertraline 50 mg daily\n\nSupplements:\n○ magnesium glycinate', 'medications': '○ sertraline 50 mg daily', 'supplements': '○ magnesium glycinate', 'dos': '2026-10-30'}
    out = note_carry.update_from_transcript(fields, 'We raised sertraline today. Synthetic transcript.', call=fake)
    assert out['medications'] == '○ sertraline 100 mg daily'
    assert 'supplements' not in out
    assert '○ sertraline 100 mg daily' in out['plan']
    assert '○ magnesium glycinate' in out['plan']
    assert 'dos' not in out
