# TrueNAS + qBittorrent + Minecraft + Seerr + Plex iCUE Widget Relay

A local relay that polls a TrueNAS SCALE server, one or more Crafty-managed
Minecraft servers, qBittorrent, Seerr, and Plex, then serves the combined
state as JSON over localhost for Corsair iCUE XENEON Edge widgets to
display and control.

## Why a relay exists at all

iCUE widgets run in a sandboxed webview: they can't hold API keys securely,
can't speak TrueNAS's WebSocket/JSON-RPC API, can't do raw TCP (which is
what pinging a Minecraft server actually requires), and can't survive
qBittorrent's cookie-session auth or SameSite restrictions across origins.
Seerr and Plex's REST APIs are friendlier (a simple header carries the
credential), but that credential still shouldn't sit in a webview's JS
where it's visible to anyone who opens devtools on the widget. The relay
is a small always-on Python process on the same Windows PC as iCUE that
does all of this server-side, and exposes the result as plain JSON the
widget can `fetch()`.

```
┌─────────────┐        ┌───────────────┐        ┌────────────────────┐
│  TrueNAS    │◄──────►│               │        │                    │
│ (WebSocket) │        │               │        │                    │
├─────────────┤        │               │        │                    │
│ Minecraft   │  SLP   │   relay.py    │◄──────►│    iCUE widgets    │
│ server(s)   │◄──────►│ (localhost:   │  HTTP  │ (poll /stats,      │
├─────────────┤        │    8787)      │        │  /mc-stats,        │
│ Crafty      │  REST  │               │        │  /qbit-stats,      │
│ Controller  │◄──────►│               │        │  /seerr-stats,     │
├─────────────┤        │               │        │  /seerr-search,    │
│ qBittorrent │  REST  │               │        │  /plex-stats)      │
│ (session)   │◄──────►│               │        │                    │
├─────────────┤        │               │        │                    │
│ Seerr       │  REST  │               │        │                    │
│ (API key)   │◄──────►│               │        │                    │
├─────────────┤        │               │        │                    │
│ Plex        │  REST  │               │        │                    │
│ (token)     │◄──────►│               │        │                    │
└─────────────┘        └───────────────┘        └────────────────────┘
```

## What it monitors and controls

**TrueNAS** — CPU/RAM/temp, network throughput, disk I/O, ZFS pool health
and capacity, app status and pending updates, alerts, backup task status.
Polled two ways: a realtime WebSocket subscription for fast-moving stats,
and a slower periodic poll (every `slow_poll_seconds`) for anything that
doesn't need per-second freshness.

**Minecraft server(s)** — each server is queried directly over the Java
Server List Ping protocol (via `mcstatus`), not through Crafty. This is
deliberate: the ping protocol needs no credentials, and for a Forge server
it's also where the mod list comes from (mod ID + version, embedded in the
ping response itself). Reports online/offline, player count and names,
version, MOTD, latency, and — for modded servers — the full mod list.

Any number of servers can be configured, each with its own `host:port`,
and each polled on its own thread — they're fully independent and can all
be online and joinable at the same time.

**Crafty Controller** — used only for what the ping protocol can't
provide: starting/stopping/restarting a server, triggering a backup, and
reporting CPU/RAM/world size and whether the process is starting/updating.
This needs a Crafty API token and is entirely optional per server — if a
given server has no `crafty_server_id` configured, the relay still works
for read-only monitoring of that server, the control buttons just don't
appear in the widget for it. One shared token/base URL covers however many
servers are configured; each server's own `crafty_server_id` says which
Crafty-side server it maps to.

**qBittorrent** — polled via cookie-session auth (qBittorrent's WebUI API
has no API-key auth, only username/password → session cookie, held for the
life of the relay process). Reports per-torrent state, progress and speed,
plus session-wide transfer totals, and can pause/resume individual
torrents.

**Seerr** — polled via `X-Api-Key` header auth against Seerr's REST API
(the Overseerr/Jellyseerr-compatible `/api/v1/...` routes). Reports
recently-added movies/TV (split by type), popular movies/TV (via Seerr's
discover endpoints), and recent requests with their approval/availability
status. Also supports live search-as-you-type and submitting a new request
— the widget calls the relay, the relay forwards to Seerr with the API
key attached, so the key never reaches the widget's JS. Request/media
titles and posters are resolved via a follow-up TMDB lookup and cached in
memory, since Seerr's request/media list endpoints only return IDs, not
titles.

