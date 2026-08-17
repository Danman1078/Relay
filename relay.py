"""
Local relay: polls a TrueNAS SCALE server over its JSON-RPC/WebSocket API
and serves the results as JSON at http://127.0.0.1:<port>/stats for the
iCUE XENEON EDGE widget to fetch(). Keeps the TrueNAS API key off the
on-screen widget and sidesteps CORS (the widget only ever talks to
localhost, and this relay sends Access-Control-Allow-Origin: *).

Run: python relay.py   (edit config.json first)
Debug: http://127.0.0.1:<port>/debug shows the raw last payloads so
field-name mismatches (TrueNAS versions differ) can be spotted.
"""

import http.server
import json
import re
import socketserver
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime
from pathlib import Path

from truenas_api_client import APIKeyAuthMech, Client
from mcstatus import JavaServer

CONFIG = json.loads((Path(__file__).with_name("config.json")).read_text())

HOST = CONFIG["host"]
USERNAME = CONFIG["username"]
API_KEY = CONFIG["api_key"]
PORT = CONFIG.get("port", 8787)
SLOW_POLL_SECONDS = CONFIG.get("slow_poll_seconds", 30)
POOLS_FILTER = set(CONFIG.get("pools") or [])
SHOW = CONFIG.get("show") or {}
USE_SSL = CONFIG.get("use_ssl", True)
VERIFY_SSL = CONFIG.get("verify_ssl", False)
TRUENAS_URI = f"{'wss' if USE_SSL else 'ws'}://{HOST}/api/current"

# Minecraft servers (e.g. Crafty-managed FTB/Forge servers), each polled
# directly over the Java Server List Ping protocol -- no Crafty API token
# needed for this part. Crafty's own control API (below) is a separate,
# optional concern per server.
#
# Each server now runs on its own host:port and can be live/joinable at the
# same time as the others (this used to assume only one could ever be
# running, sharing one port -- that's no longer the case, so each entry is
# fully independent: its own ping target, its own Crafty server_id, its own
# poll thread, its own slot in STATE).
#
# config.json shape:
#   "minecraft": {
#     "poll_seconds": 15, "mod_list_cap": 400,
#     "crafty": {"base_url": ..., "api_token": "...", "verify_ssl": false},
#     "servers": [
#       {"name": "Direwolf20", "host": "192.168.1.181", "port": 25565, "crafty_server_id": "..."},
#       {"name": "Server 2",   "host": "192.168.1.181", "port": 25566, "crafty_server_id": "..."}
#     ]
#   }
MC_CFG = CONFIG.get("minecraft") or {}
MC_POLL_SECONDS = MC_CFG.get("poll_seconds", 15)
MC_MOD_LIST_CAP = MC_CFG.get("mod_list_cap", 400)  # keep payload sane on huge modpacks

MC_SERVERS = list(MC_CFG.get("servers") or [])
if not MC_SERVERS and MC_CFG.get("enabled"):
    # old single-server config -- treat it as a one-entry list. Crafty's
    # server_id used to live under minecraft.crafty.server_id; carry that
    # over too if present.
    MC_SERVERS = [{
        "name": "Server",
        "host": MC_CFG.get("host", HOST),
        "port": MC_CFG.get("port", 25565),
        "crafty_server_id": (MC_CFG.get("crafty") or {}).get("server_id", ""),
    }]

MC_ENABLED = len(MC_SERVERS) > 0

# Crafty's own control API -- only needed for start/stop/restart/backup,
# since the SLP ping above is read-only. Get each server_id from Crafty's
# URL when viewing that server (or GET /api/v2/servers), and api_token from
# Profile -> API Keys -> Generate Key in the Crafty web UI. One shared
# token/base_url covers all servers; each server's own crafty_server_id
# (in MC_SERVERS above) says which Crafty-side server it maps to.
CRAFTY_CFG = MC_CFG.get("crafty") or {}
CRAFTY_BASE_URL = CRAFTY_CFG.get("base_url", "https://192.168.1.181:30146").rstrip("/")
CRAFTY_API_TOKEN = CRAFTY_CFG.get("api_token", "")
CRAFTY_VERIFY_SSL = CRAFTY_CFG.get("verify_ssl", False)
CRAFTY_ENABLED = bool(CRAFTY_API_TOKEN) and any(s.get("crafty_server_id") for s in MC_SERVERS)

# qBittorrent Web API -- polled via cookie-session auth (there's no API-key
# auth in qBittorrent's WebUI API, only username/password -> session cookie).
# Doing this here rather than in the browser sidesteps two dead ends: the
# widget itself has no CORS-safe way to reach a cross-origin host, and even
# with CORS headers added on the qBittorrent side, the session cookie
# wouldn't survive the browser's SameSite policy on a cross-site fetch.
# A plain urllib client has neither restriction.
QBIT_CFG = CONFIG.get("qbittorrent") or {}
QBIT_ENABLED = QBIT_CFG.get("enabled", False)
QBIT_BASE_URL = QBIT_CFG.get("base_url", "http://127.0.0.1:8080").rstrip("/")
QBIT_USERNAME = QBIT_CFG.get("username", "")
QBIT_PASSWORD = QBIT_CFG.get("password", "")
QBIT_POLL_SECONDS = QBIT_CFG.get("poll_seconds", 3)
QBIT_MAX_ROWS = QBIT_CFG.get("max_rows", 5)
QBIT_VERIFY_SSL = QBIT_CFG.get("verify_ssl", True)
QBIT_BAN_BACKOFF_SECONDS = QBIT_CFG.get("ban_backoff_seconds", 600)

QBIT_DL_STATES = {"downloading", "metaDL", "allocating", "checkingDL", "forcedDL"}
QBIT_UP_STATES = {"uploading", "checkingUP", "forcedUP"}
# Queued/stalled: still incomplete and still *intends* to run (the client
# will resume it automatically once conditions allow) -- distinct from an
# explicit stop/pause, which is why this is its own group.
QBIT_QUEUED_STATES = {"queuedDL", "stalledDL", "queuedUP", "stalledUP"}

# Seerr (media request app) -- polled via its REST API using an API key
# (Settings -> General -> API Key in Seerr). Same urllib-based approach as
# qBittorrent above, no extra dependency needed.
# config.json shape:
#   "seerr": {
#     "enabled": true, "base_url": "http://192.168.1.181:30357",
#     "api_key": "...", "poll_seconds": 30
#   }
SEERR_CFG = CONFIG.get("seerr") or {}
SEERR_ENABLED = SEERR_CFG.get("enabled", False)
SEERR_BASE_URL = SEERR_CFG.get("base_url", "http://192.168.1.181:30357").rstrip("/")
SEERR_API_KEY = SEERR_CFG.get("api_key", "")
SEERR_POLL_SECONDS = SEERR_CFG.get("poll_seconds", 30)
SEERR_REQUEST_STATUS_PENDING = 1

# Plex Media Server -- polled via X-Plex-Token header auth (Plex has no
# separate API-key system; the token is generated once via a Plex sign-in
# and used directly). Reports live playback sessions (with transcode
# status/hardware acceleration) and recently added media. Field names below
# are based on Plex's documented XML/JSON API shape -- not yet confirmed
# against a real response from this instance, so treat this as a first
# pass to test and adjust, same as the early Seerr integration was.
# config.json shape:
#   "plex": {
#     "enabled": true, "base_url": "http://192.168.1.181:32400",
#     "token": "...", "poll_seconds": 5
#   }
PLEX_CFG = CONFIG.get("plex") or {}
PLEX_ENABLED = PLEX_CFG.get("enabled", False)
PLEX_BASE_URL = PLEX_CFG.get("base_url", "http://192.168.1.181:32400").rstrip("/")
PLEX_TOKEN = PLEX_CFG.get("token", "")
PLEX_POLL_SECONDS = PLEX_CFG.get("poll_seconds", 5)


def qbit_classify(state):
    """Returns "dl" | "up" | "paused" | "stopped". Deliberately a catch-all
    (anything not recognized falls into "stopped") rather than exact-set
    membership only -- an earlier version silently dropped any torrent
    whose state string wasn't in one of the known sets, which is why a
    manually-stopped torrent could vanish from the widget entirely instead
    of showing up as stopped. Covers pausedDL/pausedUP (older qBittorrent),
    stoppedDL/stoppedUP/stopped (5.0+), and error/missingFiles/unknown/
    moving/checkingResumeData as a safety net."""
    if state in QBIT_DL_STATES:
        return "dl"
    if state in QBIT_UP_STATES:
        return "up"
    if state in QBIT_QUEUED_STATES:
        return "paused"
    return "stopped"



