# Practice Desk

No PHI in this repo. No clinic name, no clinician name, no real phone, fax, address, or mailbox. Secrets stay in the environment, never in git.

The booker is one process. `app/main.py` includes one APIRouter per area. `app/runtime.py` re-exports the shared helpers. Request shapes, sign-in, the chart pipe, billing helpers, and page paint each live in one module. Route bodies live in `app/routers/` and call those helpers as `runtime.<name>`. A helper reads a name a test might replace from `app.runtime` at call time. Tests assign `app.main.<name>`; the runner copies that assignment onto `app.runtime`.

| Folder | Job |
| --- | --- |
| `app/routers/` | HTTP routes: book, desk, notes, billing, chat, portal, pages |
| `app/booking/` | Calendars, slots, links, reminders |
| `app/notes/` | Notes, intake files, scribe drafts |
| `app/http/` | Request shapes, sign-in, page paint |
| `app/billing/` | Fees, card charges, superbills, and the billing helpers |
| `app/messaging/` | Email, SMS, chat threads |
| `app/portal/` | Patient sign-in, staff auth, onboarding |
| `app/infra/` | Config, tokens, the client store, the chart pipe |
| `app/data/` | Email identity loaded at startup |

`api_book` is a sequence of named steps: email, visit type, consent, gates, time span, slot check, cipher check, hold, description, calendar write, consent save, hold record, dual-write, notices, response. A calendar write that does not land is HTTP 502. Saving the hold row stays fail-open so a booked visit is not reported as failed when only the hold index did not write.

Money, sign-in, and calendar writes raise when they fail. A bad token is a 401 or 404. Desk cancel deletes the visit first, then sends email or SMS; a failed notice is HTTP 502 and is not reported as a clean success. The superbill visit list, the billing-folder lookup, and the payment thank-you page return HTTP 502 when the read fails. Staff routes take the secret from the `x-staff-secret` header only. The scribe ingest token is compared with `hmac.compare_digest`.

Example hosts, the host label, and the example time zone come from `app/booking/practice_links.py` and `app/data/email_defaults.json`. A mounted config file overrides the time zone. `config/event-types.yaml` is not in git; copy `config/event-types.example.yaml`. The style pack in git is a synthetic seed.

Config for a real clinic (event types, practice profile, secrets) is mounted at deploy time. This tree ships example hosts on `example.com` and empty secret fields.

Chart files use one pipe: mkdir, list, read, save, share. `CHART_BACKEND` picks it. `proton` (the default) calls the notify bridge. `gdrive` and `nextcloud` answer those same calls in-process. Paths stay `/Clients/...`. See `docs/DRIVE_CHOICE.md`. A missing credential is an error, not an empty success.
