"""Remember a signed edit: the model wrote X, the host changed it to Y.

Local only. Scrubbed (no name, DOB, phone, email). The file can still hold
clinical wording, so it lives under data/style-pack-learned and must not be
committed. Consulted on the next transcript update. Nothing here calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

_NAME_LINE = re.compile(r"(?im)^\s*(name|patient)\s*:.*$")
_DOB_LINE = re.compile(r"(?im)^\s*(date\s+of\s+birth|dob)\s*:.*$")
_PHONE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_MDY = re.compile(r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b")
_ISO = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")

SECTIONS = (
    "subjective",
    "objective",
    "assessment",
    "plan",
    "medications",
    "supplements",
    "recommendations",
    "return_visit",
)


def learned_dir() -> Path:
    raw = (os.environ.get("STYLE_PACK_LEARNED_DIR") or "").strip()
    if raw:
        p = Path(raw)
    elif Path("/data/logs").is_dir():
        # Droplet booker log volume. Survives a restart. Not the public repo.
        p = Path("/data/logs/style-pack-learned")
    else:
        p = Path(__file__).resolve().parents[2] / "data" / "style-pack-learned"
    p.mkdir(parents=True, exist_ok=True)
    return p


def scrub(text: str) -> str:
    """Drop identifiers. Keep clinical wording, including doses."""
    s = _NAME_LINE.sub("Name: [REDACTED]", text or "")
    s = _DOB_LINE.sub("DOB: [REDACTED]", s)
    s = _EMAIL.sub("[REDACTED]", s)
    s = _PHONE.sub("[REDACTED]", s)
    s = _MDY.sub("[date]", s)
    s = _ISO.sub("[date]", s)
    return s


def _key(client_id: str, event_id: str, dos: str = "") -> str:
    # Event id is the visit. Dos only when the note was opened outside a visit.
    raw = f"{client_id}|{event_id or dos}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _snap_path() -> Path:
    return learned_dir() / "draft_snapshots.json"


def _pairs_path() -> Path:
    return learned_dir() / "edit_pairs.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_snapshot(client_id: str, event_id: str, fields: dict[str, str], *, dos: str = "") -> None:
    """What the model (plus carry) put in the draft, before the host edited."""
    if not (client_id or event_id):
        return
    keep = {k: str(v or "")[:8000] for k, v in (fields or {}).items() if k in SECTIONS and str(v or "").strip()}
    if not keep:
        return
    with _LOCK:
        data = _read(_snap_path())
        data[_key(client_id, event_id, dos)] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fields": keep,
        }
        # last 40 drafts is enough; the file can hold clinical text
        if len(data) > 40:
            ordered = sorted(data.items(), key=lambda kv: str(kv[1].get("at") or ""))
            data = dict(ordered[-40:])
        _snap_path().write_text(json.dumps(data), encoding="utf-8")


def load_snapshot(client_id: str, event_id: str, *, dos: str = "") -> dict[str, str] | None:
    if not (client_id or event_id):
        return None
    row = _read(_snap_path()).get(_key(client_id, event_id, dos))
    if not isinstance(row, dict):
        return None
    fields = row.get("fields")
    return dict(fields) if isinstance(fields, dict) else None


def _lines(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s and not s.endswith(":"):
            out.append(s)
    return out


def _pairs_from(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for key in SECTIONS:
        old_lines = _lines(scrub(str(before.get(key) or "")))
        new_lines = _lines(scrub(str(after.get(key) or "")))
        if old_lines == new_lines:
            continue
        dropped = [ln for ln in old_lines if ln not in new_lines]
        added = [ln for ln in new_lines if ln not in old_lines]
        used: set[int] = set()
        for src in dropped:
            src_toks = set(re.findall(r"[a-z0-9]+", src.lower()))
            best_i, best = -1, 0
            for i, dst in enumerate(added):
                if i in used:
                    continue
                dst_toks = set(re.findall(r"[a-z0-9]+", dst.lower()))
                score = len(src_toks & dst_toks)
                if score > best and score >= 1:
                    best, best_i = score, i
            if best_i >= 0:
                used.add(best_i)
                if src != added[best_i] and "[REDACTED]" not in src and "[REDACTED]" not in added[best_i]:
                    found.append({"section": key, "before": src[:500], "after": added[best_i][:500], "kind": "replace"})
            elif "[REDACTED]" not in src:
                found.append({"section": key, "before": src[:500], "after": "", "kind": "drop"})
        for i, dst in enumerate(added):
            if i not in used and "[REDACTED]" not in dst:
                found.append({"section": key, "before": "", "after": dst[:500], "kind": "add"})
    return found


def remember_signed(client_id: str, event_id: str, signed: dict[str, Any], *, dos: str = "") -> dict[str, Any]:
    """Compare the last generated draft to what was signed. No-op without a snapshot."""
    before = load_snapshot(client_id, event_id, dos=dos)
    if not before:
        return {"ok": True, "skipped": "no_snapshot"}
    fresh = _pairs_from(before, {k: str(v or "") for k, v in (signed or {}).items()})
    if not fresh:
        return {"ok": True, "pairs": 0}
    with _LOCK:
        data = _read(_pairs_path())
        items = list(data.get("pairs") or [])
        for pair in fresh:
            hit = False
            for it in items:
                if (
                    it.get("section") == pair["section"]
                    and it.get("before") == pair["before"]
                    and it.get("after") == pair["after"]
                    and it.get("kind") == pair["kind"]
                ):
                    it["count"] = int(it.get("count") or 1) + 1
                    hit = True
                    break
            if not hit:
                items.append({**pair, "count": 1})
        items = items[-200:]
        data["pairs"] = items
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _pairs_path().write_text(json.dumps(data), encoding="utf-8")
    return {"ok": True, "pairs": len(fresh)}


def pairs() -> list[dict[str, Any]]:
    data = _read(_pairs_path())
    raw = data.get("pairs") or []
    return [p for p in raw if isinstance(p, dict)]


def prompt_block(max_chars: int = 1200) -> str:
    """Wording he has corrected, for the Bedrock update prompt. No identifiers."""
    rows = [p for p in pairs() if p.get("kind") == "replace" and p.get("before") and p.get("after")]
    rows.sort(key=lambda p: -int(p.get("count") or 1))
    lines = []
    for p in rows[:12]:
        lines.append(
            f"- In {p.get('section')}, he changes \"{p['before']}\" to \"{p['after']}\" "
            f"(seen {int(p.get('count') or 1)}). Do that unless the transcript contradicts it."
        )
    if not lines:
        return ""
    text = "CLINICIAN EDITS (local; apply the same kind of change):\n" + "\n".join(lines)
    return text[:max_chars]


def apply_known(changed: dict[str, str], transcript: str) -> dict[str, str]:
    """Deterministic pass: a repeated line edit wins when this transcript did not say the old line."""
    spoken = (transcript or "").lower()
    out = dict(changed)
    for p in pairs():
        if p.get("kind") != "replace":
            continue
        section = str(p.get("section") or "")
        before = str(p.get("before") or "")
        after = str(p.get("after") or "")
        if not section or not before or not after or section not in out:
            continue
        bare = re.sub(r"^[\s○•*\-]+", "", before).strip().lower()
        if before.lower() in spoken or (bare and bare in spoken):
            continue
        text = out[section]
        if before not in text:
            continue
        out[section] = text.replace(before, after)
    return out