# Update-available check: compares the running MC version against Mojang's
# public release manifest. Vanilla-only signal (a modpack "update" is really
# about mod versions, which this can't see) but still useful as a heads-up.
VERSION_CHECK_SECONDS = 6 * 3600


# TrueNAS < 26 doesn't support SCRAM auth for API keys, so we authenticate
# with PLAIN (raw key, protected only by the wss:// TLS transport).
def make_client():
    c = Client(uri=TRUENAS_URI, verify_ssl=VERIFY_SSL)
    c.login_with_api_key(USERNAME, API_KEY, auth_mechanism=APIKeyAuthMech.PLAIN)
    return c


UPGRADE_LOCK = threading.Lock()
UPGRADING = set()          # app names with an upgrade job currently running
UPGRADE_RESULTS = {}       # app name -> {"status": "success"|"error", "message": ...}


def run_upgrade(app_name):
    """Runs app.upgrade on its own connection/thread; app.upgrade is a job so
    this blocks until TrueNAS finishes the upgrade (can take minutes)."""
    with UPGRADE_LOCK:
        if app_name in UPGRADING:
            return
        UPGRADING.add(app_name)
    try:
        with make_client() as c:
            c.call("app.upgrade", app_name, {"app_version": "latest"}, job=True, timeout=1800)
        with UPGRADE_LOCK:
            UPGRADE_RESULTS[app_name] = {"status": "success"}
    except Exception as exc:
        with UPGRADE_LOCK:
            UPGRADE_RESULTS[app_name] = {"status": "error", "message": repr(exc)}
    finally:
        with UPGRADE_LOCK:
            UPGRADING.discard(app_name)


STATE_LOCK = threading.Lock()
STATE = {
    "show": SHOW,
    "cpu_percent": None,
    "cpu_temp": None,
    "mem_percent": None,
    "mem_used_gb": None,
    "mem_total_gb": None,
    "mem_free_gb": None,
    "mem_other_gb": None,
    "arc_gb": None,
    "net_rx_mbps": None,
    "net_tx_mbps": None,
    "disk_read_mbs": None,
    "disk_write_mbs": None,
    "pools": [],
    "apps": [],
    "app_updates": None,
    "app_update_names": [],
    "alerts_warning": None,
    "alerts_critical": None,
    "alerts": [],
    "hostname": None,
    "tn_version": None,
    "uptime_seconds": None,
    "iface_name": None,
    "ip_address": None,
    "link_up": None,
    "backup_tasks": [],
    "updated_at": None,
    "connected": False,
    "minecraft_enabled": MC_ENABLED,
    "minecraft_servers": [
        {
            "name": s.get("name", "Server"),
            "host": s.get("host", HOST),
            "port": s.get("port", 25565),
            "online": False,
            "players_online": None,
            "players_max": None,
            "player_names": [],
            "version": None,
            "protocol": None,
            "motd": None,
            "latency_ms": None,
            "mod_loader": None,          # "forge" | "vanilla" | None (unknown/offline)
            "mod_count": None,
            "mod_list": [],              # [{"id": "...", "version": "..."}]
            "mod_list_truncated": False,
            "last_online_at": None,
            "updated_at": None,
            "error": None,
            "mc_latest_release": None,
            "update_available": None,    # None = unknown/not checked yet, True/False once checked
            "empty_since": None,         # epoch since players_online last hit 0 while running
            "console_tail": [],          # last few lines from Crafty's server log
            "player_history": [],        # rolling players_online samples, ~last hour
            # Crafty control state -- only populated if this server has a
            # crafty_server_id configured (start/stop/restart/backup, plus
            # CPU/RAM/world/uptime, none of which the ping protocol exposes)
            "crafty_enabled": CRAFTY_ENABLED and bool(s.get("crafty_server_id")),
            "crafty_server_id": s.get("crafty_server_id", ""),
            "crafty_running": None,      # True/False/None (None = unknown/not configured)
            "crafty_starting": None,     # waiting_start -- process is up, MC hasn't finished loading
            "crafty_updating": None,
            "crafty_action_pending": None,  # "start" | "stop" | "restart" | "backup" | None
            "crafty_error": None,
            "crafty_cpu_percent": None,
            "crafty_mem": None,           # human string, e.g. "1.06GB" -- converted from Crafty's raw KB number
            "crafty_mem_percent": None,
            "world_name": None,
            "world_size": None,           # e.g. "128MB" -- Crafty's own formatted string
            "started_at": None,           # epoch, parsed from Crafty's own "started" field
            "last_backup_triggered_at": None,  # in-memory only -- resets on relay restart,
                                                # this is "last backup we asked for", not
                                                # Crafty's own backup history (no API for that)
        }
        for s in MC_SERVERS
    ],
    "qbittorrent": {
        "enabled": QBIT_ENABLED,
        "online": False,
        "dl_count": None,
        "up_count": None,
        "paused_count": None,
        "stopped_count": None,
        "dl_speed": None,             # bytes/sec
        "up_speed": None,             # bytes/sec
        "dl_data": None,              # session total, bytes
        "up_data": None,              # session total, bytes
        "total_count": None,
        "torrents": [],               # [{hash, name, state, group, progress, dlspeed, upspeed, eta}]
        "updated_at": None,
        "error": None,
    },
    "seerr": {
        "enabled": SEERR_ENABLED,
        "online": False,
        "openRequestsCount": None,
        "requests": [],      # [{id, tmdbId, title, poster, type, requestedBy, status, requestStatus}]
        "recentMovies": [],  # [{id, tmdbId, title, poster, type, status}]
        "recentTV": [],
        "popularMovies": [], # [{tmdbId, title, poster, type, year, status}]
        "popularTV": [],
        "updated_at": None,
        "error": None,
    },
    "plex": {
        "enabled": PLEX_ENABLED,
        "online": False,
        "sessionCount": None,
        "transcodingCount": None,
        "sessions": [],       # [{title, type, thumb, user, state, progress, transcoding, hwTranscode, quality, bandwidthKbps, location, device, sessionId}]
        "recentlyAdded": [],  # [{title, type, thumb, addedAt}]
        "updated_at": None,
        "error": None,
    },
}
DEBUG = {"last_realtime_raw": None, "last_slow_poll_error": None, "last_realtime_error": None,
         "last_mc_error": None, "last_qbit_error": None, "last_seerr_error": None, "last_plex_error": None}


def bytes_to_gb(n):
    return round(n / (1024 ** 3), 1) if n is not None else None


def update_realtime(event):
    """Mapping of TrueNAS's reporting.realtime payload (confirmed against a live
    25.04.2.6 server via the /debug endpoint)."""
    DEBUG["last_realtime_raw"] = event
    fields = event.get("fields", event) if isinstance(event, dict) else {}

    cpu_agg = ((fields.get("cpu") or {}).get("cpu")) or {}
    cpu_percent = cpu_agg.get("usage")
    cpu_temp = cpu_agg.get("temp")

    mem = fields.get("memory") or {}
    mem_total = mem.get("physical_memory_total")
    mem_avail = mem.get("physical_memory_available")
    arc_size = mem.get("arc_size")
    mem_percent = None
    if mem_total and mem_avail is not None:
        mem_percent = round((1 - mem_avail / mem_total) * 100, 1)

    disks = fields.get("disks") or {}
    disk_read = disks.get("read_bytes")   # bytes/sec
    disk_write = disks.get("write_bytes")  # bytes/sec

    interfaces = fields.get("interfaces") or {}
    rx_total = tx_total = 0
    have_net = False
    for iface in interfaces.values():
        if not isinstance(iface, dict):
            continue
        rx = iface.get("received_bytes_rate")
        tx = iface.get("sent_bytes_rate")
        if rx is not None:
            rx_total += rx
            have_net = True
        if tx is not None:
            tx_total += tx
            have_net = True

    with STATE_LOCK:
        if cpu_percent is not None:
            STATE["cpu_percent"] = round(cpu_percent, 1)
        if cpu_temp is not None:
            STATE["cpu_temp"] = round(cpu_temp, 1)
        if mem_percent is not None:
            STATE["mem_percent"] = mem_percent
            STATE["mem_used_gb"] = bytes_to_gb(mem_total - mem_avail)
            STATE["mem_total_gb"] = bytes_to_gb(mem_total)
            STATE["mem_free_gb"] = bytes_to_gb(mem_avail)
        if arc_size is not None:
            STATE["arc_gb"] = bytes_to_gb(arc_size)
        if mem_percent is not None and arc_size is not None:
            other = max(0, (mem_total - mem_avail) - arc_size)
            STATE["mem_other_gb"] = bytes_to_gb(other)
        if disk_read is not None:
            STATE["disk_read_mbs"] = round(disk_read / 1_000_000, 1)
        if disk_write is not None:
            STATE["disk_write_mbs"] = round(disk_write / 1_000_000, 1)
        if have_net:
            STATE["net_rx_mbps"] = round(rx_total * 8 / 1_000_000, 2)
            STATE["net_tx_mbps"] = round(tx_total * 8 / 1_000_000, 2)
        STATE["updated_at"] = time.time()
        STATE["connected"] = True


