# Chart storage for a vanilla install

The booker has one pipe for chart files: make a folder, list it, read a file, save a file, share a file. `CHART_BACKEND` picks who answers that pipe. The default is `proton`. Paths are the same on every backend: living charts under `/Clients/<Last, First>/`, with `Billing` and `Chart/Visit Notes` (plus Consents, Labs, Letters, Orders).

Signing a note creates those folders when the living folder is missing. If the store cannot be listed, the sign returns HTTP 502 and writes nothing.

## Proton Drive — default

The booker does not talk to Proton itself. It calls a notify service on `NOTIFY_WEBHOOK_URL` (default `http://127.0.0.1:8791/notify`):

- `POST /drive/mkdir` with a JSON `path`
- `GET /drive/list?path=`
- `GET /drive/file?path=`
- `POST /drive/save` with `path` and `b64` or `text`
- `POST /drive/share` with `path`

Leave `CHART_BACKEND` unset, or set it to `proton`. Run your own bridge that accepts those calls and set the notify token. The bridge is not in this repo. Folder links may point at `drive.proton.me`.

## Google Drive — optional

Set `CHART_BACKEND=gdrive`, `GOOGLE_DRIVE_SA_FILE` to a service-account JSON file, and `GOOGLE_DRIVE_ROOT_ID` to the folder that holds `/Clients`. Optional `GOOGLE_DRIVE_IMPERSONATE` is a Workspace user to act as. The calendar service account is not reused.

The same paths are folders under that root. A share makes an anyone-with-the-link reader permission and returns the Google link. If Google refuses the link, the share fails. It does not pretend the file was shared.

## Nextcloud — optional

Set `CHART_BACKEND=nextcloud`, `NEXTCLOUD_URL` (https), `NEXTCLOUD_USER`, and `NEXTCLOUD_APP_PASSWORD`. The app password is an app password, not the account password. The URL must be https, except localhost for a local box.

Files go over WebDAV (`MKCOL`, `PROPFIND`, `GET`, `PUT`) on the same paths. A share creates a public link through Nextcloud's share API. If that link is missing, the share fails.

## What a new install still sets

- `CHART_BACKEND=proton`, `gdrive`, or `nextcloud`. Anything else is an error.
- For Proton: `NOTIFY_WEBHOOK_URL` and the notify token, plus a bridge you run.
- For Google Drive: the service-account file and the root folder id.
- For Nextcloud: the URL, user, and app password.
- The practice's own event-type file, mounted at deploy time. The example file is generic.

Email and SMS still use the notify bridge. Only the chart calls switch with `CHART_BACKEND`.
