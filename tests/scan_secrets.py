"""Fail if the tree contains secrets, live clinic defaults, or a clinic style-pack corpus."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = (
    "app",
    "static",
    "tests",
    "config",
    "scripts",
    "docs",
    "radicale",
    "_host",
)
SKIP_DIRS = {".git", ".venv", "__pycache__", ".ruff_cache", "node_modules"}
SKIP_SUFFIXES = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico",
    ".woff", ".woff2", ".zip", ".svg",
}

PRACTICE_PY = ROOT / "app" / "infra" / "practice.py"

FORBIDDEN_DEFAULT_FRAGMENTS = (
    "1881910081",
    "036.132941",
    "TW-00062",
    "86-1977816",
)
CORPUS_MARKERS = (
    "needle-deid-pack-avs",
    "Desktop/needle-deid",
)

REMIND_SECRET_RE = re.compile(r"REMIND_CRON_SECRET\s*=\s*\S")
PRIVATE_KEY_RE = re.compile(r"BEGIN (RSA|OPENSSH|PRIVATE)")
FETCH_SECRET_RE = re.compile(r"(fetch|deskFetch)\([^)]*\?secret=", re.I)
SK_KEY_RE = re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b")
NPI_DEFAULT_RE = re.compile(r"""["']npi["']\s*:\s*["'](\d{10})["']""")


def _skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    name = path.name
    if name == ".env" or (name.startswith(".env") and name != ".env.example"):
        return True
    if ".bak" in name:
        return True
    return False


def _iter_files() -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        path = path.resolve()
        if path in seen or not path.is_file() or _skip(path):
            return
        seen.add(path)
        out.append(path)

    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            add(path)
    for path in sorted(ROOT.iterdir()):
        if path.is_file():
            add(path)
    return out


def _extract_braced_block(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    brace = text.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for i in range(brace, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    return ""


def _check_practice_defaults(text: str, errors: list[str]) -> None:
    defaults_src = _extract_braced_block(text, "DEFAULTS")
    if not defaults_src:
        errors.append("app/infra/practice.py: DEFAULTS block not found")
        return
    for frag in FORBIDDEN_DEFAULT_FRAGMENTS:
        if frag in defaults_src:
            errors.append(f"app/infra/practice.py: forbidden default fragment {frag!r}")
    m = NPI_DEFAULT_RE.search(defaults_src)
    if m and m.group(1) != "0000000000":
        errors.append(f"app/infra/practice.py: NPI-shaped default {m.group(1)!r}")

    m = re.search(r"DEFAULT_LICENSES[^=]*=\s*(\[[^\]]*\])", text, re.S)
    if not m:
        errors.append("app/infra/practice.py: DEFAULT_LICENSES block not found")
        return
    licenses_src = m.group(1)
    if licenses_src.strip() not in ("[]", "[ ]"):
        for frag in FORBIDDEN_DEFAULT_FRAGMENTS:
            if frag in licenses_src:
                errors.append(f"app/infra/practice.py: forbidden license default {frag!r}")
        if re.search(r"\{\s*[\"']state[\"']", licenses_src):
            errors.append("app/infra/practice.py: DEFAULT_LICENSES must be empty for OSS")


def _scan_file(path: Path, errors: list[str]) -> None:
    rel = path.relative_to(ROOT)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    check_corpus = path.name != "scan_secrets.py"
    for i, line in enumerate(text.splitlines(), 1):
        where = f"{rel}:{i}"
        if REMIND_SECRET_RE.search(line) and path.name != "scan_secrets.py":
            errors.append(f"{where}: REMIND_CRON_SECRET with value")
        if PRIVATE_KEY_RE.search(line):
            errors.append(f"{where}: private key material")
        if FETCH_SECRET_RE.search(line) and "never" not in line.lower():
            errors.append(f"{where}: ?secret= on fetch/deskFetch URL")
        if "?secret=" in line and "never" not in line.lower():
            if re.search(r"(fetch\s*\(|deskFetch\s*\(|fetch\s*[`'\"]|deskFetch\s*[`'\"])", line, re.I):
                errors.append(f"{where}: ?secret= on fetch/deskFetch URL")
        if path.parts[-2:][0] != "tests" and path.parent.name != "tests":
            if ' + "secret="' in line or " + 'secret='" in line:
                errors.append(f"{where}: staff secret concatenated onto a URL")
        if SK_KEY_RE.search(line):
            errors.append(f"{where}: sk- API key pattern")
        if check_corpus:
            for marker in CORPUS_MARKERS:
                if marker in line:
                    errors.append(f"{where}: style-pack corpus marker")
                    break


def main() -> int:
    errors: list[str] = []
    if PRACTICE_PY.is_file():
        _check_practice_defaults(PRACTICE_PY.read_text(encoding="utf-8"), errors)
    for path in _iter_files():
        _scan_file(path, errors)
    if errors:
        print("scan_secrets FAIL")
        for err in errors:
            print(" ", err)
        return 1
    print("scan_secrets ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