def realtime_thread():
    while True:
        try:
            with make_client() as c:
                c.subscribe("reporting.realtime", lambda mtype, **msg: update_realtime(msg))
                while True:
                    time.sleep(1)
        except Exception as exc:
            DEBUG["last_realtime_error"] = repr(exc)
            with STATE_LOCK:
                STATE["connected"] = False
            time.sleep(5)


def pool_scan_label(pool):
    """Return e.g. 'SCRUB 42%' while a scrub/resilver runs, else None."""
    scan = pool.get("scan") or {}
    if scan.get("state") == "SCANNING":
        func = scan.get("function") or "SCAN"
        pct = scan.get("percentage")
        if pct is not None:
            return f"{func} {pct:.0f}%"
        return func
    return None


def pool_scrub_info(pool):
    """Last scrub date (YYYY-MM-DD) and duration string, or (None, None)."""
    scan = pool.get("scan") or {}
    if scan.get("function") != "SCRUB" or scan.get("state") != "FINISHED":
        return None, None
    start = scan.get("start_time")
    end = scan.get("end_time")
    date_str = str(end)[:10] if end else None
    duration_str = None
    try:
        secs = (end - start).total_seconds()
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        duration_str = f"{h}h {m}m"
    except Exception:
        pass
    return date_str, duration_str


def pool_topology_summary(pool):
    """e.g. '1 x RAIDZ1 | 3 wide', plus (disks_total, disks_with_errors)."""
    topology = pool.get("topology") or {}
    vdevs = topology.get("data") or []

    disks_total = 0
    disks_error = 0

    def walk(node):
        nonlocal disks_total, disks_error
        if node.get("type") == "DISK":
            disks_total += 1
            stats = node.get("stats") or {}
            if (stats.get("read_errors") or 0) + (stats.get("write_errors") or 0) + (stats.get("checksum_errors") or 0) > 0:
                disks_error += 1
        for child in node.get("children") or []:
            walk(child)

    for group_name in ("data", "cache", "log", "spare", "special", "dedup"):
        for vdev in topology.get(group_name) or []:
            walk(vdev)

    if vdevs:
        vdev_type = vdevs[0].get("type", "?")
        width = len(vdevs[0].get("children") or []) or 1
        uniform = all(v.get("type") == vdev_type and len(v.get("children") or []) == width for v in vdevs)
        if uniform:
            topo_str = f"{len(vdevs)} x {vdev_type} | {width} wide"
        else:
            topo_str = f"{len(vdevs)} vdevs"
    else:
        topo_str = "?"

    return topo_str, disks_total, disks_error


_CRAFTY_SSL_CTX = ssl.create_default_context()
if not CRAFTY_VERIFY_SSL:
    _CRAFTY_SSL_CTX.check_hostname = False
    _CRAFTY_SSL_CTX.verify_mode = ssl.CERT_NONE


def crafty_request(path, method="GET", body=None, timeout=8):
    """Minimal REST call against Crafty's v2 API. Raises on any failure --
    callers catch and record the error rather than letting it kill a thread."""
    url = f"{CRAFTY_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {CRAFTY_API_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout, context=_CRAFTY_SSL_CTX) as resp:
        return json.loads(resp.read())


_MEM_PLAUSIBLE_MAX_GB = 128  # no realistic home-server Minecraft container exceeds this


def _format_mem_raw(raw):
    """Crafty's 'mem' field is a raw number, not the pre-formatted string its
    own docs example implies. The original assumption here was "it's always
    kilobytes" (confirmed empirically once: 1114112.0 for a ~1GB working
    set). That assumption turned out to be wrong: on a real multi-server
    Crafty instance, one server's raw value really is KB while the OTHER
    server's raw value is bytes -- same Crafty install, same API, different
    units, and nothing in the response says which. Interpreting bytes as KB
    is exactly what produced a ~3000GB reading for a server actually using
    a few GB.

    There's no field to disambiguate, so use plausibility instead: a real
    Minecraft server's working set is never in the hundreds/thousands of
    GB. Try the KB interpretation first (the more common case observed);
    if that comes out absurd, the raw value must have been bytes instead."""
    if raw is None:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return str(raw)  # unexpected shape -- show it rather than hide it
    if n <= 0:
        return "0MB"
    kb_gb = n / 1024 / 1024
    gb = kb_gb if kb_gb <= _MEM_PLAUSIBLE_MAX_GB else n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f}GB"
    return f"{gb * 1024:.0f}MB"


def _format_mem_from_percent(mem_percent):
    """Extra cross-check when available: derive used RAM from Crafty's
    mem_percent (of total host RAM) combined with the host's actual total
    RAM, which the relay may know from TrueNAS's own reporting.realtime
    feed. More precise than the raw-value guess above when it's available,
    but that feed isn't always populated in every deployment, so this
    returns None (letting the caller fall back to _format_mem_raw) rather
    than being relied on as the only path."""
    if mem_percent is None:
        return None
    with STATE_LOCK:
        total_gb = STATE.get("mem_total_gb")
    if not total_gb:
        return None
    used_gb = total_gb * (mem_percent / 100)
    if used_gb >= 1:
        return f"{used_gb:.2f}GB"
    return f"{used_gb * 1024:.0f}MB"


def _find_server_state(server_name):
    """Returns the STATE dict for a configured server by name, or None."""
    for entry in STATE["minecraft_servers"]:
        if entry["name"] == server_name:
            return entry
    return None


def _find_crafty_id(server_name):
    for s in MC_SERVERS:
        if s.get("name", "Server") == server_name and s.get("crafty_server_id"):
            return s["crafty_server_id"]
    return None


def crafty_get_stats(server_name):
    """GET /servers/{id}/stats for one specific server -- each configured
    server has its own crafty_server_id now, so there's no more guessing
    which one is 'active'; this just refreshes that one server's own
    CPU/RAM/world/uptime fields, which the ping protocol can't provide."""
    sid = _find_crafty_id(server_name)
    if not sid:
        return
    payload = crafty_request(f"/api/v2/servers/{sid}/stats")
    data = payload.get("data") or {}

    started_at = None
    started_raw = data.get("started") or payload.get("started")
    if started_raw:
        try:
            started_at = datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            started_at = None

    mem_percent = data.get("mem_percent")
    mem_str = _format_mem_from_percent(mem_percent)
    if mem_str is None:
        mem_str = _format_mem_raw(data.get("mem"))

    with STATE_LOCK:
        entry = _find_server_state(server_name)
        if entry is None:
            return
        entry["crafty_running"] = bool(data.get("running"))
        entry["crafty_starting"] = bool(data.get("waiting_start"))
        entry["crafty_updating"] = bool(data.get("updating"))
        entry["crafty_cpu_percent"] = data.get("cpu")
        entry["crafty_mem"] = mem_str
        entry["crafty_mem_percent"] = mem_percent
        entry["world_name"] = data.get("world_name")
        entry["world_size"] = data.get("world_size")
        entry["started_at"] = started_at
        entry["crafty_error"] = None


def crafty_get_logs(server_name):
    """GET /servers/{id}/logs for one specific server. Crafty can return a
    large buffer (its own max_log_lines config, often several hundred), so
    this keeps only the last few lines."""
    sid = _find_crafty_id(server_name)
    if not sid:
        return
    payload = crafty_request(f"/api/v2/servers/{sid}/logs")
    lines = payload.get("data") or []
    with STATE_LOCK:
        entry = _find_server_state(server_name)
        if entry is not None:
            entry["console_tail"] = lines[-6:]


