"""OSS defaults must not ship live clinic letterhead."""
from __future__ import annotations
from app.infra import practice
_BLANK_FIELDS = ('legal_name', 'clinician_name', 'clinician_title', 'address', 'phone', 'fax', 'email', 'website', 'meet', 'ein', 'npi')
_FORBIDDEN_FRAGMENTS = ('1881910081', '036.132941', 'TW-00062', '86-1977816', '773-312', 'drmattbrown', 'psycharts', 'matt@', 'meet.drmattbrown')

def test_practice_defaults_are_blank() -> None:
    assert practice.DEFAULTS['display_name'] == 'Example Practice'
    for key in _BLANK_FIELDS:
        assert practice.DEFAULTS[key] == '', f'{key} should be blank in DEFAULTS'
    assert practice.DEFAULT_LICENSES == []
    blob = str(practice.DEFAULTS) + str(practice.DEFAULT_LICENSES)
    for frag in _FORBIDDEN_FRAGMENTS:
        assert frag not in blob, f'forbidden fragment in shipped defaults: {frag}'

def test_booking_page_uses_visit_fee_and_profile_name() -> None:
    import json
    import tempfile
    from pathlib import Path
    import app.runtime as runtime
    old = practice.ROOT
    tmp = Path(tempfile.mkdtemp(prefix='book-blurb-'))
    practice.ROOT = tmp
    try:
        blank = runtime._book_page('new-client').body.decode()
        assert 'Example Clinician' not in blank
        assert '$600' not in blank
        assert '$200' in blank
        assert 'the clinician' in blank
        assert 'America/Chicago' in blank
        (tmp / 'practice.json').write_text(json.dumps({'clinician_name': 'Ada Example', 'clinician_title': 'LCSW'}), encoding='utf-8')
        named = runtime._book_page('new-client').body.decode()
        assert 'Ada Example, LCSW' in named
        assert '$200' in named
        follow = runtime._book_page('follow-up').body.decode()
        assert 'Ada Example, LCSW' in follow
        assert '$100' in follow and '$160' in follow
        assert '$265' not in follow and '$420' not in follow
    finally:
        practice.ROOT = old

def test_readme_phone_names_every_call_setting() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    phone = root.joinpath('README.md').read_text(encoding='utf-8').split('## Phone', 1)[1]
    example = root.joinpath('.env.example').read_text(encoding='utf-8')
    names = (
        'TWILIO_ACCOUNT_SID',
        'TWILIO_AUTH_TOKEN',
        'TWILIO_PHONE_NUMBER',
        'TWILIO_VOICE_FROM',
        'TWILIO_API_KEY_SID',
        'TWILIO_API_KEY_SECRET',
        'TWILIO_DESK_TWIML_APP_SID',
    )
    for name in names:
        assert name in phone, name
        assert f'{name}=' in example, name
    assert 'when `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set' not in phone
