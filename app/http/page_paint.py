"""Pages served to the browser. Routes call these as runtime.<name>."""
from __future__ import annotations

import json
import os

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

import app.runtime as runtime
from app.booking.practice_links import (
    book_host,
    book_url,
    live_origin,
    portal_origin,
    short_host,
    site_url,
    video_host,
    video_url,
    www_origin,
)
from app.infra.config import get_event_type, load_config, timezone_name
from app.messaging.email_templates import HOST_EMAIL, HOST_PHONE, HOST_SHORT

def _demo_on() -> bool:
    return (os.environ.get('DEMO') or '').strip().lower() in ('1', 'true', 'yes', 'on')

def _paint_page(html: str) -> str:
    """Fill the example identity from config when a page is served."""
    tz = timezone_name()
    if 'data-tz=' not in html:
        html = html.replace('<html', '<html data-tz="' + tz + '"', 1)
    pairs = (('https://book.' + 'example.com', book_url()), ('https://meet.' + 'example.com', video_url()), ('https://live.' + 'example.com', live_origin()), ('https://portal.' + 'example.com', portal_origin()), ('https://www.' + 'example.com', www_origin()), ('https://' + 'example.com', site_url()), ('go.' + 'example.com', short_host()), ('book.' + 'example.com', book_host()), ('meet.' + 'example.com', video_host()), ('desk@' + 'example.com', HOST_EMAIL), ('555-' + '010-0100', HOST_PHONE), ('the ' + 'clinician', HOST_SHORT), ('America/' + 'Chicago', tz))
    for (old, new) in pairs:
        if old != new and old in html:
            html = html.replace(old, new)
    return html

def _page(name: str, cache: bool=True):
    path = runtime.STATIC / name
    if not path.exists():
        raise HTTPException(404)
    headers = {} if cache else {'Cache-Control': 'no-store'}
    if path.suffix == '.html':
        return Response(content=runtime._paint_page(path.read_text(encoding='utf-8')), media_type='text/html; charset=utf-8', headers=headers)
    return FileResponse(path, headers=headers)

_BOOK_DESCRIPTIONS = {'new-client': 'Book a new-patient video evaluation (about 90–100 minutes, $600) with Example Clinician, DO, MBA. Times are shown in Central Time.', 'follow-up': 'Book a follow-up video visit with Example Clinician, DO, MBA (current patients). Times are shown in Central Time.'}

def _book_page(slug: str) -> HTMLResponse:
    """book.html with the visit title, meta description and JSON-LD rendered on the server (JS still hydrates the rest)."""
    import html as _html
    path = runtime.STATIC / 'book.html'
    if not path.exists():
        raise HTTPException(404)
    raw = path.read_text(encoding='utf-8')
    et = get_event_type(load_config(), slug) if slug else None
    title = str((et or {}).get('title') or 'Book a visit').strip() or 'Book a visit'
    desc = _BOOK_DESCRIPTIONS.get(slug, f'Book a {title.lower()} with {HOST_SHORT} by secure video. Times are shown in Central Time.')
    page_url = f'{book_url()}/{slug}' if slug else book_url() + '/'
    ld = {'@context': 'https://schema.org', '@type': 'WebPage', '@id': page_url + '#webpage', 'url': page_url, 'name': f'{title} · {HOST_SHORT}', 'description': desc, 'inLanguage': 'en-US', 'isPartOf': {'@id': site_url() + '/#website'}, 'about': {'@id': site_url() + '/#practice'}, 'provider': {'@type': ['MedicalBusiness', 'MedicalOrganization'], '@id': site_url() + '/#practice', 'name': HOST_SHORT, 'url': site_url(), 'telephone': '+1-' + HOST_PHONE}, 'potentialAction': {'@type': 'ReserveAction', 'target': page_url}}
    ld_json = json.dumps(ld).replace('<', '\\u003c')
    head_extra = f'  <meta name="description" content="{_html.escape(desc)}" />\n  <link rel="canonical" href="{_html.escape(page_url)}" />\n  <script type="application/ld+json">{ld_json}</script>\n'
    out = raw.replace('<title>Book · ' + HOST_SHORT + '</title>', f'<title>{_html.escape(title)} · {HOST_SHORT}</title>', 1)
    out = out.replace('<h1 id="book-title">Book a visit</h1>', f'<h1 id="book-title">{_html.escape(title)}</h1>', 1)
    out = out.replace('</head>', head_extra + '</head>', 1)
    if slug == 'follow-up':
        start = out.find('<label class="check">')
        end = out.find('</label>', start)
        if start != -1 and end != -1 and ('name="sms_consent"' in out[start:end]):
            out = out[:start] + out[end + len('</label>'):]
        out = out.replace('placeholder="Optional — needed for SMS"', 'placeholder="Optional"', 1)
    return HTMLResponse(runtime._paint_page(out), headers={'Cache-Control': 'no-store'})

_NO_INDEX = {'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer'}

_C_PAGE = "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=robots content='noindex,nofollow'><meta name=referrer content=no-referrer><meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title></head><body style='font-family:system-ui;max-width:32em;margin:3em auto;padding:0 1em;line-height:1.5'><h1 style='font-size:1.3em'>{title}</h1>{body}</body></html>"

def _c_expired() -> HTMLResponse:
    return HTMLResponse(_C_PAGE.format(title='Log in to read your messages', body=f"<p>This link has expired or was replaced. Your messages are safe.</p><p><a href='{runtime.SITE_PUBLIC}/portal' style='font-size:1.1em'>Log in with a code</a> to read them.</p>"), status_code=410, headers=_NO_INDEX)

def _c_ip(request: Request | None) -> str:
    if request is None:
        return ''
    return request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else '')