def crafty_send_action(server_name, action):
    """POST /servers/{id}/action/{start_server|stop_server|restart_server|
    backup_server} for one specific server. Runs on its own thread so the
    HTTP handler returns immediately; the widget polls /mc-stats afterwards
    to see the state change land."""
    with STATE_LOCK:
        entry = _find_server_state(server_name)
        if entry is not None:
            entry["crafty_action_pending"] = action

    try:
        sid = _find_crafty_id(server_name)
        if not sid:
            raise ValueError(f"no crafty_server_id configured for {server_name!r}")

        crafty_request(f"/api/v2/servers/{sid}/action/{action}_server", method="POST")
        with STATE_LOCK:
            if entry is not None:
                entry["crafty_error"] = None
                if action == "backup":
                    entry["last_backup_triggered_at"] = time.time()
    except Exception as exc:
        with STATE_LOCK:
            if entry is not None:
                entry["crafty_error"] = str(exc)
    finally:
        with STATE_LOCK:
            if entry is not None:
                entry["crafty_action_pending"] = None
        try:
            crafty_get_stats(server_name)
        except Exception:
            pass


_QBIT_COOKIE_JAR = http.cookiejar.CookieJar()
if QBIT_BASE_URL.startswith("https") and not QBIT_VERIFY_SSL:
    _qbit_ctx = ssl.create_default_context()
    _qbit_ctx.check_hostname = False
    _qbit_ctx.verify_mode = ssl.CERT_NONE
    _QBIT_OPENER = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_QBIT_COOKIE_JAR),
        urllib.request.HTTPSHandler(context=_qbit_ctx),
    )
else:
    _QBIT_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_QBIT_COOKIE_JAR))
_QBIT_LOGGED_IN = False


def qbit_request(path, method="GET", form_body=None, timeout=8):
    """Minimal client for qBittorrent's WebUI API. Session cookie is held in
    _QBIT_COOKIE_JAR across calls -- same opener every time, like a browser
    would, but without any of a browser's CORS/SameSite restrictions."""
    url = f"{QBIT_BASE_URL}{path}"
    data = form_body.encode() if form_body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Referer", QBIT_BASE_URL)  # some qBittorrent builds check this even with CSRF off
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _QBIT_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        # qBittorrent returns 403 on bad/expired session -- callers check
        # for that specifically, so surface it as a normal (status, body)
        # pair rather than letting it propagate as an exception.
        return exc.code, exc.read().decode(errors="replace")


def qbit_login():
    global _QBIT_LOGGED_IN
    if not QBIT_USERNAME:
        _QBIT_LOGGED_IN = True  # e.g. localhost-bypass or subnet-whitelist auth -- no login call needed
        return True
    try:
        body = f"username={urllib.parse.quote(QBIT_USERNAME)}&password={urllib.parse.quote(QBIT_PASSWORD)}"
        status, text = qbit_request("/api/v2/auth/login", method="POST", form_body=body)
        # Older qBittorrent: 200 with body "Ok.". Newer builds (confirmed via
        # curl -v against a real instance): 204 No Content with an empty
        # body, but a valid session cookie is still set either way -- that
        # cookie, not the body text, is the real signal of success.
        _QBIT_LOGGED_IN = status in (200, 204) and text.strip() in ("", "Ok.")
        if not _QBIT_LOGGED_IN:
            body = text.strip()
            if status == 403 and "banned" in body.lower():
                DEBUG["last_qbit_error"] = body
                DEBUG["last_qbit_backoff_until"] = time.time() + QBIT_BAN_BACKOFF_SECONDS
            else:
                DEBUG["last_qbit_error"] = f"login HTTP {status}: {body[:200]!r}"
    except Exception as exc:
        DEBUG["last_qbit_error"] = repr(exc)
        _QBIT_LOGGED_IN = False
    return _QBIT_LOGGED_IN


def qbit_poll_thread():
    """Polls qBittorrent's WebUI API for torrent list + transfer totals.
    Logs in lazily on first use and again if a call ever comes back 403
    (session expired/qBittorrent restarted)."""
    global _QBIT_LOGGED_IN
    while True:
        try:
            backoff_until = DEBUG.get("last_qbit_backoff_until", 0)
            if backoff_until > time.time():
                remaining = int(backoff_until - time.time())
                with STATE_LOCK:
                    STATE["qbittorrent"]["online"] = False
                    STATE["qbittorrent"]["error"] = f"IP temporarily banned by qBittorrent; retrying in {remaining}s"
                    STATE["qbittorrent"]["updated_at"] = time.time()
                time.sleep(min(QBIT_POLL_SECONDS,5))
                continue

            if not _QBIT_LOGGED_IN and not qbit_login():
                with STATE_LOCK:
                    STATE["qbittorrent"]["online"] = False
                    err = DEBUG.get("last_qbit_error","")
                    if "banned" in err.lower():
                        STATE["qbittorrent"]["error"] = "IP temporarily banned by qBittorrent"
                    else:
                        STATE["qbittorrent"]["error"] = "login failed -- check qbittorrent.username/password in config.json"
                    STATE["qbittorrent"]["updated_at"] = time.time()
                time.sleep(QBIT_POLL_SECONDS)
                continue

            try:
                status, torrents_raw = qbit_request("/api/v2/torrents/info")
                if status == 403:
                    _QBIT_LOGGED_IN = False
                    raise RuntimeError("session expired (403) -- will re-login next cycle")
                torrents = json.loads(torrents_raw)

                status2, transfer_raw = qbit_request("/api/v2/transfer/info")
                if status2 == 403:
                    _QBIT_LOGGED_IN = False
                    raise RuntimeError("session expired (403) -- will re-login next cycle")
                transfer = json.loads(transfer_raw)
            except urllib.error.URLError as exc:
                raise RuntimeError(f"can't reach {QBIT_BASE_URL}: {exc}")

            dl, up, paused, stopped = [], [], [], []
            for t in torrents:
                group = qbit_classify(t.get("state"))
                {"dl": dl, "up": up, "paused": paused, "stopped": stopped}[group].append(t)
            dl.sort(key=lambda t: t.get("dlspeed", 0), reverse=True)
            up.sort(key=lambda t: t.get("upspeed", 0), reverse=True)
            paused.sort(key=lambda t: (t.get("name") or "").lower())
            stopped.sort(key=lambda t: (t.get("name") or "").lower())

            def _row(t, group):
                return {
                    "hash": t.get("hash"), "name": t.get("name"), "state": t.get("state"), "group": group,
                    "progress": t.get("progress"), "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"),
                    "eta": t.get("eta"),
                }

            # Row budget priority: actively downloading and explicitly-stopped
            # torrents are what a person is most likely to want to check on or
            # act on (tap to resume); queued/stalled next; already-seeding
            # torrents last, since those need no attention.
            rows = [_row(t, "dl") for t in dl[:QBIT_MAX_ROWS]]
            remaining = max(0, QBIT_MAX_ROWS - len(rows))
            rows += [_row(t, "stopped") for t in stopped[:remaining]]
            remaining = max(0, QBIT_MAX_ROWS - len(rows))
            rows += [_row(t, "paused") for t in paused[:remaining]]
            remaining = max(0, QBIT_MAX_ROWS - len(rows))
            rows += [_row(t, "up") for t in up[:remaining]]

            with STATE_LOCK:
                q = STATE["qbittorrent"]
                q["online"] = True
                q["dl_count"] = len(dl)
                q["up_count"] = len(up)
                q["paused_count"] = len(paused)
                q["stopped_count"] = len(stopped)
                q["dl_speed"] = transfer.get("dl_info_speed")
                q["up_speed"] = transfer.get("up_info_speed")
                q["dl_data"] = transfer.get("dl_info_data")
                q["up_data"] = transfer.get("up_info_data")
                q["total_count"] = len(torrents)
                q["torrents"] = rows
                q["updated_at"] = time.time()
                q["error"] = None
        except Exception as exc:
            DEBUG["last_qbit_error"] = repr(exc)
            with STATE_LOCK:
                STATE["qbittorrent"]["online"] = False
                STATE["qbittorrent"]["error"] = str(exc)
                STATE["qbittorrent"]["updated_at"] = time.time()

        time.sleep(QBIT_POLL_SECONDS)