**Plex** — polled via `X-Plex-Token` header auth against the Plex Media
Server's own REST API (no separate API-key system — the token is the same
one Plex's own apps use, generated once via a Plex sign-in). Reports live
playback sessions — poster, user, device, playback state, progress,
direct play vs. direct stream vs. transcode (and whether a transcode is
using hardware acceleration), per-session bandwidth, and LAN vs. WAN — plus
recently added movies/TV/seasons/episodes. Also supports stopping
("kicking") a session: the widget calls the relay, the relay forwards to
Plex's terminate endpoint with the token attached, same pattern as Seerr's
request submission.

## Setup

Install dependencies:

```
pip install -r requirements.txt
```

Fill in `config.json`:

`host`, `username`, `api_key` — your TrueNAS SCALE box and an API key
generated from its UI.

`minecraft.servers` — a list, one entry per Minecraft server:

```json
"minecraft": {
  "poll_seconds": 15,
  "mod_list_cap": 400,
  "crafty": {
    "base_url": "...",
    "api_token": "...",
    "verify_ssl": false
  },
  "servers": [
    { "name": "Direwolf20 1.21", "host": "192.168.1.181", "port": 25565, "crafty_server_id": "..." },
    { "name": "Direwolf20 1.20", "host": "192.168.1.181", "port": 25566, "crafty_server_id": "..." }
  ]
}
```

- `host` / `port` — where that server itself listens (not Crafty's web UI
  port). Each server needs its own port if they're meant to run at the
  same time.
- `minecraft.crafty` (optional, for start/stop/restart/backup):
  - `base_url` — Crafty's web UI address, including the correct scheme
    (`https://`, even on a non-default port — Crafty serves HTTPS with a
    self-signed cert by default, so also leave `verify_ssl: false` unless
    you've replaced that cert).
  - `api_token` — Crafty web UI → your username → Profile → API Keys →
    Generate Key. The token's role needs Commands permission on every
    target server for start/stop/restart/backup to work.
  - `crafty_server_id` per server — the UUID Crafty uses internally for
    that server (visible in the URL when viewing it in Crafty's UI, or via
    `GET /api/v2/servers`). Omit it on a server to leave that one
    monitoring-only.

`qbittorrent` (optional):

```json
"qbittorrent": {
  "enabled": true,
  "base_url": "http://127.0.0.1:8080",
  "username": "...",
  "password": "...",
  "poll_seconds": 3,
  "max_rows": 5
}
```

Leave `username` blank if your qBittorrent instance uses localhost-bypass
or subnet-whitelist auth instead of a login form.

`seerr` (optional):

```json
"seerr": {
  "enabled": true,
  "base_url": "http://192.168.1.181:30357",
  "api_key": "...",
  "poll_seconds": 30
}
```

- `base_url` — Seerr's own web UI address; the API lives at
  `{base_url}/api/v1/...`.
- `api_key` — Seerr web UI → Settings → General → API Key.
- `poll_seconds` — how often recently-added/popular/requests are
  refreshed in the background. Search is live, not on this schedule — it
  only runs when someone's actually typing in the widget.

`plex` (optional):

```json
"plex": {
  "enabled": true,
  "base_url": "http://192.168.1.181:32400",
  "token": "...",
  "poll_seconds": 5
}
```

- `base_url` — the Plex Media Server's own address (not a separate
  service — same host:port your Plex apps already connect to).
- `token` — sign in to Plex's web app, open any item's **Get Info → View
  XML** link, and copy the `X-Plex-Token` value from the resulting URL.
  Plex has no separate API-key system; this is the same token the web app
  itself uses.
- `poll_seconds` — kept short (5s default) relative to the other
  integrations since Now Playing is meant to feel live, not like a
  periodic status check.

Run it:

```
python relay.py
```

Or install it as a background task via `setup-task.ps1` (registers a
scheduled task that runs `supervisor.ps1`, which keeps `relay.py` alive
and restarts it if it ever exits).

Verify:

```
curl http://127.0.0.1:8787/stats
curl http://127.0.0.1:8787/mc-stats
curl http://127.0.0.1:8787/qbit-stats
curl http://127.0.0.1:8787/seerr-stats
curl http://127.0.0.1:8787/plex-stats
```

## Endpoints

