"""Tiny After-Visit Summary PDF. No extra deps. No PHI in logs."""
from __future__ import annotations

import re


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


_AMAZON = re.compile(
    r"https?://(?:www\.)?amazon\.com/"
    r"([A-Za-z0-9][A-Za-z0-9-]*)/dp/([A-Za-z0-9]{10})/?",
    re.I,
)


def stitch_amazon(text: str) -> str:
    """Put the item name back on the amazon.com line. The old note broke the link where X belongs."""
    text = re.sub(r"(https?://(?:www\.)?amazon\.com/)\s+", r"\1", text or "", flags=re.I)

    def join_asin(m: re.Match[str]) -> str:
        both = m.group(1) + m.group(2)
        return "/dp/" + both[:10] + "/" + both[10:]

    text = re.sub(r"/dp/([A-Za-z0-9]{1,9})\s+([A-Za-z0-9]+)", join_asin, text, flags=re.I)

    def pad_asin(m: re.Match[str]) -> str:
        have, more = m.group(1), m.group(2)
        need = 10 - len(have)
        if need <= 0:
            return m.group(0)
        return "/dp/" + have + more[:need] + "/" + more[need:]

    return re.sub(r"/dp/([A-Za-z0-9]{1,9})[\s/]+([A-Za-z0-9].*)", pad_asin, text)


def _restore_amazon_tail(extra: str) -> str:
    """Anything after Y/ is the next line, not part of the link."""
    extra = (extra or "").lstrip("/").lstrip()
    if extra[:3].upper() == "ORY" and extra[3:].lower().startswith("ogi"):
        return "OR\nYogi" + extra[6:]
    return extra


def clip_amazon_urls(text: str) -> str:
    """An Amazon link is exactly https://www.amazon.com/X/dp/Y/ . Nothing after that slash."""
    text = stitch_amazon(text)

    def split_one(m: re.Match[str]) -> str:
        token = m.group(0)
        mm = re.match(
            r"(https?://(?:www\.)?amazon\.com/)([A-Za-z0-9][A-Za-z0-9-]*)/dp/([A-Za-z0-9]{10})/?(.*)",
            token,
            re.I,
        )
        if not mm:
            return token
        canon = f"{mm.group(1)}{mm.group(2)}/dp/{mm.group(3)}/"
        extra = _restore_amazon_tail(mm.group(4))
        return canon + ("\n" + extra if extra else "")

    return re.sub(
        r"https?://(?:www\.)?amazon\.com/[A-Za-z0-9][A-Za-z0-9-]*/dp/[A-Za-z0-9]{10}/?\S*",
        split_one,
        text,
        flags=re.I,
    )


def _wrap_url(url: str, width: int) -> list[str]:
    """Break a long link only at a slash or punctuation, never mid-token, and add no spaces."""
    if "amazon.com/" in url.lower() and "/dp/" in url.lower():
        return [url]
    if len(url) <= width:
        return [url]
    out: list[str] = []
    rest = url
    while len(rest) > width:
        window = rest[:width]
        cut = max(window.rfind(c) for c in "/?&=-_")
        if cut < 8:
            cut = width
        out.append(rest[:cut + 1] if cut < width and rest[cut] in "/?&=-_" else rest[:cut])
        rest = rest[len(out[-1]):]
    if rest:
        out.append(rest)
    return out


def _split_link_line(s: str) -> list[tuple[str, bool]]:
    """Pull each URL out so the words after it are not drawn as part of the link."""
    parts: list[tuple[str, bool]] = []
    pos = 0
    for m in re.finditer(r"https?://[^\s]+", s, re.I):
        if m.start() > pos and s[pos:m.start()].strip():
            parts.append((s[pos:m.start()].strip(), False))
        url = m.group(0).rstrip(".,;:)")
        clipped = clip_amazon_urls(url)
        if "\n" in clipped:
            url, extra = clipped.split("\n", 1)
            tail = extra + m.group(0)[len(m.group(0).rstrip(".,;:)")):]
        else:
            url = clipped
            tail = m.group(0)[len(url):]
        parts.append((url, True))
        pos = m.end()
        if tail:
            parts.append((tail, False))
    if pos < len(s) and s[pos:].strip():
        parts.append((s[pos:].strip(), False))
    return parts or [(s, False)]


