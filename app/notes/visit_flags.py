"""Staff flags on a calendar visit. Not a charge and not a chart note.

Stored next to the phone book. No names in the row.
"""
from __future__ import annotations

from datetime import datetime, timezone

import app.infra.clients_db as clients_db

FLAGS = ("", "no_show", "late_cancel")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS visit_flags (
          event_id TEXT PRIMARY KEY,
          flag TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )


def set_flag(event_id: str, flag: str) -> str:
    eid = (event_id or "").strip()
    if not eid:
        raise ValueError("need a visit")
    flag = (flag or "").strip()
    if flag not in FLAGS:
        raise ValueError("flag must be no-show, late cancel, or clear")
    with clients_db.connect() as con:
        _ensure(con)
        if not flag:
            con.execute("DELETE FROM visit_flags WHERE event_id = ?", (eid,))
        else:
            con.execute(
                "INSERT INTO visit_flags (event_id, flag, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET flag = excluded.flag, updated_at = excluded.updated_at",
                (eid, flag, _now()),
            )
        con.commit()
    return flag


def for_events(event_ids: list[str]) -> dict[str, str]:
    ids = [e for e in event_ids if e]
    if not ids:
        return {}
    with clients_db.connect() as con:
        _ensure(con)
        q = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT event_id, flag FROM visit_flags WHERE event_id IN ({q})",
            ids,
        ).fetchall()
        con.commit()
    return {str(r["event_id"]): str(r["flag"]) for r in rows if r["flag"]}