| Endpoint          | Method | Purpose |
|-------------------|--------|---------|
| `/stats`          | GET    | Full combined payload — TrueNAS, Minecraft, qBittorrent, Seerr, and Plex all in one response |
| `/mc-stats`       | GET    | Minecraft + Crafty payload for every configured server |
| `/mc-action`      | POST   | `{"action": "start"\|"stop"\|"restart"\|"backup", "server": "<name>"}` |
| `/qbit-stats`     | GET    | qBittorrent payload (torrents + transfer totals) |
| `/qbit-action`    | POST   | `{"hash": "<torrent hash>", "action": "pause"\|"resume"}` |
| `/seerr-stats`    | GET    | Seerr payload — recently added (movies/TV), popular (movies/TV), and recent requests with status |
| `/seerr-search`   | GET    | `?q=<query>` — live search proxy, not cached; hits Seerr on every call |
| `/seerr-request`  | POST   | `{"tmdbId": <id>, "mediaType": "movie"\|"tv"}` — submits a new request to Seerr |
| `/plex-stats`     | GET    | Plex payload — active sessions (with playback/transcode/bandwidth detail) and recently added |
| `/plex-stop`      | POST   | `{"sessionId": "<id>"}` — terminates ("kicks") an active playback session |
| `/update-app`, `/update-all` | POST | Trigger TrueNAS app upgrades |
| `/debug`          | GET    | Last raw payloads and last errors — check this first when a field is missing or wrong |

## Known quirks (found by testing against real installs, not just docs)

Crafty's documented API schema doesn't always match what it actually
returns:

- `mem` comes back as a raw number, but its unit isn't consistent across
  servers on the same Crafty instance — confirmed both kilobytes and bytes
  on two servers behind the very same Crafty install, with nothing in the
  response saying which. Trusting one unit blindly once produced a
  ~3000GB reading for a server actually using a few GB. The relay now
  picks whichever interpretation gives a plausible working-set size (a
  real Minecraft server is never in the hundreds/thousands of GB) rather
  than assuming KB.
- No real "last backup" timestamp exists in Crafty's API. What the widget
  shows as "backup requested Xm ago" is just this relay's own memory of
  the last time it asked Crafty to run a backup — it resets on relay
  restart, and isn't Crafty's actual backup history.
- No "update available" signal for the Minecraft executable itself. Crafty
  only exposes an `updating` boolean (an update is currently in progress),
  not whether one is available. Version comparison for vanilla updates
  checks the running version against Mojang's public release manifest.
  Mojang moved from the old 1.21.x scheme to a year-based 26.x scheme in
  2026 — the relay treats a mismatch between the two numbering styles as
  "unknown" rather than flagging a false positive.

qBittorrent quirks:

- Newer builds (5.0+) return `204 No Content` on login instead of the
  older `200 OK` + `"Ok."` body — the session cookie is the real signal of
  success either way, not the response body.
- A stopped/paused/errored torrent whose state string isn't in one of the
  known dl/up/queued sets now falls into a catch-all "stopped" bucket
  rather than being silently dropped from the widget.

Seerr quirks:

- `/api/v1/request` results only carry a `tmdbId` on the nested `media`
  object — no title, no poster. Getting either requires a follow-up call
  to `/api/v1/movie/{tmdbId}` or `/api/v1/tv/{tmdbId}`. The relay resolves
  and caches these in memory so a title isn't re-fetched from TMDB on
  every poll cycle.
- The `filter=pending` query param on `/api/v1/request` did **not**
  reliably exclude already-approved requests when tested against a live
  instance — the relay filters by the numeric `status` field client-side
  instead of trusting the query param.
- Request status and media status are two different numbers on two
  different objects: `request.status` (1 = pending approval, 2 = approved,
  3 = declined) and `request.media.status` (1/`null` = not in library,
  2 = pending, 3 = processing, 4 = partially available, 5 = available).
  The widget's status pills are driven by both — a declined request shows
  as declined regardless of what the (now irrelevant) media status says.

Plex quirks:

- Pagination (`X-Plex-Container-Size`/`-Start`) must be sent as HTTP
  **headers**, not query params — confirmed against a live instance where
  passing the size as a query param was silently ignored and Plex's
  default page size (50) came back instead of the requested limit.
- Whether a session is transcoding is more reliably read from
  `Media.Part.decision` (`"directplay"` / `"copy"` / `"transcode"`) than
  from checking whether a `TranscodeSession` element is present at all —
  confirmed a real direct-play session that had no `TranscodeSession`
  element, which is the expected/documented behavior, but relying on
  element presence alone as the *only* transcoding signal would miss
  edge cases `decision` catches directly.
