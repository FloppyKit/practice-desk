"""Tablet shell — GO-DESK-MOBILE. No PHI, no live clinic."""
from __future__ import annotations
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / 'static' / 'desk.html'
LAYOUT = ROOT / 'static' / 'js' / 'desk-layout.js'
CALL = ROOT / 'static' / 'js' / 'desk-call.js'

def test_layout_module_exists() -> None:
    src = LAYOUT.read_text(encoding='utf-8')
    assert 'deskLayout' in src
    assert 'notes' in src and 'office' in src
    r = subprocess.run(['node', '--check', str(LAYOUT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout

def test_ipad_defaults_notes_tabs7_defaults_office() -> None:
    script = '\nconst path = require("path");\nconst layout = require(path.resolve("static/js/desk-layout.js"));\nconst ipad = layout.detect(\n  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1", platform: "iPad", maxTouchPoints: 5 },\n  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },\n  "",\n  ""\n);\nif (ipad !== "notes") throw new Error("ipad=" + ipad);\nconst ipados = layout.detect(\n  { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15", platform: "MacIntel", maxTouchPoints: 5 },\n  { innerWidth: 1024, innerHeight: 1366, screen: { width: 1024, height: 1366 } },\n  "",\n  ""\n);\nif (ipados !== "notes") throw new Error("ipados=" + ipados);\nconst tab = layout.detect(\n  { userAgent: "Mozilla/5.0 (Linux; Android 11; SM-T870) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", platform: "Linux armv8l", maxTouchPoints: 5 },\n  { innerWidth: 800, innerHeight: 1280, screen: { width: 800, height: 1280 } },\n  "",\n  ""\n);\nif (tab !== "office") throw new Error("tab=" + tab);\nconst stored = layout.detect(\n  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X)", platform: "iPad", maxTouchPoints: 5 },\n  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },\n  "office",\n  ""\n);\nif (stored !== "office") throw new Error("stored=" + stored);\nconst q = layout.detect(\n  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X)", platform: "iPad", maxTouchPoints: 5 },\n  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },\n  "office",\n  "?layout=notes"\n);\nif (q !== "notes") throw new Error("query=" + q);\nconst desk = layout.detect(\n  { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120", platform: "MacIntel", maxTouchPoints: 0 },\n  { innerWidth: 1440, innerHeight: 900, screen: { width: 1440, height: 900 }, matchMedia: function () { return { matches: false }; } },\n  "",\n  ""\n);\nif (desk !== "desk") throw new Error("desk=" + desk);\n'
    r = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout or 'node layout detect failed'

def test_file_browser_names_the_chart_backend() -> None:
    html = DESK.read_text(encoding='utf-8')
    assert 'function chartStoreName' in html
    assert 'window.__chartBackend = meta.chart_backend' in html
    assert 'id="files-pop-kicker"' in html
    assert '/static/js/chart-drive.js' in html
    assert 'proton-drive.js' not in html
    js = (ROOT / 'static' / 'js' / 'chart-drive.js').read_text(encoding='utf-8')
    assert 'function storeName' in js
    assert 'Proton Drive' not in js
    sw = (ROOT / 'static' / 'js' / 'desk-sw.js').read_text(encoding='utf-8')
    assert '/static/js/chart-drive.js' in sw
    assert 'proton-drive.js' not in sw

def test_desk_html_tablet_shell() -> None:
    html = DESK.read_text(encoding='utf-8')
    for needle in ('id="layout-notes"', 'id="layout-office"', 'id="banner-unlock"', 'id="this-week"', 'id="office-placeholder"', 'id="office-join-placeholder"', 'desk-layout.js', 'desk-layout-notes', 'desk-layout-office', 'min-width:44px', 'html.desk-touch #this-week', 'desk-icon-label', 'Join from this tablet', 'Notes stay on the other device', 'viewport-fit=cover'):
        assert needle in html, f'missing {needle}'
    assert 'id="open-calendar"' in html and 'aria-label="Calendar"' in html and ('title="Calendar"' in html)
    assert 'deskFetch' in html
    assert 'const secret = () => ""' in html
    assert 'openUnlock()' in html

def test_desk_call_secret_not_on_query() -> None:
    src = CALL.read_text(encoding='utf-8')
    assert '?secret=' not in src
    assert '&secret=' not in src
    assert ' + "secret="' not in src
    assert 'X-Staff-Secret' in src
    assert 'deskFetch' in src

def test_notes_layout_hides_office_pane() -> None:
    html = DESK.read_text(encoding='utf-8')
    assert 'html.desk-layout-notes #video-card' in html
    assert 'html.desk-layout-office #note-pop' in html
    assert 'layoutMode === "notes") return false' in html or "layoutMode === 'notes') return false" in html

def test_no_demo_live_host_in_placeholder_path() -> None:
    html = DESK.read_text(encoding='utf-8')
    assert 'function officeUsesPlaceholder' in html
    assert 'function joinOfficePlaceholder' in html
    assert 'if (!officeLive && !officeUsesPlaceholder()) startOffice();' in html
    assert 'Office is off in demo' in html
    assert 'getAttribute("data-demo") === "1"' in html
    assert '<script src="https://live.example.com/static/js/host-office.js' not in html

def test_demo_desk_page_paints_data_demo_and_skips_live_script() -> None:
    main = (ROOT / 'app' / 'runtime.py').read_text(encoding='utf-8')
    desk_src = (ROOT / 'app' / 'routers' / 'desk.py').read_text(encoding='utf-8')
    assert 'def _demo_on' in main
    assert 'data-demo="1"' in desk_src
    assert 'data-stripped-demo-host-office' not in desk_src
    html = DESK.read_text(encoding='utf-8')
    assert 'Office is off in demo' in html
    assert '<script src="https://live.example.com/static/js/host-office.js' not in html

def test_note_sheet_stays_below_banner():
    """Dogfood: tall note/superbill must stay below banner so Close stays reachable."""
    html = Path('static/desk.html').read_text(encoding='utf-8')
    assert 'Keep sheet fully on-screen' in html
    assert 'Always below banner so Close stays reachable' in html
    assert 'el.style.top = pad + "px"' not in html
    assert 'position: sticky' in html
    assert 'function fitSheet' in html

def test_books_always_overlays_calendar():
    """Dogfood: Books must be is-overlay whenever open — never an in-flow tile vs week grid."""
    html = Path('static/desk.html').read_text(encoding='utf-8')
    assert 'books.classList.toggle("is-overlay", !!showB)' in html
    assert 'Drawers always overlay the calendar' in html
    assert '#books-pop.is-overlay { z-index:73; }' in html or 'z-index:73' in html