def _wrap(text: str, width: int = 86) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        raw = raw.strip()
        if not raw:
            lines.append("")
            continue
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            if cut < 20:
                cut = width
            lines.append(raw[:cut].strip())
            raw = raw[cut:].strip()
        if raw:
            lines.append(raw)
    return lines or [""]


# Standard Helvetica / Helvetica-Bold advance widths (AFM, WinAnsi, no kerning) for ASCII 32..126.
# The AVS header aligns a comma in one row with a space in another, so "looks about right" is not enough:
# the host 2026-11-17 asked for an exact vertical axis.
_HELV = tuple(int(n) for n in """
278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556
1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778
667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556
333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556
556 556 333 500 278 556 500 722 500 500 500 334 260 334 584
""".split())
_HELV_BOLD = tuple(int(n) for n in """
278 333 474 556 556 889 722 278 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611
975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778
667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556
333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611
611 611 389 556 333 611 556 778 556 556 500 389 280 389 584
""".split())
_BULLET_W = {"\u2022": 350, "\u25e6": 350, "\u25aa": 350, "\u2219": 350, "\u2013": 556, "\u2014": 1000}


def helvetica_width(text: str, size: float, bold: bool = False) -> float:
    """Advance width of Helvetica (or Helvetica-Bold) text at `size`, in points."""
    table = _HELV_BOLD if bold else _HELV
    total = 0
    for ch in text or "":
        o = ord(ch)
        if 32 <= o <= 126:
            total += table[o - 32]
        else:
            total += _BULLET_W.get(ch, 556)
    return total * size / 1000.0


MEET_WITH_LENGTH = re.compile(r"^(.*?)\s*[-\u2013]\s*(\d{1,3})\s*min\.?$", re.I)
MEET_DAY_OPEN = re.compile(r"^([A-Za-z]+\.?\s+\d{1,2})(,)(.*)$")


def meet_rows(value: str) -> list[tuple[str, str, str]]:
    """The Meet again column as rows of (left, separator, right) that share one vertical axis.

    The space in "Meet again", the comma after the day, and the space in
    "30 Min" must sit on the same x. "December 8, 2026, 11:00 AM - 30 min" becomes
    ("Meet", " ", "again:") / ("December 8", ",", " 2026, 11:00 AM") / ("30", " ", "Min").
    Row 0 is the bold label; every other row is body text.
    """
    raw = (value or "").strip() or "Not booked"
    when, mins = raw, ""
    m = MEET_WITH_LENGTH.match(raw)
    if m and m.group(1).strip():
        when, mins = m.group(1).strip(), m.group(2)
    rows: list[tuple[str, str, str]] = [("Meet", " ", "again:")]
    day = MEET_DAY_OPEN.match(when)
    rows.append((day.group(1), day.group(2), day.group(3)) if day else (when, "", ""))
    if mins:
        rows.append((mins, " ", "Min"))
    return rows


# Patient-facing AVS order (January 2025 letterhead, Meet again first). S/O/A never go on it.
AVS_PATIENT_ORDER: tuple[tuple[str, str], ...] = (
    ("medications", "Medications"),
    ("supplements", "Supplements"),
    ("recommendations", "Other recommendations"),
)
CHART_ONLY = ("subjective", "objective", "assessment", "plan", "interval_update", "data", "long_run", "addendum")


