from __future__ import annotations

import app.runtime as runtime
from fastapi import APIRouter

router = APIRouter()

def page_book_slug(slug: str):
    """Public book page for any enabled service."""
    from app.booking.event_store import RESERVED
    if '.' in slug or slug in RESERVED:
        raise runtime.HTTPException(404)
    et = (runtime.load_config().get('event_types') or {}).get(slug)
    if not isinstance(et, dict) or et.get('enabled') is False or et.get('public') is False:
        raise runtime.HTTPException(404)
    return runtime._book_page(slug)

router.add_api_route('/{slug}', page_book_slug, methods=['GET'])
