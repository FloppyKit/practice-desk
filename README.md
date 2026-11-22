# Practice Desk

**No PHI in this repo.** No clinic name, no real phone, and no secrets. Those stay in the environment and in the practice profile on the host.

This is a booker and a staff desk. You configure it. Charts are files in Proton Drive, Google Drive, or Nextcloud. See [ARCHITECTURE.md](ARCHITECTURE.md) for the layout and [docs/MODULE_MAP.md](docs/MODULE_MAP.md) for one job per file.

[docs/PRODUCT.md](docs/PRODUCT.md) is older than the code. It still says the chart stays in Proton Drive and that there is no patient sign-in. The booker can use Google Drive or Nextcloud, and it has a portal session.

## Run it

Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/event-types.example.yaml config/event-types.yaml
```

In `.env`, set `BOOKER_CONFIG` to the visit file you copied. The value shipped in `.env.example` is the path inside the container (`/app/config/event-types.yaml`). On your own machine, use the path of the copy. If `BOOKER_CONFIG` is unset and `config/event-types.yaml` is missing, the app reads `config/event-types.example.yaml`.

Demo calendar, no network: set `CALENDAR_BACKEND=demo` or `DEMO=1`. The week comes from `config/demo-week.json`.

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

The image in `Dockerfile` runs the same app on port 8090. There is no compose file in this repo.

## Charts

`CHART_BACKEND` is `proton` (the default), `gdrive`, or `nextcloud`. See [docs/DRIVE_CHOICE.md](docs/DRIVE_CHOICE.md).

Proton does not talk to Proton from this repo. It calls a notify bridge on `NOTIFY_WEBHOOK_URL` (default `http://127.0.0.1:8791/notify`). You run that bridge. It is not included here. Email and SMS stay on that bridge. They do not switch when you change `CHART_BACKEND`.

A Google share is an anyone-with-the-link reader. A Nextcloud share is a public link. Those links are for a demo folder only. This booker does not create a named-account share or a login-gated link. Do not put real charts on that share.

## Booking page

The public booking page reads the visit title, length, and fee from the event-type file. It reads the clinician name from the practice profile (`clinician_name` and `clinician_title` in the practice directory). Until you set a name, the page says "the clinician". An optional `description` on a visit type replaces the generated sentence.

## Phone

The desk call button places a call through Twilio Voice when `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set. That is the phone option in this booker. Leave those empty and the button has no live call.