- `Player.title` (the friendly device name, e.g. "Daniel's S25") is a far
  more useful "device" label than `Player.device` (a raw model string,
  e.g. "SM-S931B") — both exist on every session, the relay prefers the
  former.
- `/library/recentlyAdded` mixes top-level types: movies come back as
  flat `Video`/`movie` entries, but TV is reported at the **season**
  level as `Directory`/`season` entries (the whole season was added, not
  individual episodes) — these need `parentTitle`/`parentThumb` (the
  show) rather than their own bare "Season 1" title/thumb to be useful at
  a glance. Episode-level entries (seen in `/status/sessions`, for Now
  Playing) use `grandparentTitle`/`-Thumb` the same way, one level
  further up.
- `Session.location` (`"lan"` / `"wan"`) is the local-vs-remote signal,
  separate from `Session.bandwidth` (kbps) — both live on the same
  `Session` element, distinct from `Player` and `TranscodeSession`.
- `Session.id` — not `Player.machineIdentifier` — is the value
  `/status/sessions/terminate` expects as its `sessionId` param for
  stopping a stream. They happened to match in testing, but `Session.id`
  is the field the terminate endpoint actually documents.

## Widgets

### `mc-widget/`

Built for the Medium XENEON Edge slot (840×696px) — resized up from the
original Small (840×344) single-server layout to fit two independent
server panels stacked vertically.

Polls `/mc-stats` every 10 seconds and, per server, switches between two
layouts depending on state:

- **Online** — a dashboard: players as the hero stat with a sparkline,
  version/ping, CPU/RAM/world size, and the mod list (or "no mods
  detected" for vanilla).
- **Not online** — a centered diagnostic instead of a half-empty panel,
  distinguishing Offline, Starting…, Updating…, Crafty Error, and Relay
  Down, each with its own message and (where relevant) the full error text
  and last-seen time.

Start/Stop/Restart/Backup controls are per-panel (Stop and Restart require
a second tap to confirm), and the bottom-right timestamp flips to a STALE
warning if a panel hasn't heard from the relay in over 30 seconds, so a
frozen relay doesn't silently look identical to a genuinely offline
server. The widget's own version is shown in its top-right corner.

### `seerr-widget/`

A browsable Seerr front-end, not just a status readout — search, browse,
and tap to request, all from the touchscreen.

- **Movies** / **TV Series** tabs, each showing a "Recently Added" row
  stacked above a "Popular" row, posters pulled from TMDB via Seerr.
- **Requests** tab shows recent requests with a status pill per poster —
  Requested, Processing, Available, or Declined — and a live count on the
  tab itself (`Requests (3)`) for anything still open.
- Typing in the search bar debounces 350ms, then queries Seerr live
  (not cached) and shows results in place, tabs still visible underneath.
- Tapping the **+** on any poster (search results or Popular rows) submits
  a request through the relay — the widget never holds the Seerr API key.

### `plex-widget/`

A live Now Playing dashboard, not a request browser like `seerr-widget/`
— built for glancing at what's streaming right now and whether it's
straining the server.

- **Now Playing** — one card per active session: poster, title, "user ·
  device", a colored state dot (playing/paused/buffering), a LAN/WAN
  pill, a progress bar along the poster's bottom edge, and a mode pill
  underneath (Direct Play / Direct Stream / Transcoding — with a ⚡ when a
  transcode is using hardware acceleration) plus per-session bandwidth.
  A single active session gets a wider card centered in the row instead
  of a small card in an otherwise empty space; idle (no sessions) shows a
  compact centered "Nothing playing" state instead of a large blank area,
  and Recently Added takes the freed vertical space.
- A stop (⏻) button per session requires a second confirming tap before
  it actually terminates that playback session — same pattern as the
  Minecraft widget's Stop/Restart confirmation.
- **Recently Added** — same compact poster style as the Seerr widget,
  with a small badge per poster (`MOVIE`, `SHOW`, `SEASON n`, or
  `Sn · En` for episodes) so a TV-heavy library doesn't read as an
  undifferentiated poster wall.
- The top bar's subtext line doubles as an at-a-glance summary — `0
  streams` when idle, or `2 streams · 1 transcoding` when active.

Poll interval and the relay's port are both configurable from iCUE's
Personalization panel (Connection group) on every widget, without editing
the widget files.

Package any widget with:

```
icuewidget validate
icuewidget package
```

run from inside that widget's own folder.
