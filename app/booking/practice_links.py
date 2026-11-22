"""Practice link bases: one place for the example.com bases.

PRACTICE_SITE_URL   public site (default https://example.com)
PRACTICE_VIDEO_URL  video waiting room (default https://meet.example.com)
"""
from __future__ import annotations

import os


def site_url() -> str:
    return (os.environ.get("PRACTICE_SITE_URL") or "https://example.com").strip().rstrip("/")


def video_url() -> str:
    return (os.environ.get("PRACTICE_VIDEO_URL") or "https://meet.example.com").strip().rstrip("/")


def book_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or "https://book.example.com").strip().rstrip("/")


def short_host() -> str:
    return (os.environ.get("SHLINK_DOMAIN") or "go.example.com").strip().lower()


def link_map() -> list[tuple[str, str]]:
    """Old/wrong practice links found in dot phrases -> current live link."""
    s, v = site_url(), video_url()
    return [
        ("https://doxy.me/example", v),
        ("portal.example.com", f"{s}/portal"),
        ("www.example.com", s),
    ]


def fix_text(body: str) -> tuple[str, int]:
    n = 0
    for old, new in link_map():
        c = body.count(old)
        if c and old not in new:
            body = body.replace(old, new)
            n += c
    return body, n


def book_host() -> str:
    return book_url().split("://", 1)[-1]


def video_host() -> str:
    return video_url().split("://", 1)[-1]


def live_origin() -> str:
    return (os.environ.get("PRACTICE_LIVE_URL") or "https://live.example.com").strip().rstrip("/")


def portal_origin() -> str:
    return (os.environ.get("PRACTICE_PORTAL_URL") or "https://portal.example.com").strip().rstrip("/")


def www_origin() -> str:
    return (os.environ.get("PRACTICE_WWW_URL") or "https://www.example.com").strip().rstrip("/")


def cors_origins() -> list[str]:
    """Browser origins for the example install. Env overrides win. Duplicates dropped."""
    raw = [
        book_url(),
        site_url(),
        video_url(),
        live_origin(),
        www_origin(),
        portal_origin(),
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "http://127.0.0.1:7882",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
    out: list[str] = []
    for item in raw:
        if item and item not in out:
            out.append(item)
    return out