def qbit_torrent_action(torrent_hash, action):
    """action is "pause" or "resume". qBittorrent 5.0+ (WebAPI v2.11+)
    renamed these to /torrents/stop and /torrents/start; older builds only
    have /torrents/pause and /torrents/resume. Try the new path first and
    fall back to the old one on a 404, so this works either way without
    needing to know the qBittorrent version up front."""
    if action not in ("pause", "resume"):
        raise ValueError(f"unknown action: {action}")

    new_path = "/api/v2/torrents/stop" if action == "pause" else "/api/v2/torrents/start"
    old_path = f"/api/v2/torrents/{action}"
    body = f"hashes={urllib.parse.quote(torrent_hash)}"

    if not _QBIT_LOGGED_IN and not qbit_login():
        raise RuntimeError("not logged in to qBittorrent")

    status, text = qbit_request(new_path, method="POST", form_body=body)
    if status == 404:
        status, text = qbit_request(old_path, method="POST", form_body=body)
    if status == 403:
        # session expired mid-action -- one retry after a fresh login
        if qbit_login():
            status, text = qbit_request(new_path, method="POST", form_body=body)
            if status == 404:
                status, text = qbit_request(old_path, method="POST", form_body=body)
    if status not in (200, 204):
        raise RuntimeError(f"qBittorrent returned HTTP {status}: {text[:200]!r}")


# Small in-memory cache so repeated polls don't re-resolve the same title
# from TMDB every cycle -- Seerr's /request and /media endpoints only return
# tmdbId on the nested media object, not a title or poster.
_SEERR_DETAILS_CACHE = {}

TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w300"


def seerr_request(path, params=None, method="GET", json_body=None, timeout=8):
    """Minimal client for Seerr's REST API, X-Api-Key header auth. Mirrors
    qbit_request()'s shape but Seerr needs no session/cookie handling."""
    url = f"{SEERR_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", SEERR_API_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def seerr_resolve_details(tmdb_id, media_type):
    """Look up title + poster path for an item that only has a tmdbId
    (the case for /request and /media results). Returns {"title", "poster"}."""
    cache_key = (media_type, tmdb_id)
    if cache_key in _SEERR_DETAILS_CACHE:
        return _SEERR_DETAILS_CACHE[cache_key]
    path = "movie" if media_type == "movie" else "tv"
    try:
        status, text = seerr_request(f"/api/v1/{path}/{tmdb_id}")
        data = json.loads(text)
        details = {
            "title": data.get("title") or data.get("name") or f"tmdb:{tmdb_id}",
            "poster": data.get("posterPath"),
        }
    except Exception:
        details = {"title": f"tmdb:{tmdb_id}", "poster": None}
    _SEERR_DETAILS_CACHE[cache_key] = details
    return details


def seerr_search(query):
    """Live search proxy -- not cached in STATE, called on-demand from the
    widget as the person types. TMDB multi-search results already carry
    title/posterPath/mediaInfo directly, no follow-up lookup needed."""
    status, text = seerr_request("/api/v1/search", {"query": query, "page": 1})
    data = json.loads(text)
    results = []
    for r in data.get("results", []):
        media_type = r.get("mediaType")
        if media_type not in ("movie", "tv"):
            continue  # skip "person" results
        media_info = r.get("mediaInfo") or {}
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        results.append({
            "tmdbId": r.get("id"),
            "title": r.get("title") or r.get("name"),
            "type": media_type,
            "poster": r.get("posterPath"),
            "year": date[:4],
            # Seerr/Overseerr MediaStatus: None/1=not in library, 2=pending,
            # 3=processing, 4=partially available, 5=available
            "status": media_info.get("status"),
        })
        if len(results) >= 20:
            break
    return results


def seerr_discover(media_type, take=15):
    """Popular movies/TV -- Seerr's discover endpoints return TMDB-shaped
    results (id, title/name, posterPath, releaseDate/firstAirDate,
    mediaInfo), same shape as search, just no query."""
    path = "/api/v1/discover/movies" if media_type == "movie" else "/api/v1/discover/tv"
    status, text = seerr_request(path, {"page": 1})
    data = json.loads(text)
    results = []
    for r in data.get("results", []):
        media_info = r.get("mediaInfo") or {}
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        results.append({
            "tmdbId": r.get("id"),
            "title": r.get("title") or r.get("name"),
            "type": media_type,
            "poster": r.get("posterPath"),
            "year": date[:4],
            "status": media_info.get("status"),
        })
        if len(results) >= take:
            break
    return results


def seerr_submit_request(tmdb_id, media_type):
    """POST a new request to Seerr, which hands off to Radarr/Sonarr. Only
    ever called from the relay's own /seerr-request route -- the API key
    never reaches the widget's JS."""
    status, text = seerr_request(
        "/api/v1/request", method="POST",
        json_body={"mediaType": media_type, "mediaId": tmdb_id},
    )
    return status in (200, 201), text


def seerr_poll_once():
    """One pass of the Seerr poll -- pulled out of the loop so a fresh
    request submission can trigger an immediate refresh instead of waiting
    up to SEERR_POLL_SECONDS for the next scheduled poll."""
    try:
        status, text = seerr_request("/api/v1/request", {"take": 15, "sort": "added"})
        req_json = json.loads(text)

        # Unlike the original version, this includes every recent request
        # regardless of status -- not just ones awaiting admin approval --
        # so the Requests tab can show Requested/Processing/Available states
        # instead of only ever showing "pending".
        requests_list = []
        open_count = 0
        for r in req_json.get("results", []):
            media = r.get("media") or {}
            tmdb_id = media.get("tmdbId")
            media_type = r.get("type") or media.get("mediaType")
            details = seerr_resolve_details(tmdb_id, media_type) if tmdb_id else {"title": f"Request #{r.get('id')}", "poster": None}
            request_status = r.get("status")   # 1=pending approval, 2=approved, 3=declined
            media_status = media.get("status")  # 1/None=unknown, 2=pending, 3=processing, 4=partial, 5=available
            requests_list.append({
                "id": r.get("id"),
                "tmdbId": tmdb_id,
                "title": details["title"],
                "poster": details["poster"],
                "type": media_type,
                "requestedBy": (r.get("requestedBy") or {}).get("displayName", "Unknown"),
                "status": media_status,
                "requestStatus": request_status,
            })
            # "Open" = still awaiting approval or in progress, not yet
            # available and not declined -- what the tab badge counts.
            if request_status != 3 and media_status != 5:
                open_count += 1

        status2, text2 = seerr_request("/api/v1/media", {"filter": "available", "take": 30, "sort": "mediaAdded"})
        recent_json = json.loads(text2)

        recent_movies, recent_tv = [], []
        for m in recent_json.get("results", []):
            tmdb_id = m.get("tmdbId")
            media_type = m.get("mediaType")
            details = seerr_resolve_details(tmdb_id, media_type) if tmdb_id else {"title": f"Media #{m.get('id')}", "poster": None}
            item = {"id": m.get("id"), "tmdbId": tmdb_id, "title": details["title"], "poster": details["poster"], "type": media_type, "status": 5}
            (recent_movies if media_type == "movie" else recent_tv).append(item)

        popular_movies = seerr_discover("movie")
        popular_tv = seerr_discover("tv")

        with STATE_LOCK:
            s = STATE["seerr"]
            s["online"] = True
            s["openRequestsCount"] = open_count
            s["requests"] = requests_list[:15]
            s["recentMovies"] = recent_movies[:15]
            s["recentTV"] = recent_tv[:15]
            s["popularMovies"] = popular_movies
            s["popularTV"] = popular_tv
            s["updated_at"] = time.time()
            s["error"] = None
    except Exception as exc:
        DEBUG["last_seerr_error"] = repr(exc)
        with STATE_LOCK:
            STATE["seerr"]["online"] = False
            STATE["seerr"]["error"] = str(exc)
            STATE["seerr"]["updated_at"] = time.time()


def seerr_poll_thread_once():
    """Fire-and-forget wrapper so a POST handler can kick off one extra
    poll on its own thread without blocking the HTTP response."""
    seerr_poll_once()


def seerr_poll_thread():
    while True:
        seerr_poll_once()
        time.sleep(SEERR_POLL_SECONDS)


