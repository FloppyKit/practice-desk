from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO / "config" / "event-types.example.yaml"
_LOCAL = _REPO / "config" / "event-types.yaml"


def _default_config_path() -> Path:
    chosen = (os.environ.get("BOOKER_CONFIG") or "").strip()
    if chosen:
        return Path(chosen)
    if _LOCAL.is_file():
        return _LOCAL
    return _EXAMPLE


CONFIG_PATH = _default_config_path()
SA_PATH = Path(
    os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "/secrets/google-calendar/service-account.json",
    )
)
DEFAULT_CALENDAR = os.environ.get("GOOGLE_CALENDAR_ID", "")


_CFG_CACHE: dict[str, Any] = {"key": None, "data": None}


def timezone_name() -> str:
    """Example zone lives in the email defaults. A mounted config overrides it."""
    import json

    fallback = "America/Chicago"
    defaults = _REPO / "app" / "data" / "email_defaults.json"
    try:
        fallback = str(json.loads(defaults.read_text(encoding="utf-8")).get("timezone") or fallback)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return fallback


def practice_zone():
    from zoneinfo import ZoneInfo

    name = timezone_name()
    if CONFIG_PATH.is_file():
        name = str(load_config().get("timezone") or name)
    return ZoneInfo(name)


def load_config() -> dict[str, Any]:
    # deskfix12: YAML parse was ~45 ms and runs 3x per drawer open; cache it by file mtime/size.
    import copy

    st = CONFIG_PATH.stat()
    key = (st.st_mtime_ns, st.st_size)
    if _CFG_CACHE["key"] != key:
        with CONFIG_PATH.open() as f:
            _CFG_CACHE["data"] = yaml.safe_load(f) or {}
        _CFG_CACHE["key"] = key
    data = copy.deepcopy(_CFG_CACHE["data"])
    data.setdefault("timezone", timezone_name())
    data.setdefault("busy_calendars", [DEFAULT_CALENDAR] if DEFAULT_CALENDAR else [])
    data.setdefault("event_types", {})
    from app.booking.event_store import apply_overlay

    return apply_overlay(data)


def get_event_type(config: dict[str, Any], slug: str) -> dict[str, Any] | None:
    et = config.get("event_types", {}).get(slug)
    if not et or et.get("enabled") is False:
        return None
    return et
