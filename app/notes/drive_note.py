"""Latest Visit Notes file on Proton Drive → template fields. Bodies are not logged."""
from __future__ import annotations

import re
from typing import Any

from app.notes.note_carry import plan_from_avs
from app.notes.pdf_text import pdf_text

_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_MDY = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](20\d{2})(?!\d)")


def name_dos(name: str) -> str:
    """Date of service in a Visit Notes filename. ISO, or M/D/YYYY with . / or -."""
    m = _DATE.search(name or "")
    if m:
        return m.group(1)
    m = _MDY.search(name or "")
    if not m:
        return ""
    mo, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= dd <= 31):
        return ""
    return f"{yy:04d}-{mo:02d}-{dd:02d}"
_LABELS = {
    "meet again": "return_visit",
    "return visit": "return_visit",
    "medications": "medications",
    "medication": "medications",
    "supplements": "supplements",
    "supplement": "supplements",
    "other recommendations": "recommendations",
    "recommendations": "recommendations",
    "subjective": "subjective",
    "objective": "objective",
    "assessment": "assessment",
    "plan": "plan",
    "interval update": "interval_update",
}
_SKIP_LINE = re.compile(r"(?i)^(tel:|fax:|npi |licenses:)")


def pick_latest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most recent note file in a Visit Notes listing. Date in the name wins; chart copies lose a tie."""
    files = []
    for it in items or []:
        if (it.get("type") or "file") == "folder":
            continue
        name = str(it.get("name") or "")
        if not name or name.startswith("."):
            continue
        low = name.lower()
        if not low.endswith((".pdf", ".md", ".txt")) and not name_dos(name):
            continue
        files.append((name_dos(name) or "0000-00-00", 0 if "chart" in low else 1, name, it))
    if not files:
        return None
    files.sort(reverse=True)
    return files[0][3]


def parse_note_text(text: str) -> dict[str, str]:
    """Headings from a Drive note (our PDF text, or a chart markdown AVS) → field ids."""
    fields: dict[str, list[str]] = {}
    cur = ""
    for raw in (text or "").replace("\r", "").splitlines():
        s = raw.strip().lstrip("\ufeff")
        if not s or s.lower() in {"&nbsp;", "&amp;nbsp;"}:
            if cur:
                fields[cur].append("")
            continue
        label = re.sub(r"\*+", "", s).strip().rstrip(":").lower()
        key = _LABELS.get(label)
        inline = re.match(r"^(.{2,40}):\s+(\S.*)$", s)
        if key and not inline:
            cur = key
            fields.setdefault(cur, [])
            continue
        if inline and _LABELS.get(inline.group(1).strip().lower()):
            cur = _LABELS[inline.group(1).strip().lower()]
            fields.setdefault(cur, [])
            fields[cur].append(inline.group(2).strip())
            continue
        if cur and not _SKIP_LINE.match(s) and not s.startswith("![]("):
            fields[cur].append(s)
    out = {}
    for key, buf in fields.items():
        while buf and not buf[-1].strip():
            buf.pop()
        body = "\n".join(buf).strip()
        if body:
            out[key] = body[:8000]
    plan = plan_from_avs(out)
    if plan and not out.get("plan"):
        out["plan"] = plan
    return out


def text_of(name: str, blob: bytes) -> str:
    low = (name or "").lower()
    if low.endswith(".pdf") or blob[:5] == b"%PDF-":
        return pdf_text(blob)
    return blob.decode("utf-8", "replace")
