"""Pull visible text out of a note PDF. No extra packages. No logging."""
from __future__ import annotations

import re
import zlib


def _unescape(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c != 0x5C or i + 1 >= len(raw):
            out.append(c)
            i += 1
            continue
        n = raw[i + 1]
        if n in (0x28, 0x29, 0x5C):
            out.append(n)
            i += 2
        elif n in (0x6E, 0x72):
            out.append(0x0A)
            i += 2
        elif n == 0x74:
            out.append(0x09)
            i += 2
        elif 48 <= n <= 55:
            octal = bytes([n])
            i += 2
            for _ in range(2):
                if i < len(raw) and 48 <= raw[i] <= 55:
                    octal += bytes([raw[i]])
                    i += 1
                else:
                    break
            out.append(int(octal, 8) & 0xFF)
        else:
            out.append(n)
            i += 2
    return out.decode("cp1252", "replace")


def _strings(blob: bytes) -> list[str]:
    found = []
    i = 0
    while i < len(blob):
        if blob[i] != 0x28:
            i += 1
            continue
        i += 1
        start = i
        depth = 1
        while i < len(blob) and depth:
            if blob[i] == 0x5C:
                i += 2
                continue
            if blob[i] == 0x28:
                depth += 1
            elif blob[i] == 0x29:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            found.append(_unescape(blob[start:i]))
        i += 1
    return found


def _streams(data: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        chunk = data[start:end].rstrip(b"\r\n")
        head = data[max(0, m.start() - 800):m.start()]
        if b"FlateDecode" in head:
            try:
                chunk = zlib.decompress(chunk)
            except zlib.error:
                continue
        out.append(chunk)
    return out


def _literal_text(data: bytes) -> str:
    """Lines of text in reading order, as well as a simple PDF allows."""
    lines: list[str] = []
    blobs = _streams(data) or [data]
    for blob in blobs:
        if b"Tj" not in blob and b"TJ" not in blob:
            continue
        for s in _strings(blob):
            s = s.replace("\x00", "").strip()
            if s:
                lines.append(s)
    return "\n".join(lines).strip()


def _cmap(blob: bytes) -> dict[bytes, str]:
    """ToUnicode bfchar / bfrange. Identity-H AVS files store glyphs as hex, not literal strings."""
    text = blob.decode("latin1", "replace")
    mapping: dict[bytes, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                ch = bytes.fromhex(dst).decode("utf-16-be") if len(dst) >= 4 else chr(int(dst, 16))
                mapping[bytes.fromhex(src)] = ch
            except (ValueError, UnicodeDecodeError):
                continue
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for src, end, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            width = max(1, len(src) // 2)
            try:
                start_n, end_n, dst_n = int(src, 16), int(end, 16), int(dst, 16)
            except ValueError:
                continue
            for i, cp in enumerate(range(start_n, min(end_n, start_n + 800) + 1)):
                mapping[cp.to_bytes(width, "big")] = chr(dst_n + i)
    return mapping


def _decode_hex(raw: bytes, mapping: dict[bytes, str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        key2 = raw[i:i + 2]
        key1 = raw[i:i + 1]
        if len(key2) == 2 and key2 in mapping:
            out.append(mapping[key2])
            i += 2
        elif key1 in mapping:
            out.append(mapping[key1])
            i += 1
        else:
            i += 1
    return "".join(out)


def _identity_text(data: bytes) -> str:
    """Text from Identity-H PDFs (ToUnicode + hex Tj/TJ). Empty when the file has no such map."""
    blobs = _streams(data)
    mapping: dict[bytes, str] = {}
    for blob in blobs:
        if b"beginbfchar" in blob or b"beginbfrange" in blob:
            mapping.update(_cmap(blob))
    if not mapping:
        return ""
    lines: list[str] = []
    op = re.compile(
        rb"(\[(?:[^\[\]]|<[^>]*>)*\]\s*TJ)|(<[0-9A-Fa-f]+>\s*Tj)|(\((?:\\.|[^\\)])*\)\s*Tj)"
    )
    for blob in blobs:
        if b"Tj" not in blob and b"TJ" not in blob:
            continue
        for m in op.finditer(blob):
            chunk = m.group(0)
            bits = [_decode_hex(bytes.fromhex(h.decode()), mapping) for h in re.findall(rb"<([0-9A-Fa-f]+)>", chunk)]
            if not any(bits):
                bits = [_unescape(s) for s in _raw_parens(chunk)]
            line = "".join(bits).replace("\x00", "").strip()
            if line:
                lines.append(line)
    return "\n".join(lines).strip()


def _raw_parens(blob: bytes) -> list[bytes]:
    found = []
    i = 0
    while i < len(blob):
        if blob[i] != 0x28:
            i += 1
            continue
        i += 1
        start = i
        depth = 1
        while i < len(blob) and depth:
            if blob[i] == 0x5C:
                i += 2
                continue
            if blob[i] == 0x28:
                depth += 1
            elif blob[i] == 0x29:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            found.append(blob[start:i])
        i += 1
    return found


def _has_heading(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in ("medication", "supplement", "recommendation", "subjective", "assessment", "meet again"))


def _positioned_text(data: bytes) -> str:
    """Join one-glyph Tj runs that share a text line. Older AVS files emit each letter alone."""
    blobs = _streams(data)
    mapping: dict[bytes, str] = {}
    for blob in blobs:
        if b"beginbfchar" in blob or b"beginbfrange" in blob:
            mapping.update(_cmap(blob))
    if not mapping:
        return ""
    op = re.compile(
        rb"(?P<tm>(?:[-+0-9.]+\s+){5}[-+0-9.]+\s+Tm)"
        rb"|(?P<hex><[0-9A-Fa-f]+>\s*Tj)"
        rb"|(?P<lit>\((?:\\.|[^\\)])*\)\s*Tj)"
    )
    lines = [""]
    for blob in blobs:
        if b"Tj" not in blob and b"TJ" not in blob:
            continue
        for m in op.finditer(blob):
            if m.group("tm"):
                if lines[-1]:
                    lines.append("")
                continue
            if m.group("hex"):
                raw = re.search(rb"<([0-9A-Fa-f]+)>", m.group("hex"))
                if raw:
                    lines[-1] += _decode_hex(bytes.fromhex(raw.group(1).decode()), mapping)
                continue
            if m.group("lit"):
                inner = _raw_parens(m.group("lit"))
                if inner:
                    lines[-1] += _unescape(inner[0])
    return "\n".join(ln.strip() for ln in lines if ln.strip())


def pdf_text(data: bytes) -> str:
    """Lines of text in reading order. Desk PDFs are literal strings; older AVS files are Identity-H."""
    plain = _literal_text(data)
    if _has_heading(plain):
        return plain
    ident = _identity_text(data)
    if _has_heading(ident):
        return ident
    positioned = _positioned_text(data)
    if _has_heading(positioned):
        return positioned
    return positioned or plain or ident
