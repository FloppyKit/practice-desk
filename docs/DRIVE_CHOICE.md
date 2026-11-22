# Chart storage for a vanilla install

This note is a map of what the code already does. It does not add a backend.

## Proton Drive — default, and it is the one the code speaks

Chart folders are Proton paths: living charts under `/Clients`, older charts under `/Client files`. The booker does not talk to Proton itself. It calls a notify service on `NOTIFY_WEBHOOK_URL` (default `http://127.0.0.1:8791/notify`):

- `POST /drive/mkdir` with a JSON `path`
- `GET /drive/list?path=`
- file read and save go through the same notify base

Signing a note creates `Billing` and `Chart/Visit Notes` (plus Consents, Labs, Letters, Orders) when the living folder is missing. If the drive cannot be listed, the sign returns HTTP 502 and writes nothing. Folder links in the patient record may point at `drive.proton.me`.

A new install keeps this default by running a small bridge that accepts those HTTP calls and performs them on a Proton Drive account. The bridge is not in this repo. Point `NOTIFY_WEBHOOK_URL` at it and set the notify token. No application change is required.

## Google Drive — optional, not recommended, not built

Google in this tree is the calendar (event read, write, and a mirror), not the chart. There is no Google Drive client, no Drive folder id, and no path that stores a note or a superbill in Google Drive. Adding it means a new module with the same mkdir/list/read/write operations the notify bridge already exposes, plus a choice at startup so a site does not write half the chart to Proton and half to Google. That is more moving parts, a second vendor, and a second place PHI can land. Leave it off unless a site already has a reason.

## Nextcloud — not built

Nothing in the booker speaks WebDAV or Nextcloud. A site that wants Nextcloud has to add a backend that honors the same folder layout (`/Clients/<Last, First>/Chart/...` and `Billing`) and the same failure rule: if the folder cannot be read, do not pretend the file was saved. The route code can stay if that backend is what the notify bridge calls. Building a second client inside the booker is the larger job.

## What a new install still has to add

- A notify bridge for Proton (the default), or a replacement that serves `/drive/mkdir` and `/drive/list` for another store.
- `NOTIFY_WEBHOOK_URL` and the notify token in the environment.
- The practice's own event-type file, mounted at deploy time. The example file is generic.
- Nothing else in this repo to choose Proton. Choosing Google Drive or Nextcloud is new work, and Google Drive is the one to skip.