def plex_request(path, params=None, headers=None, timeout=8):
    """Minimal client for Plex's API. Auth is via X-Plex-Token header (Plex
    has no separate API-key concept -- this token comes from a one-time
    Plex sign-in). Accept: application/json gets JSON back instead of
    Plex's default XML. Pagination (X-Plex-Container-Size/-Start) must be
    sent as headers, not query params -- confirmed against a live instance
    where passing it as a query param was silently ignored and the default
    page size (50) came back instead."""
    url = f"{PLEX_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("X-Plex-Token", PLEX_TOKEN)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def plex_thumb_url(thumb_path):
    """Plex thumb/art paths are relative and themselves need the token to
    load -- build a full URL the widget's <img> can use directly."""
    if not thumb_path:
        return None
    sep = "&" if "?" in thumb_path else "?"
    return f"{PLEX_BASE_URL}{thumb_path}{sep}X-Plex-Token={PLEX_TOKEN}"


def plex_display_item(m):
    """Normalizes a /library/recentlyAdded entry into {title, type, thumb,
    addedAt, badge}. Confirmed against a real response that recentlyAdded
    mixes top-level types -- movies come back as flat Video/movie entries,
    but TV is reported at the season level as Directory/season entries (the
    whole season was added, not individual episodes), each needing its
    parentTitle/parentThumb (the show) rather than its own bare "Season 1"
    title/thumb to be useful at a glance. Episode-level entries (seen in
    /status/sessions, not confirmed here for recentlyAdded) use
    grandparentTitle/-Thumb the same way, one level further up -- handled
    the same for consistency. "badge" is a short display label
    distinguishing movie/season/episode -- season's own "index" attribute
    is the season number; an episode's "parentIndex"/"index" are the
    season/episode numbers respectively (standard Plex convention)."""
    t = m.get("type")
    if t == "episode":
        show = m.get("grandparentTitle")
        title = f"{show} \u2013 {m.get('title')}" if show else m.get("title")
        thumb = m.get("thumb") or m.get("grandparentThumb")
        season_num = m.get("parentIndex")
        ep_num = m.get("index")
        badge = f"S{season_num} \u00b7 E{ep_num}" if season_num is not None and ep_num is not None else "EPISODE"
    elif t == "season":
        show = m.get("parentTitle")
        title = f"{show} \u2013 {m.get('title')}" if show else m.get("title")
        thumb = m.get("thumb") or m.get("parentThumb")
        season_num = m.get("index")
        badge = f"SEASON {season_num}" if season_num is not None else "SEASON"
    elif t == "show":
        title = m.get("title")
        thumb = m.get("thumb")
        badge = "SHOW"
    else:  # movie
        title = m.get("title")
        thumb = m.get("thumb")
        badge = "MOVIE"
    return {
        "title": title,
        "type": t,
        "badge": badge,
        "thumb": plex_thumb_url(thumb),
        "addedAt": m.get("addedAt"),
    }

def plex_poll_once():
    """One pass of the Plex poll: active sessions (with transcode/hardware
    status) and recently added media. NOTE: field names/paths below follow
    Plex's documented API shape but haven't been confirmed against a real
    response from this instance yet -- if sessions or recently-added come
    back empty or wrong, check /debug's last_plex_error and paste a raw
    /status/sessions response so the field mapping can be corrected, same
    process used to nail down Seerr's actual shape earlier."""
    try:
        status, text = plex_request("/status/sessions")
        data = json.loads(text)
        raw_sessions = (data.get("MediaContainer") or {}).get("Metadata") or []

        sessions = []
        transcoding_count = 0
        for m in raw_sessions:
            media0 = (m.get("Media") or [{}])[0]
            part0 = (media0.get("Part") or [{}])[0]
            # decision is the authoritative play-mode signal, always present
            # regardless of whether a TranscodeSession element also is --
            # confirmed against a real direct-play session that had no
            # TranscodeSession element at all.
            decision = part0.get("decision")  # "directplay" | "copy" | "transcode"
            video_res = media0.get("videoResolution")  # e.g. "1080", "4k"
            transcode = m.get("TranscodeSession")
            is_transcoding = decision == "transcode" or transcode is not None
            if is_transcoding:
                transcoding_count += 1

            if decision == "transcode":
                mode_label = "Transcoding"
            elif decision == "copy":
                mode_label = "Direct Stream"
            else:
                mode_label = "Direct Play"
            quality = f"{video_res} \u00b7 {mode_label}" if video_res else mode_label

            player = m.get("Player") or {}
            user = m.get("User") or {}
            session_info = m.get("Session") or {}

            duration = m.get("duration") or 0
            offset = m.get("viewOffset") or 0
            progress = (offset / duration) if duration else 0

            title = m.get("title")
            if m.get("type") == "episode":
                show = m.get("grandparentTitle")
                if show:
                    title = f"{show} \u2013 {title}"

            sessions.append({
                "title": title,
                "type": m.get("type"),
                "thumb": plex_thumb_url(m.get("thumb") or m.get("grandparentThumb")),
                "user": user.get("title", "Unknown"),
                "state": player.get("state"),  # playing | paused | buffering
                "progress": round(progress, 3),
                "transcoding": is_transcoding,
                "hwTranscode": bool(transcode.get("transcodeHwRequested")) if transcode else False,
                "quality": quality,
                "bandwidthKbps": session_info.get("bandwidth"),
                "location": session_info.get("location"),  # "lan" | "wan"
                # Player.title is the friendly device name (e.g. "Daniel's
                # S25"); Player.device is a raw model string (e.g.
                # "SM-S931B") -- confirmed the former is far more useful.
                "device": player.get("title") or player.get("product") or player.get("device"),
                # Session.id is what /status/sessions/terminate expects as
                # its sessionId param -- confirmed it's a distinct value
                # from ratingKey/Player.machineIdentifier in a real
                # response (they happened to match in the sample seen, but
                # Session.id is the field the terminate endpoint documents).
                "sessionId": session_info.get("id"),
            })

        recently_added = []
        try:
            status2, text2 = plex_request(
                "/library/recentlyAdded",
                headers={"X-Plex-Container-Size": 15, "X-Plex-Container-Start": 0},
            )
            data2 = json.loads(text2)
            for m in (data2.get("MediaContainer") or {}).get("Metadata") or []:
                recently_added.append(plex_display_item(m))
        except Exception as exc2:
            # Recently-added is a nice-to-have -- don't let it take down
            # session reporting if this specific endpoint isn't right for
            # this Plex version.
            DEBUG["last_plex_error"] = f"recentlyAdded: {exc2!r}"

        with STATE_LOCK:
            s = STATE["plex"]
            s["online"] = True
            s["sessionCount"] = len(sessions)
            s["transcodingCount"] = transcoding_count
            s["sessions"] = sessions
            s["recentlyAdded"] = recently_added
            s["updated_at"] = time.time()
            s["error"] = None
    except Exception as exc:
        DEBUG["last_plex_error"] = repr(exc)
        with STATE_LOCK:
            STATE["plex"]["online"] = False
            STATE["plex"]["error"] = str(exc)
            STATE["plex"]["updated_at"] = time.time()


def plex_stop_session(session_id, reason="Stopped from iCUE widget"):
    """Terminates a playback session -- kicks the stream. Uses Plex's
    documented /status/sessions/terminate endpoint. Destructive, so the
    widget requires a second confirming tap before this is ever called,
    same pattern as the Minecraft widget's Stop/Restart confirmation."""
    status, text = plex_request(
        "/status/sessions/terminate",
        {"sessionId": session_id, "reason": reason},
    )
    return status == 200, text


def plex_poll_thread_once():
    """Fire-and-forget wrapper so a POST handler can kick off one extra
    poll on its own thread without blocking the HTTP response -- mirrors
    seerr_poll_thread_once so a stopped session disappears from Now
    Playing immediately instead of waiting up to PLEX_POLL_SECONDS."""
    plex_poll_once()


def plex_poll_thread():
    while True:
        plex_poll_once()
        time.sleep(PLEX_POLL_SECONDS)


