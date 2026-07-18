# TrueNAS + Minecraft iCUE Widget Relay

A local relay that polls a TrueNAS SCALE server and a Crafty-managed
Minecraft server, then serves the combined state as JSON over
`localhost` for a Corsair iCUE XENEON Edge widget to display and
control.

## Why a relay exists at all

iCUE widgets run in a sandboxed webview: they can't hold API keys
securely, can't speak TrueNAS's WebSocket/JSON-RPC API, and can't do
raw TCP (which is what pinging a Minecraft server actually requires).
The relay is a small always-on Python process on the same Windows PC
as iCUE that does all of that server-side, and exposes the result as
plain JSON the widget can `fetch()`.

```
┌─────────────┐        ┌───────────────┐        ┌────────────────────┐
│  TrueNAS     │◄──────►│               │◄──────►│  iCUE widget        │
│  (WebSocket) │        │   relay.py    │        │  (index.html,       │
├─────────────┤        │  (localhost:  │        │   polls /stats and   │
│  Minecraft   │◄──────►│   8787)       │◄──────►│   /mc-stats)         │
│  server      │  SLP   │               │  HTTP  │                     │
├─────────────┤        │               │        └────────────────────┘
│  Crafty      │◄──────►│               │
│  Controller  │  REST  └───────────────┘
└─────────────┘
```

## What it monitors and controls

**TrueNAS** — CPU/RAM/temp, network throughput, disk I/O, ZFS pool
health and capacity, app status and pending updates, alerts, backup
task status. Polled two ways: a realtime WebSocket subscription for
fast-moving stats, and a slower periodic poll (every
`slow_poll_seconds`) for anything that doesn't need per-second
freshness.

**Minecraft server** — queried directly over the Java Server List
Ping protocol (via [`mcstatus`](https://pypi.org/project/mcstatus/)),
*not* through Crafty. This is deliberate: the ping protocol needs no
credentials, and for a Forge server it's also where the mod list
comes from (mod ID + version, embedded in the ping response itself).
Reports online/offline, player count and names, version, MOTD,
latency, and — for modded servers — the full mod list.

**Crafty Controller** — used only for what the ping protocol can't
provide: starting/stopping/restarting the server, triggering a
backup, and reporting CPU/RAM/world size and whether the process is
mid-update. This needs a Crafty API token and is entirely optional —
if it's not configured, the relay still works for read-only
monitoring, the control buttons just don't appear in the widget.

## Setup

1. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

2. **Fill in `config.json`:**
   - `host`, `username`, `api_key` — your TrueNAS SCALE box and an API
     key generated from its UI.
   - `minecraft.host` / `minecraft.port` — where the Minecraft server
     itself listens (not Crafty's web UI port).
   - `minecraft.crafty` (optional, for start/stop/restart/backup):
     - `base_url` — Crafty's web UI address, **including the correct
       scheme** (`https://`, even on a non-default port — Crafty
       serves HTTPS with a self-signed cert by default, so also leave
       `verify_ssl: false` unless you've replaced that cert).
     - `server_id` — the UUID Crafty uses internally for this server
       (visible in the URL when viewing the server in Crafty's UI, or
       via `GET /api/v2/servers`).
     - `api_token` — Crafty web UI → your username → Profile → API
       Keys → Generate Key. The token's role needs **Commands**
       permission on the target server for start/stop/restart/backup
       to work.

3. **Run it:**
   ```
   python relay.py
   ```
   Or install it as a background task via `setup-task.ps1`
   (registers a scheduled task that runs `supervisor.ps1`, which
   keeps `relay.py` alive and restarts it if it ever exits).

4. **Verify:**
   ```
   curl http://127.0.0.1:8787/stats
   curl http://127.0.0.1:8787/mc-stats
   ```

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/stats` | GET | Full TrueNAS payload (the original widget's data source) |
| `/mc-stats` | GET | Minecraft + Crafty payload only |
| `/mc-action` | POST | `{"action": "start" \| "stop" \| "restart" \| "backup"}` |
| `/update-app`, `/update-all` | POST | Trigger TrueNAS app upgrades |
| `/debug` | GET | Last raw payloads and last errors — check this first when a field is missing or wrong |

## Known quirks (found by testing against a real Crafty install, not just its docs)

Crafty's documented API schema doesn't always match what it actually
returns:

- **`mem`** comes back as a raw number (kilobytes), not the
  pre-formatted string (`"42.2MB"`) its own docs example shows. The
  relay converts this to a human-readable string itself.
- **No real "last backup" timestamp** exists in Crafty's API. What
  the widget shows as "backup requested Xm ago" is just this relay's
  own memory of the last time *it* asked Crafty to run a backup — it
  resets on relay restart, and isn't Crafty's actual backup history.
- **No "update available" signal** for the Minecraft executable
  itself. Crafty only exposes an `updating` boolean (an update is
  *currently in progress*), not whether one is available.
- **Version comparison for vanilla updates** checks the running
  version against Mojang's public release manifest. Note that Mojang
  moved from the old `1.21.x` scheme to a year-based `26.x` scheme in
  2026 — the relay treats a mismatch between the two numbering styles
  as "unknown" rather than flagging a false positive.

## Widget

The `mc-widget/` folder (`manifest.json`, `index.html`, `icon.svg`)
is a Corsair iCUE custom widget built for the Small XENEON Edge slot
(840×344px). It polls `/mc-stats` every 10 seconds and renders:
status, players, version, latency, CPU/RAM/world size, mod list,
uptime, a players-online sparkline, a recent console line, and
Start/Stop/Restart/Backup controls (Stop and Restart require a
second tap to confirm).

Package it with:
```
icuewidget validate
icuewidget package
```
run from inside the `mc-widget` folder.
