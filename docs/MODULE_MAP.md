Practice Desk — module map
==========================

One folder, one file, one job. Routes stay in app/routers/ and call helpers as runtime.<name>.

app/
  main.py                 Include one router per area. Shared helpers stay on runtime.
  runtime.py              Booking and desk steps the routes still call, plus re-exports.

app/http/
  shapes.py               Request bodies.
  signin.py               Staff and host sign-in checks.
  page_paint.py           Fill a public page and return it.

app/infra/
  chart_store.py          Pick proton, Google Drive, or Nextcloud. Same mkdir/list/read/save/share calls.
  chart_pipe.py           Chart calls the routes use. Proton still goes through the notify bridge.
  gdrive_store.py         Google Drive behind that pipe.
  nextcloud_store.py      Nextcloud WebDAV behind that pipe.
  drive_paths.py          Resolve a client to an existing chart folder. Do not invent names.
  clients_db.py           On-box client phone book. SQLite. No note bodies.
  practice.py             Public practice identity.
  config.py               Event-type file and the practice time zone.
  tokens.py               Signed cancel, verify, and book tokens.
  demo_store.py           Demo calendar from config/demo-week.json. No network.

app/booking/
  cal.py                  Calendar facade. Local CalDAV is the record. Google is an optional mirror.
  caldav_store.py         Read and write the local calendar.
  gcal.py                 Google Calendar API used by that mirror.
  gsync.py                Copy Google busy time onto the local calendar.
  ics.py                  Busy-only public feed, plus a token feed for one visit.
  slots.py                Open times for a visit type.
  event_store.py          Desk-edited visit types, over the shipped example file.
  week_people.py          People on the week grid, for desk search. Pointers only.
  visit_link.py           Match a scribe recording to a visit by time.
  reminders.py            Timed reminders for booker visits.
  cal_reminders.py        Timed reminders for every timed visit on the practice calendar.
  shortlinks.py           Short links for booking, pay, forms, and intake.
  practice_links.py       Example host names for links.
  card_link.py            Send the card-change link to the email on file.

app/notes/
  note_pdf.py             After-visit summary PDF.
  note_parse.py           Turn a fixed scribe note into desk fields. No model call.
  note_carry.py           Carry the last signed summary into a new draft.
  note_templates.py       Note templates as section boxes.
  note_prefs.py           Remember a signed edit the host made.
  signed_notes.py         One row per signed summary.
  drive_note.py           Turn a visit-note file into template fields.
  pdf_text.py             Visible text from a note PDF.
  dot_phrases.py          Staff text expanders.
  style_pack.py           Load the synthetic style seed.
  style_pack_learn.py     Update local style counts from a scrubbed signed note.
  scribe_drafts.py        Pending visit drafts. Bodies are not logged.
  visit_flags.py          Staff flags on a visit. Not a charge and not a chart note.
  intake_pdf.py           Signed intake form as a PDF.
  intake_copies.py        Which signed intake PDFs to list, and when a resend is due.
  intake_batch.py         Intake-link outreach, throttled and resumable.

app/billing/
  billing.py              Visit codes, the letterhead gate, billing records, share links.
  fees.py                 Visit-fee ledger. Same SQLite as the phone book.
  helpers.py              Billing helpers the routes call.
  stripe_pay.py           Off-session card charge. Description is the date only.
  superbills.py           One statement from picked visits, fees, and imported visits.
  superbill_pdf.py        Statement PDF.

app/messaging/
  notify.py               Booking email on the notify bridge, plus an optional SMS send.
  email_templates.py      One card layout for practice email.
  sms_bridge.py           Queue a text for the desk to send.
  spruce_api.py           Send a logistics text through Spruce.
  chat_threads.py         Website chat. The patient writes here.
  chat_links.py           Pre-authenticated reply link. The code is random. No PHI in the URL.

app/portal/
  patients.py             Client phone book used by the desk. No notes.
  staff_auth.py           Server-to-server staff secret, header only.
  desk_auth.py            Desk sign-in: emailed code, then a rolling session cookie.
  verify_codes.py         One-time code to the email or mobile already on the account.
  portal_sessions.py      Website-portal session: opaque, revocable, rolling.
  onboarding.py           Intake flags and held new-client bookings.
  uploads.py              Sniff one portal upload and record where it was filed.
  refills.py              Refill request intake, match, and rules. Read-only.
  desk_call.py            Staff click-to-call.
  desk_agent.py           Staff assistant tools. Not the public chat.
  cipher_mode.py          Cipher flags. Default off.
  cipher_seal.py          Seal and open a booking envelope. Ciphertext only on the server.
  cipher_queue.py         Queue of sealed envelopes. The desk drains it.

app/routers/
  book.py                 Public book, cancel, feeds, desk booking, and the call routes.
  desk.py                 Staff desk API, practice profile, chart files, and the desk page.
  notes.py                Sign, templates, phrases, scribe drafts, and cipher drain.
  billing.py              Fees, card pay, and superbills.
  chat.py                 Chat threads, the text queue, and desk mail.
  portal.py               Patient verify, portal session, and uploads.
  pages.py                Public page for a visit-type slug. Included last.

app/data/
  email_defaults.json     Example practice identity loaded at startup.

static/
  desk.html               Staff desk. Week, notes, practice, billing, mail, and refills stay on this page.
  book.html               Public booking page.
  index.html              Public front door.
  new-client.html         New-client page.
  follow-up.html          Follow-up page.
  thanks.html             After-booking page.
  css/booker.css          Public booker styles.
  js/booker.js            Public slot picker.
  js/theme.js             Light and dark toggle.
  js/desk-auth.js         Staff fetches. The secret is a header, never a query string.
  js/desk-layout.js       Tablet layout: notes, office, or desk.
  js/desk-vault.js        Encrypted last-note vault, plus the sealed-intake panel.
  js/desk-books.js        Books panel.
  js/desk-services.js     Practice services and weekly hours.
  js/desk-phrases.js      Dot-phrase library and in-note expand.
  js/desk-call.js         Click-to-call bar, plus the handset toggle.
  js/chart-drive.js       Chart file browser, plus opening and closing that panel.
  js/desk-sw.js           Shell cache. It does not cache API responses or note bodies.
  js/cipher-envelope.js   Seal a booking envelope in the browser.
  js/cipher-booker.js     Booker glue that queues the sealed envelope before booking.
  js/pa-router.js         Staff-assistant verbs.
  js/staff-agent.js       Staff-assistant box. Local verbs first.

tests/
  run_unit.py             Run every test_ function. No pytest required.
  scan_secrets.py         Fail the tree on secrets, live clinic defaults, or a real style corpus.
  scribe.py               Visit piece tracker. Loaded by test_scribe_pieces.py.
  test_*.py               One module per area.

scripts/
  modejson.py             Print the desk mode as JSON. Default is demo.

config/
  event-types.example.yaml  Example visit types. The real file is mounted, not committed.
  billing-codes.yaml        Example visit codes.
  demo-week.json            Fake week for demo mode.
  note-templates/           Shipped note templates.
  style-pack/               Synthetic style seed.

radicale/
  config                    Local calendar server config.
  mkusers.py                Write the calendar user file from the environment.
  Dockerfile                Calendar server image.