def _pretty_dos(raw: str) -> str:
    from datetime import datetime

    s = (raw or "").strip()[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return s
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _enc(s: str) -> bytes:
    """Helvetica WinAnsi has a bullet at 0x95. A circle bullet is not in that font, so it was printing as ?."""
    s = s or ""
    for ch in ("○", "●", "◦", "▪", "∙"):
        s = s.replace(ch, "•")
    return _esc(s).encode("cp1252", "replace")


def _ends_with_url(s: str) -> bool:
    return bool(re.search(r"https?://\S+$", (s or "").strip(), re.I))


def _url_continuation(buf: str, nxt: str) -> bool:
    """Old notes split a link after the scheme, or before a dot or slash. A letter after the last slash is the next sentence."""
    if not nxt or " " in nxt or not _ends_with_url(buf):
        return False
    if nxt[0] in "./ ?&=_%-":
        return nxt[0] != " "
    host = buf.strip().split("://", 1)[-1].split("/", 1)[0]
    if "." not in host and nxt[0].islower():
        return True
    return False


def repair_split_urls(text: str) -> str:
    """Close a space that an earlier join put in the middle of a link."""
    def fix(m: re.Match[str]) -> str:
        url, cont = m.group(1), m.group(2)
        host = url.split("://", 1)[-1].split("/", 1)[0]
        if cont.startswith(".") and "." not in host:
            return url + cont
        if cont.startswith("/") and "/" not in url.split("://", 1)[-1]:
            return url + cont
        return m.group(0)

    prev = None
    cur = text or ""
    while cur != prev:
        prev = cur
        cur = re.sub(r"(https?://\S+)\s+(\.[^\s]+|/[^\s]+)", fix, cur, flags=re.I)
    return cur


_MED_ACTIONS = (
    "Start", "Stop", "Continue", "Consider", "Increase", "Reduce", "Decrease",
    "Try", "Restart", "Resume", "Add", "Switch to",
)
_NEW_MED = re.compile(
    r"(?<=\S)[\s.;]+(?=(?:" + "|".join(_MED_ACTIONS) + r")\s+[A-Z])"
)


def one_med_per_line(text: str) -> str:
    """One medication per line, each with a bullet. A new action (Start, Stop, …) starts the next line."""
    out: list[str] = []
    for raw in (text or "").replace("\r", "").splitlines():
        if not raw.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        if raw[:1] in " \t":
            out.append(raw.rstrip())
            continue
        s = raw.strip()
        mark = "• "
        if s[:1] in "•○●◦▪∙":
            mark = s[:1] + " "
            s = s[2:].lstrip() if s[1:2] == " " else s[1:].lstrip()
        elif s.startswith(("* ", "- ")):
            s = s[2:].lstrip()
        parts = [p.strip(" \t;.") for p in _NEW_MED.split(s)]
        parts = [p for p in parts if p]
        for p in parts:
            if p[:1] in "•○●◦▪∙":
                p = p[1:].lstrip()
            out.append(mark + p)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def reflow_body(text: str) -> str:
    """Join line breaks that the old-note extractor inserted mid-sentence. Bullets stay their own items."""
    items: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            items.append(buf.strip())
        buf = ""

    def bullet_line(s: str) -> str:
        if s in {"?", "•", "○", "●", "*", "-"}:
            return "•"
        if s[:1] in "○●◦•▪∙" or s.startswith(("* ", "- ", "? ")):
            rest = s[2:].lstrip() if s[1:2] == " " else s[1:].lstrip()
            return "• " + rest if rest else "•"
        return ""

    for raw in clip_amazon_urls(repair_split_urls(text)).replace("\r", "").split("\n"):
        s = raw.strip()
        if not s:
            flush()
            continue
        b = bullet_line(s)
        if b:
            flush()
            buf = b
            continue
        if buf == "•":
            buf = "• " + s
            continue
        if not buf:
            buf = s
            continue
        if _url_continuation(buf, s):
            buf += s
            continue
        if buf.endswith("-") and s[:1].isalnum():
            buf += s
            continue
        if _ends_with_url(buf):
            flush()
            buf = s
            continue
        if len(s) == 1 and s.isupper():
            flush()
            buf = s
            continue
        if len(buf) == 1 and buf.isalpha() and s[:1].islower():
            buf += s
            continue
        if buf.endswith("-") and s[:1].isalnum():
            buf += s
            continue
        if len(s) <= 2 and not s.isupper():
            buf += s
        else:
            buf += " " + s
    flush()
    return polish_avs("\n".join(items))


def polish_avs(text: str) -> str:
    """Match the edited note: whole words, Amazon links alone, a blank line between items."""
    text = clip_amazon_urls(repair_split_urls(text))
    text = peel_url_initial(text)
    text = re.sub(r"(?<![A-Za-z])([A-Z]) ([a-z])", r"\1\2", text)
    text = re.sub(r"(\w)-\s+-\s*", r"\1 - ", text)
    text = re.sub(r"\bORY\s*ogi", "OR\nYogi", text, flags=re.I)
    text = re.sub(r"[ \t]+(https?://\S+)", r"\n\1", text)
    text = re.sub(r"(https?://\S+)[ \t]+", r"\1\n", text)
    text = re.sub(r"\s+\b(OR)\b\s+", r"\n\1\n", text)
    text = re.sub(r"\s+(Less potent:|More potent:|Yogi:)", r"\n\1", text)
    lines: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if (s.startswith("•") or s == "OR") and lines and lines[-1] and not lines[-1].endswith(":"):
            lines.append("")
        lines.append(s)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def peel_url_initial(text: str) -> str:
    """Give back a single letter that was stuck on after the link's last slash."""
    def fix(m: re.Match[str]) -> str:
        url, word = m.group(1), m.group(2)
        path = url.split("://", 1)[-1]
        head, slash, last = url.rpartition("/")
        if slash and "." in path and len(last) == 1 and last.isalpha() and word[:1].islower():
            return head + "/\n" + last + word
        return m.group(0)

    return re.sub(r"(https?://\S+)\s+([A-Za-z][A-Za-z'-]*)", fix, text or "")


def _letterhead_lines(practice: dict) -> tuple[str, list[str]]:
    name = str(practice.get("name") or "").strip()
    right: list[str] = []
    right += _wrap(str(practice.get("address") or "").strip(), 44)[:3] if practice.get("address") else []
    contact = "   ".join(p for p in (
        f"Tel: {practice['phone']}" if practice.get("phone") else "",
        f"Fax: {practice['fax']}" if practice.get("fax") else "",
    ) if p)
    if contact:
        right.append(contact)
    if practice.get("email"):
        right.append(str(practice["email"]))
    lic = " · ".join(str(x) for x in practice.get("licenses") or [] if str(x).strip())
    if lic:
        right += _wrap("Licenses: " + lic, 60)[:2]
    if practice.get("npi"):
        right.append("NPI " + str(practice["npi"]))
    return name, [r for r in right if r]


def after_visit_pdf(
    fields: dict[str, str],
    sections: list[dict] | None = None,
    title: str = "",
    letterhead: dict | None = None,
    *,
    patient: bool = False,
    logo=None,
) -> bytes:
    """AVS / note PDF on the practice letterhead (multi-page, WinAnsi text, optional uploaded logo).

    patient=True: the patient-facing AVS (Meet again, identity, Medications, Supplements,
    Other recommendations). Chart-only sections (S/O/A, plan, interval update) are never printed on it.
    """
    practice = letterhead or {}
    head = title or "After Visit Summary"
    physician = fields.get("physician") or practice.get("clinician") or "Example Clinician, DO"
    if (fields.get("medications") or "").strip():
        fields = dict(fields)
        fields["medications"] = one_med_per_line(fields.get("medications") or "")
    blocks: list[tuple[str, str]] = []
    if patient:
        blocks += [("Name", fields.get("name") or ""), ("Date of Birth", fields.get("dob") or ""),
                   ("Physician", physician)]
        for fid, label in AVS_PATIENT_ORDER:
            if (fields.get(fid) or "").strip():
                blocks.append((label, fields.get(fid) or ""))
    else:
        blocks += [("Name", fields.get("name") or ""), ("Date of Birth", fields.get("dob") or ""),
                   ("Physician", physician)]
        if sections:
            for sec in sections:
                sid = str(sec.get("id") or "")
                if sid in ("name", "dob", "physician", "dos"):
                    continue
                blocks.append((str(sec.get("label") or sid), fields.get(sid) or ""))
        else:
            blocks += [
                ("Meet again", fields.get("return_visit") or ""),
                ("Subjective", fields.get("subjective") or ""),
                ("Objective", fields.get("objective") or ""),
                ("Assessment", fields.get("assessment") or ""),
                ("Medications", fields.get("medications") or ""),
                ("Supplements", fields.get("supplements") or ""),
                ("Other recommendations", fields.get("recommendations") or ""),
            ]

    # ---- layout into pages ------------------------------------------------
    pages: list[list[str]] = [[]]
    image = None
    if logo:
        try:
            from app.billing.superbill_pdf import _image_xobject

            image = _image_xobject(logo)
        except Exception:
            image = None

    def put(x: float, y: float, text: str, size: int, bold: bool) -> None:
        font = "/F1" if bold else "/F2"
        pages[-1].append(f"BT {font} {size} Tf {x:.1f} {y:.1f} Td (" + "\x00" + text + ") Tj ET")

    def text_width(text: str, size: float, bold: bool = False) -> float:
        return helvetica_width(text, size, bold)

    # letterhead (page 1). Name on the left, blue mark centered under that name.
    name, right = _letterhead_lines(practice)
    top = 756
    name_x = 72.0
    name_y = top - 4
    if name:
        put(name_x, name_y, name, 14, True)
    logo_bottom = name_y - 16
    if image:
        iw, ih = image["w"], image["h"]
        scale = min(56.0 / ih, 150.0 / iw)
        dw, dh = iw * scale, ih * scale
        name_w = text_width(name, 14) if name else dw
        logo_x = name_x + (name_w - dw) / 2
        logo_y = name_y - 8 - dh
        pages[-1].append(f"q {dw:.1f} 0 0 {dh:.1f} {logo_x:.1f} {logo_y:.1f} cm /Im1 Do Q")
        logo_bottom = logo_y
    ry = top
    for ln in right:
        put(318, ry, ln, 9, False)
        ry -= 11
    y = min(ry, logo_bottom) - 8
    pages[-1].append(f"0.75 G 0.6 w 72 {y:.1f} m 540 {y:.1f} l S 0 G")
    y -= 24
    put(72, y, head + (":" if patient else ""), 16, True)
    y -= 18
    sub = "" if patient else _pretty_dos(fields.get("dos") or "")
    dur = str(fields.get("duration") or "").strip()
    if dur.isdigit():
        sub = (sub + " • " if sub else "") + f"{dur} Min Office Visit"
    if sub:
        put(72, y, sub, 10, False)
        y -= 18

    def newpage() -> float:
        pages.append([])
        return 740

    if patient:
        if y < 72:
            y = newpage()
        # the host 2026-11-17, January-2025 letterhead. Left column is label over value with the value flush
        # under the label (N under the D). Right column is the Meet again block, right edge on the 540 pt
        # rule, and its rows share one vertical axis: the space in "Meet again", the comma after the day,
        # and the space in "30 Min" all sit on the same x.
        #
        #   Date of Service:                  Meet again:
        #   November 17, 2026      December 8, 2026, 11:00 AM
        #                                    30 Min
        dos_label, dos_val = "Date of Service:", _pretty_dos(fields.get("dos") or "")
        size = 10.0
        left_x, right_x = 72.0, 540.0
        rows = meet_rows(fields.get("return_visit") or "")

        def row_w(row: tuple[str, str, str], bold: bool) -> float:
            return sum(text_width(part, size, bold) for part in row)

        put(left_x, y, dos_label, size, True)
        put(left_x, y - 13, dos_val, size, False)
        widths = [row_w(row, i == 0) for i, row in enumerate(rows)]
        # One left edge for M / D / 3 (the host 2026-11-17); the widest row still ends on the right rule.
        block_x = right_x - max(widths)
        left_end = left_x + max(text_width(dos_label, size, True), text_width(dos_val, size))
        if block_x < left_end + 18:
            # No room for a column beside the date: drop the Meet again block under it, wrapped.
            y -= 26
            for indent, ln, ln_size in _layout_lines(" ".join("".join(r) for r in rows)):
                if y < 60:
                    y = newpage()
                if ln:
                    put(indent, y, ln, ln_size, False)
                y -= 13 if ln_size >= 10 else 11
            y -= 8
        else:
            for i, row in enumerate(rows):
                put(block_x, y - 13 * i, "".join(row), size, i == 0)
            y -= 13 * (len(rows) - 1) + 18

    inline = {"Name", "Date of Birth", "Physician"}
    for label, val in blocks:
        if label in inline:
            if y < 72:
                y = newpage()
            put(72, y, f"{label}:", 10, True)
            put(160, y, (val or "").strip(), 10, False)
            y -= 15
            if label == "Physician":
                y -= 22
            continue
        if y < 90:
            y = newpage()
        put(72, y, f"{label}:", 11, True)
        y -= 15
        for indent, ln, size in _layout_lines(val):
            if y < 60:
                y = newpage()
            if ln:
                put(indent, y, ln, size, False)
            y -= 13 if size >= 10 else 11
        y -= 8

    # ---- serialize ----------------------------------------------------------
    n_pages = len(pages)
    page_ids = [7 + 2 * i if image else 5 + 2 * i for i in range(n_pages)]
    # objects: 1 catalog, 2 pages, 3 font bold, 4 font regular, [5,6 = image + smask if image... ]
    objs: dict[int, bytes] = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    xobj = b""
    if image:
        # superbill helper numbers its objects 7 (image) and 8 (soft mask); renumber to 5/6.
        for chunk in image["objects"]:
            chunk = chunk.replace(b"/SMask 8 0 R", b"/SMask 6 0 R")
            num, rest = chunk.split(b" 0 obj ", 1)
            num_i = {b"7": 5, b"8": 6}[num.strip()]
            body = rest.rsplit(b"endobj", 1)[0].rstrip()
            objs[num_i] = body
        xobj = b" /XObject << /Im1 5 0 R >>"
    kids = []
    for i, ops in enumerate(pages):
        pid = page_ids[i]
        cid = pid + 1
        kids.append(f"{pid} 0 R".encode())
        stream = b"\n".join(_op_bytes(o) for o in ops)
        objs[pid] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents " + f"{cid} 0 R".encode()
                     + b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >>" + xobj + b" >> >>")
        objs[cid] = b"<< /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream"
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count " + str(n_pages).encode() + b" >>"
    top_id = max(objs)
    for k in range(1, top_id + 1):
        objs.setdefault(k, b"null")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k in range(1, top_id + 1):
        offsets.append(len(out))
        out += f"{k} 0 obj ".encode() + objs[k] + b" endobj\n"
    xref = len(out)
    out += f"xref\n0 {top_id + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer << /Size {top_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


_BULLETS = ("•", "-", "○", "*")


def _layout_lines(val: str) -> list[tuple[float, str, int]]:
    """(x, text) lines. Bullets at 84pt; indented lines (dot-phrase text under a bullet) and
    wrapped bullet tails at 96pt; plain paragraphs at 84pt."""
    out: list[tuple[float, str, int]] = []
    for raw in reflow_body(val).split("\n"):
        if not raw.strip():
            out.append((84, "", 10))
            continue
        nested = raw[:1] in (" ", "\t")
        s = raw.strip()
        bullet = s.startswith(_BULLETS)
        if s.startswith("* "):
            s = "• " + s[2:]
        x = 96 if nested else 84
        first = True
        for bit, is_url in _split_link_line(s):
            if is_url:
                for piece in _wrap_url(bit, 100):
                    out.append((84, piece, 8))
                first = False
                continue
            size = 10
            width = 84 if nested else 90
            for piece in _wrap(bit, width):
                out.append((x if first else (96 if (bullet or nested) else 84), piece, size))
                first = False
    while out and not out[-1][1]:
        out.pop()
    return out


def _op_bytes(op: str) -> bytes:
    """Ops are plain ASCII except text inside a \\x00-marked Tj string, which is escaped + WinAnsi."""
    if "\x00" not in op:
        return op.encode("latin-1", "replace")
    pre, rest = op.split("\x00", 1)
    text, post = rest.rsplit(") Tj ET", 1)
    return pre.encode("latin-1") + _enc(text) + b") Tj ET" + post.encode("latin-1")


def markdown_pdf(body: str) -> bytes:
    """Plain text-ish PDF from a rendered template."""
    lines: list[tuple[str, bool]] = []
    for raw in (body or "").replace("\r", "").split("\n"):
        s = raw.rstrip()
        if s.startswith("# "):
            lines.append((s[2:].strip(), True))
        elif s.startswith("## "):
            lines.append((s[3:].strip(), True))
        elif s.startswith("- "):
            lines.append((s[2:].strip(), False))
        else:
            for w in _wrap(s.lstrip("* ").rstrip("*"), 88):
                lines.append((w, False))
    y = 740
    parts = []
    for text, bold in lines:
        if y < 56:
            break
        size = 13 if bold else 10
        font = "/F1" if bold else "/F2"
        parts += ["BT", f"{font} {size} Tf", f"72 {y} Td", f"({_esc(text)}) Tj", "ET"]
        y -= 18 if bold else 13
    stream = "\n".join(parts).encode("latin-1", "replace")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >> endobj\n",
        b"4 0 obj << /Length "
        + str(len(stream)).encode()
        + b" >> stream\n"
        + stream
        + b"\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> endobj\n",
        b"6 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)