def mc_poll_thread(server_name, host, port):
    """Polls one Minecraft server directly (Java Server List Ping), not via
    Crafty. On success, refreshes every field for this server. On failure
    (server/container stopped, port unreachable, timeout), only flips
    online/error/updated_at so the widget can show "last seen" using the
    previous snapshot rather than blanking everything out. One of these
    runs per configured server, since each now has its own host:port and
    can be live independently of the others."""
    server = JavaServer(host, port, timeout=5)
    while True:
        try:
            status = server.status()

            player_names = []
            if status.players.sample:
                player_names = sorted(p.name for p in status.players.sample)

            mod_loader = None
            mod_count = None
            mod_list = []
            mod_truncated = False
            if status.forge_data is not None:
                mod_loader = "forge"
                mods_sorted = sorted(status.forge_data.mods, key=lambda m: m.name.lower())
                mod_count = len(mods_sorted)
                mod_list = [{"id": m.name, "version": m.marker} for m in mods_sorted[:MC_MOD_LIST_CAP]]
                mod_truncated = status.forge_data.truncated or (mod_count > MC_MOD_LIST_CAP)
            else:
                mod_loader = "vanilla"
                mod_count = 0

            try:
                motd_text = status.motd.to_plain()
            except Exception:
                motd_text = None

            now = time.time()
            with STATE_LOCK:
                mc = _find_server_state(server_name)
                if mc is not None:
                    mc["online"] = True
                    mc["players_online"] = status.players.online
                    mc["players_max"] = status.players.max
                    mc["player_names"] = player_names
                    mc["version"] = status.version.name
                    mc["protocol"] = status.version.protocol
                    mc["motd"] = motd_text
                    mc["latency_ms"] = round(status.latency, 1)
                    mc["mod_loader"] = mod_loader
                    mc["mod_count"] = mod_count
                    mc["mod_list"] = mod_list
                    mc["mod_list_truncated"] = mod_truncated
                    mc["last_online_at"] = now
                    mc["updated_at"] = now
                    mc["error"] = None

                    if status.players.online == 0:
                        if mc["empty_since"] is None:
                            mc["empty_since"] = now
                    else:
                        mc["empty_since"] = None

                    history = mc["player_history"]
                    history.append(status.players.online)
                    max_points = max(1, (3600 // MC_POLL_SECONDS))
                    if len(history) > max_points:
                        del history[: len(history) - max_points]
        except Exception as exc:
            DEBUG["last_mc_error"] = repr(exc)
            with STATE_LOCK:
                mc = _find_server_state(server_name)
                if mc is not None:
                    mc["online"] = False
                    mc["updated_at"] = time.time()
                    mc["error"] = str(exc)
                    mc["empty_since"] = None  # "empty while running" doesn't apply once offline

        if CRAFTY_ENABLED and _find_crafty_id(server_name):
            try:
                crafty_get_stats(server_name)
            except Exception as exc:
                DEBUG["last_mc_error"] = repr(exc)
                with STATE_LOCK:
                    mc = _find_server_state(server_name)
                    if mc is not None:
                        mc["crafty_error"] = str(exc)
            try:
                crafty_get_logs(server_name)
            except Exception as exc:
                DEBUG["last_mc_error"] = repr(exc)
                # don't overwrite crafty_error here -- a logs-fetch failure
                # shouldn't mask a more important stats-fetch error above

        time.sleep(MC_POLL_SECONDS)


_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _parse_version_tuple(v):
    """'1.21.1-Forge' -> (1, 21, 1); 'Paper 1.21.3' -> (1, 21, 3). Returns
    None if nothing version-shaped is found anywhere in the string (e.g. a
    snapshot ID like '24w14a')."""
    m = _VERSION_RE.search(v or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(0).split("."))


def version_check_thread():
    """Compares each server's running version against Mojang's public
    release manifest (fetched once per cycle and reused for all servers,
    rather than once per server). Only meaningful for vanilla/unmodified
    version strings -- for a modpack, 'update available' here means a new
    Minecraft release exists, not that mod updates are available
    (mcstatus/Crafty have no signal for the latter)."""
    while True:
        try:
            req = urllib.request.Request(
                "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                manifest = json.loads(resp.read())
            latest_release = manifest.get("latest", {}).get("release")
            latest_tuple = _parse_version_tuple(latest_release)

            with STATE_LOCK:
                for mc in STATE["minecraft_servers"]:
                    mc["mc_latest_release"] = latest_release
                    current_tuple = _parse_version_tuple(mc.get("version"))
                    if current_tuple and latest_tuple:
                        # Mojang moved from "1.X.Y" to a year-based "YY.N" scheme in 2026.
                        # A raw tuple compare across that boundary (e.g. (1,21,1) vs (26,2))
                        # is always "true" but means nothing -- old and new numbering aren't
                        # on the same scale, so treat a scheme mismatch as unknown instead.
                        old_scheme = lambda t: t[0] == 1
                        if old_scheme(current_tuple) != old_scheme(latest_tuple):
                            mc["update_available"] = None
                        else:
                            mc["update_available"] = latest_tuple > current_tuple
                    else:
                        mc["update_available"] = None
        except Exception as exc:
            DEBUG["last_mc_error"] = repr(exc)
        time.sleep(VERSION_CHECK_SECONDS)


def slow_poll_thread():
    while True:
        try:
            with make_client() as c:
                sysinfo = c.call("system.info")
                pool_rows = {p["name"]: p for p in c.call("pool.query")}

                pools = []
                for ds in c.call("pool.dataset.query"):
                    ds_id = ds.get("id", "")
                    if "/" in ds_id:
                        continue
                    if POOLS_FILTER and ds_id not in POOLS_FILTER:
                        continue
                    used = (ds.get("used") or {}).get("parsed")
                    avail = (ds.get("available") or {}).get("parsed")
                    total = (used or 0) + (avail or 0)
                    prow = pool_rows.get(ds_id, {})
                    scrub_date, scrub_duration = pool_scrub_info(prow)
                    topo_str, disks_total, disks_error = pool_topology_summary(prow)
                    pools.append({
                        "name": ds_id,
                        "status": prow.get("status", "UNKNOWN"),
                        "scan": pool_scan_label(prow),
                        "used_gb": bytes_to_gb(used),
                        "total_gb": bytes_to_gb(total),
                        "percent": round(used / total * 100, 1) if used is not None and total else None,
                        "topology": topo_str,
                        "disks_total": disks_total,
                        "disks_error": disks_error,
                        "last_scrub_date": scrub_date,
                        "last_scrub_duration": scrub_duration,
                    })

                apps_raw = c.call("app.query")
                apps = [
                    {
                        "name": a.get("name") or a.get("id") or "?",
                        "state": (a.get("state") or "UNKNOWN").upper(),
                        "version": a.get("version"),
                        "update_available": bool(a.get("upgrade_available")),
                    }
                    for a in apps_raw
                ]
                # problems (stopped, or update available) first, then alphabetical
                apps.sort(key=lambda a: (
                    0 if a["state"] != "RUNNING" else (1 if a["update_available"] else 2),
                    a["name"],
                ))
                apps_needing_update = [a["name"] for a in apps if a["update_available"]]

                warn = crit = 0
                alert_rows = []
                for a in c.call("alert.list"):
                    if a.get("dismissed"):
                        continue
                    level = (a.get("level") or "").upper()
                    if level == "CRITICAL":
                        crit += 1
                    elif level == "WARNING":
                        warn += 1
                    else:
                        continue  # skip INFO/lower -- app-update notices etc. duplicate the Apps tile
                    text = a.get("formatted") or a.get("text") or a.get("id") or "Alert"
                    alert_rows.append({"level": level, "text": text[:120]})

                # worst first, cap the list so the payload stays small
                severity_rank = {"CRITICAL": 0, "WARNING": 1}
                alert_rows.sort(key=lambda r: severity_rank.get(r["level"], 2))
                alert_rows = alert_rows[:6]

                iface_name = ip_address = link_state = None
                for iface in c.call("interface.query"):
                    for alias in (iface.get("state") or {}).get("aliases") or []:
                        if alias.get("type") == "INET":
                            iface_name = iface.get("name")
                            ip_address = alias.get("address")
                            link_state = (iface.get("state") or {}).get("link_state")
                            break
                    if ip_address:
                        break

                backup_tasks = []
                try:
                    for t in c.call("cloudsync.query"):
                        if not t.get("enabled"):
                            continue
                        job = t.get("job") or {}
                        finished = job.get("time_finished")
                        started = job.get("time_started")
                        state = (job.get("state") or "UNKNOWN").upper()
                        entry = {
                            "name": (t.get("description") or "Cloud Sync").split(" - ")[0][:40],
                            "direction": t.get("direction"),
                            "state": state,
                            "finished": str(finished)[:16] if finished else None,
                        }
                        if state == "FAILED":
                            # last_run: prefer when it started (a failed job may
                            # never have reached time_finished); fall back to
                            # time_finished if that's all we have.
                            last_run = started or finished
                            if last_run:
                                entry["last_run"] = str(last_run)[:16]
                            # TrueNAS job objects carry the failure reason in
                            # "error" (short) and/or "exception" (full traceback);
                            # take the first line of whichever is present and cap
                            # it so a stray traceback can't blow out the tile.
                            err = job.get("error") or job.get("exception")
                            if err:
                                entry["error"] = str(err).strip().splitlines()[0][:120]
                        backup_tasks.append(entry)
                except Exception:
                    pass
                # failed/running first
                state_rank = {"FAILED": 0, "RUNNING": 1}
                backup_tasks.sort(key=lambda t: state_rank.get(t["state"], 2))

                with STATE_LOCK:
                    STATE["pools"] = pools
                    STATE["apps"] = apps
                    STATE["app_updates"] = len(apps_needing_update)
                    STATE["app_update_names"] = apps_needing_update
                    STATE["alerts_warning"] = warn
                    STATE["alerts_critical"] = crit
                    STATE["alerts"] = alert_rows
                    STATE["hostname"] = sysinfo.get("hostname")
                    STATE["tn_version"] = sysinfo.get("version")
                    STATE["uptime_seconds"] = sysinfo.get("uptime_seconds")
                    STATE["iface_name"] = iface_name
                    STATE["ip_address"] = ip_address
                    STATE["link_up"] = (link_state == "LINK_STATE_UP")
                    STATE["backup_tasks"] = backup_tasks
        except Exception as exc:
            DEBUG["last_slow_poll_error"] = repr(exc)
        time.sleep(SLOW_POLL_SECONDS)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Parse once so routes can read query params; existing exact-path
        # routes below are unaffected since none of them ever carried a
        # query string -- comparing parsed.path is equivalent for those.
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/stats":
            with STATE_LOCK:
                payload = dict(STATE)
            with UPGRADE_LOCK:
                payload["app_upgrading"] = list(UPGRADING)
                payload["app_upgrade_results"] = dict(UPGRADE_RESULTS)
            self._send_json(payload)
        elif path == "/mc-stats":
            with STATE_LOCK:
                self._send_json(STATE["minecraft_servers"])
        elif path == "/qbit-stats":
            with STATE_LOCK:
                self._send_json(STATE["qbittorrent"])
        elif path == "/seerr-stats":
            with STATE_LOCK:
                self._send_json(STATE["seerr"])
        elif path == "/plex-stats":
            with STATE_LOCK:
                self._send_json(STATE["plex"])
        elif path == "/seerr-search":
            if not SEERR_ENABLED:
                self.send_response(400)
                self.end_headers()
                return
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                self._send_json([])
                return
            try:
                results = seerr_search(query)
                self._send_json(results)
            except Exception as exc:
                DEBUG["last_seerr_error"] = repr(exc)
                self._send_json({"error": str(exc)})
        elif path == "/debug":
            self._send_json(DEBUG)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if self.path == "/update-app":
            name = payload.get("name")
            if not name:
                self.send_response(400)
                self.end_headers()
                return
            threading.Thread(target=run_upgrade, args=(name,), daemon=True).start()
            self._send_json({"started": True, "app": name})
        elif self.path == "/update-all":
            with STATE_LOCK:
                names = list(STATE.get("app_update_names") or [])
            for name in names:
                threading.Thread(target=run_upgrade, args=(name,), daemon=True).start()
            self._send_json({"started": True, "apps": names})
        elif self.path == "/mc-action":
            if not CRAFTY_ENABLED:
                self.send_response(400)
                self.end_headers()
                return
            action = payload.get("action")
            if action not in ("start", "stop", "restart", "backup"):
                self.send_response(400)
                self.end_headers()
                return
            server_name = payload.get("server")
            if not server_name:
                self.send_response(400)
                self.end_headers()
                return
            with STATE_LOCK:
                entry = _find_server_state(server_name)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if entry["crafty_action_pending"]:
                    self._send_json({"started": False, "reason": "action already in progress"})
                    return
                # Reserve the slot inside the same locked block as the check
                # above, rather than reading crafty_action_pending here and
                # letting crafty_send_action() set it on its own thread a
                # moment later -- that gap let two rapid requests (a
                # double-tap, or a client retry) both pass the "already
                # pending" check and fire the same action twice against
                # Crafty before either had a chance to claim it.
                entry["crafty_action_pending"] = action
            threading.Thread(target=crafty_send_action, args=(server_name, action), daemon=True).start()
            self._send_json({"started": True, "action": action, "server": server_name})
        elif self.path == "/qbit-action":
            if not QBIT_ENABLED:
                self.send_response(400)
                self.end_headers()
                return
            torrent_hash = payload.get("hash")
            action = payload.get("action")
            if not torrent_hash or action not in ("pause", "resume"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                qbit_torrent_action(torrent_hash, action)
                self._send_json({"started": True, "hash": torrent_hash, "action": action})
            except Exception as exc:
                DEBUG["last_qbit_error"] = repr(exc)
                self._send_json({"started": False, "error": str(exc)})
        elif self.path == "/seerr-request":
            if not SEERR_ENABLED:
                self.send_response(400)
                self.end_headers()
                return
            tmdb_id = payload.get("tmdbId")
            media_type = payload.get("mediaType")
            if not tmdb_id or media_type not in ("movie", "tv"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                ok, text = seerr_submit_request(tmdb_id, media_type)
                self._send_json({"success": ok, "response": text[:500]})
                if ok:
                    # Nudge the next poll to happen sooner so Recent
                    # Requests reflects this immediately rather than
                    # waiting up to SEERR_POLL_SECONDS.
                    threading.Thread(target=seerr_poll_thread_once, daemon=True).start()
            except Exception as exc:
                DEBUG["last_seerr_error"] = repr(exc)
                self._send_json({"success": False, "error": str(exc)})
        elif self.path == "/plex-stop":
            if not PLEX_ENABLED:
                self.send_response(400)
                self.end_headers()
                return
            session_id = payload.get("sessionId")
            if not session_id:
                self.send_response(400)
                self.end_headers()
                return
            try:
                ok, text = plex_stop_session(session_id)
                self._send_json({"success": ok, "response": text[:500]})
                if ok:
                    threading.Thread(target=plex_poll_thread_once, daemon=True).start()
            except Exception as exc:
                DEBUG["last_plex_error"] = repr(exc)
                self._send_json({"success": False, "error": str(exc)})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    threading.Thread(target=realtime_thread, daemon=True).start()
    threading.Thread(target=slow_poll_thread, daemon=True).start()
    if MC_ENABLED:
        for s in MC_SERVERS:
            name = s.get("name", "Server")
            host = s.get("host", HOST)
            port = s.get("port", 25565)
            threading.Thread(target=mc_poll_thread, args=(name, host, port), daemon=True).start()
        threading.Thread(target=version_check_thread, daemon=True).start()
    if QBIT_ENABLED:
        threading.Thread(target=qbit_poll_thread, daemon=True).start()
    if SEERR_ENABLED:
        threading.Thread(target=seerr_poll_thread, daemon=True).start()
    if PLEX_ENABLED:
        threading.Thread(target=plex_poll_thread, daemon=True).start()
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Serving TrueNAS stats on http://127.0.0.1:{PORT}/stats  (debug: /debug)")
        if MC_ENABLED:
            for s in MC_SERVERS:
                print(f"  Minecraft '{s.get('name', 'Server')}' ({s.get('host', HOST)}:{s.get('port', 25565)}) "
                      f"polled every {MC_POLL_SECONDS}s -> /mc-stats")
        if CRAFTY_ENABLED:
            names = ", ".join(s.get("name", "Server") for s in MC_SERVERS if s.get("crafty_server_id"))
            print(f"Crafty control enabled ({CRAFTY_BASE_URL}, servers: {names}) -> POST /mc-action")
        else:
            print("Crafty control disabled -- set minecraft.servers[].crafty_server_id and "
                  "minecraft.crafty.api_token in config.json to enable start/stop")
        if QBIT_ENABLED:
            print(f"qBittorrent ({QBIT_BASE_URL}) polled every {QBIT_POLL_SECONDS}s -> /qbit-stats")
        if SEERR_ENABLED:
            print(f"Seerr ({SEERR_BASE_URL}) polled every {SEERR_POLL_SECONDS}s -> /seerr-stats")
        else:
            print("Seerr disabled -- set seerr.enabled=true and seerr.api_key in config.json to enable")
        if PLEX_ENABLED:
            print(f"Plex ({PLEX_BASE_URL}) polled every {PLEX_POLL_SECONDS}s -> /plex-stats")
        else:
            print("Plex disabled -- set plex.enabled=true and plex.token in config.json to enable")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
