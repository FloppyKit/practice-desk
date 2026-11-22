"""One-shot: copy Supabase clients → local SQLite. Prints counts only. No names."""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Allow running from repo or /app
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infra.clients_db import COLUMNS, count, upsert  # noqa: E402


def _base() -> str:
    return (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).rstrip("/")


def _key() -> str:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""


def fetch_page(offset: int, limit: int = 200) -> list[dict]:
    select = ",".join(COLUMNS)
    q = (
        f"/rest/v1/clients?select={select}&order=id.asc"
        f"&limit={limit}&offset={offset}"
    )
    req = urllib.request.Request(
        _base() + q,
        headers={
            "apikey": _key(),
            "Authorization": "Bearer " + _key(),
            "Accept": "application/json",
            "Prefer": "count=exact",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode() or "[]")


def main() -> None:
    if not _base() or not _key():
        raise SystemExit("need SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    offset = 0
    pulled = 0
    wrote = 0
    while True:
        rows = fetch_page(offset)
        if not rows:
            break
        pulled += len(rows)
        for row in rows:
            payload = {k: row.get(k) for k in COLUMNS if row.get(k) is not None}
            if not payload.get("id"):
                continue
            upsert(payload)
            wrote += 1
        if len(rows) < 200:
            break
        offset += 200
    print(f"pulled={pulled} wrote={wrote} sqlite={count()}")


if __name__ == "__main__":
    main()
