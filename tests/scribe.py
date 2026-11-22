"""Visit piece tracker for the scribe guard tests.

Generic only: no mailbox, no host path, no backend import.
"""
from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo

GUEST_GAP_SEC = 60


def _zone() -> ZoneInfo | None:
    name = (os.environ.get("PRACTICE_TIMEZONE") or "America/Chicago").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def ct_stamp(ts: float) -> str:
    zone = _zone()
    when = datetime.datetime.fromtimestamp(ts, zone) if zone else datetime.datetime.fromtimestamp(ts)
    return when.strftime("%-I:%M:%S %p CT")


def guest_key(identity: str) -> str:
    """Guest tokens are '<name-slug>-<6 hex>'; a reconnect gets new hex, same slug."""
    head, _, tail = identity.rpartition("-")
    if head and len(tail) == 6 and all(c in "0123456789abcdef" for c in tail):
        return head
    return identity


class PieceTracker:
    """Pure per-visit cut logic. tick() -> actions: open, cut, pause, resume."""

    def __init__(self, gap: float = GUEST_GAP_SEC) -> None:
        self.gap = gap
        self.open = False
        self.keys: set[str] = set()
        self.gone_since: float | None = None

    def tick(self, now: float, guests: set[str]) -> list[str]:
        acts: list[str] = []
        if guests:
            if self.open and self.gone_since is not None and not (guests & self.keys):
                acts.append("cut")
                self.open = False
            elif self.open and self.gone_since is None and not (guests & self.keys):
                acts.append("cut")
                self.open = False
            if not self.open:
                self.open, self.keys, self.gone_since = True, set(guests), None
                acts.append("open")
            else:
                if self.gone_since is not None:
                    acts.append("resume")
                self.gone_since = None
                self.keys |= guests
        elif self.open:
            if self.gone_since is None:
                self.gone_since = now
                acts.append("pause")
            elif now - self.gone_since >= self.gap:
                self.open, self.keys, self.gone_since = False, set(), None
                acts.append("cut")
        return acts
