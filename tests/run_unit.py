"""Tiny runner so we do not need pytest on the box."""
from __future__ import annotations

import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_patch_seam() -> None:
    """A test that assigns main.helper = fake reaches route bodies, which call app.runtime.helper."""
    import app.runtime as runtime
    import app.main  # noqa: F401

    class _TestPatch(types.ModuleType):
        def __setattr__(self, name: str, value: object) -> None:
            super().__setattr__(name, value)
            if name.startswith('__'):
                return
            runtime.__dict__[name] = value

    sys.modules["app.main"].__class__ = _TestPatch


_install_patch_seam()

from tests import (  # noqa: E402
    test_avs,
    test_onboarding,
    test_intake_copies,
    test_desk_agent,
    test_billing,
    test_card_link,
    test_chat_threads,
    test_week_people,
    test_uploads,
    test_staff_auth,
    test_oss_hygiene,
    test_demo_store,
    test_cipher_p1,
    test_modejson,
    test_desk_mobile,
    test_slots,
    test_sms_bridge,
    test_spruce_api,
    test_verify_codes,
    test_public_portal,
    test_refills,
    test_shortlinks,
    test_cal_reminders,
    test_chart_store,
    test_drive_note,
    test_note_carry,
    test_visit_link,
    test_intake_fixes,
    test_scribe_drafts,
    test_scribe_guard,
    test_scribe_pieces,
)


def main() -> int:
    failed = 0
    ran = 0
    for mod in (
        test_avs,
        test_onboarding,
        test_intake_copies,
        test_desk_agent,
        test_billing,
        test_card_link,
        test_chat_threads,
        test_week_people,
        test_uploads,
        test_staff_auth,
        test_oss_hygiene,
        test_demo_store,
        test_cipher_p1,
        test_modejson,
        test_desk_mobile,
        test_slots,
        test_sms_bridge,
        test_spruce_api,
        test_verify_codes,
        test_public_portal,
        test_refills,
        test_shortlinks,
        test_cal_reminders,
        test_chart_store,
        test_drive_note,
        test_note_carry,
        test_visit_link,
        test_intake_fixes,
        test_scribe_drafts,
        test_scribe_guard,
        test_scribe_pieces,
    ):
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            ran += 1
            try:
                fn()
                print("ok", mod.__name__ + "." + name)
            except Exception:
                failed += 1
                print("FAIL", mod.__name__ + "." + name)
                traceback.print_exc()
    print("ran", ran, "failed", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
