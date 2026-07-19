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

# Minecraft server (e.g. a Crafty-managed FTB/Forge server) polled directly
# over the Java Server List Ping protocol -- no Crafty API token needed.
# Crafty's own "is it running" state is a separate concern; this just asks
# the Minecraft server itself, which also happens to be how mod data is
# obtained (Forge servers embed it in the ping response).
MC_CFG = CONFIG.get("minecraft") or {}
MC_ENABLED = MC_CFG.get("enabled", False)
MC_HOST = MC_CFG.get("host", HOST)
MC_PORT = MC_CFG.get("port", 25565)
MC_POLL_SECONDS = MC_CFG.get("poll_seconds", 15)
MC_MOD_LIST_CAP = MC_CFG.get("mod_list_cap", 400)  # keep payload sane on huge modpacks

# Crafty's own control API -- only needed for the start/stop button, since
# the SLP ping above is read-only. Get server_id from Crafty's URL when
# viewing the server (or GET /api/v2/servers), and api_token from
# Profile -> API Keys -> Generate Key in the Crafty web UI.
CRAFTY_CFG = MC_CFG.get("crafty") or {}
CRAFTY_ENABLED = bool(CRAFTY_CFG.get("server_id")) and bool(CRAFTY_CFG.get("api_token"))
CRAFTY_BASE_URL = CRAFTY_CFG.get("base_url", "https://192.168.1.181:30146").rstrip("/")
CRAFTY_SERVER_ID = CRAFTY_CFG.get("server_id", "")
CRAFTY_API_TOKEN = CRAFTY_CFG.get("api_token", "")
CRAFTY_VERIFY_SSL = CRAFTY_CFG.get("verify_ssl", False)

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

QBIT_DL_STATES = {"downloading", "metaDL", "allocating", "checkingDL", "forcedDL"}
QBIT_UP_STATES = {"uploading", "checkingUP", "forcedUP"}
# Queued/stalled: still incomplete and still *intends* to run (the client
# will resume it automatically once conditions allow) -- distinct from an
# explicit stop/pause, which is why this is its own group.
QBIT_QUEUED_STATES = {"queuedDL", "stalledDL", "queuedUP", "stalledUP"}


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
    "minecraft": {
        "enabled": MC_ENABLED,
        "online": False,
        "host": MC_HOST,
        "port": MC_PORT,
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
        # Crafty control state (only populated if crafty is configured)
        "crafty_enabled": CRAFTY_ENABLED,
        "crafty_running": None,      # True/False/None (None = unknown/not configured)
        "crafty_starting": None,     # waiting_start -- process is up, MC hasn't finished loading
        "crafty_updating": None,
        "crafty_action_pending": None,  # "start" | "stop" | "restart" | "backup" | None
        "crafty_error": None,
        "crafty_cpu_percent": None,
        "crafty_mem": None,           # e.g. "1.2GB" -- Crafty's own formatted string
        "crafty_mem_percent": None,
        "world_name": None,
        "world_size": None,           # e.g. "128MB" -- Crafty's own formatted string
        "last_backup_triggered_at": None,  # in-memory only -- resets on relay restart,
                                            # this is "last backup we asked for", not
                                            # Crafty's own backup history (no API for that)
        "mc_latest_release": None,
        "update_available": None,     # None = unknown/not checked yet, True/False once checked
        "started_at": None,           # epoch, parsed from Crafty's own "started" field
        "empty_since": None,          # epoch since players_online last hit 0 while running
        "console_tail": [],           # last few lines from Crafty's server log
        "player_history": [],         # rolling players_online samples, ~last hour
    },
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
}
DEBUG = {"last_realtime_raw": None, "last_slow_poll_error": None, "last_realtime_error": None,
         "last_mc_error": None, "last_qbit_error": None}


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


def _format_mem_kb(raw):
    """Crafty's 'mem' field is a raw number, not the pre-formatted string its
    own docs example implies -- confirmed empirically (1114112.0 for a ~1GB
    working set), so it's kilobytes. Convert to a human string ourselves."""
    if raw is None:
        return None
    try:
        kb = float(raw)
    except (TypeError, ValueError):
        return str(raw)  # unexpected shape -- show it rather than hide it
    mb = kb / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f}GB"
    return f"{mb:.0f}MB"


def crafty_get_stats():
    """GET /servers/{id}/stats -- used for running/starting/updating state,
    which the SLP ping alone can't distinguish (e.g. 'process is up but the
    modpack hasn't finished loading yet' vs 'fully offline'), plus CPU/RAM
    and world size, which only Crafty (not the ping protocol) exposes."""
    payload = crafty_request(f"/api/v2/servers/{CRAFTY_SERVER_ID}/stats")
    data = payload.get("data") or {}

    started_at = None
    started_raw = data.get("started") or payload.get("started")
    if started_raw:
        try:
            started_at = datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            started_at = None  # format mismatch or server hasn't reported a start time yet

    with STATE_LOCK:
        mc = STATE["minecraft"]
        mc["crafty_running"] = bool(data.get("running"))
        mc["crafty_starting"] = bool(data.get("waiting_start"))
        mc["crafty_updating"] = bool(data.get("updating"))
        mc["crafty_cpu_percent"] = data.get("cpu")
        mc["crafty_mem"] = _format_mem_kb(data.get("mem"))
        mc["crafty_mem_percent"] = data.get("mem_percent")
        mc["world_name"] = data.get("world_name")
        mc["world_size"] = data.get("world_size")
        mc["started_at"] = started_at
        mc["crafty_error"] = None


def crafty_get_logs():
    """GET /servers/{id}/logs -- Crafty can return a large buffer (its own
    max_log_lines config, often several hundred), so this keeps only the
    last few lines rather than shipping the whole thing to the widget."""
    payload = crafty_request(f"/api/v2/servers/{CRAFTY_SERVER_ID}/logs")
    lines = payload.get("data") or []
    with STATE_LOCK:
        STATE["minecraft"]["console_tail"] = lines[-6:]


def crafty_send_action(action):
    """POST /servers/{id}/action/{start_server|stop_server}. Runs on its own
    thread so the HTTP handler returns immediately; the widget polls
    /mc-stats afterwards to see the state change land."""
    with STATE_LOCK:
        STATE["minecraft"]["crafty_action_pending"] = action
    try:
        crafty_request(f"/api/v2/servers/{CRAFTY_SERVER_ID}/action/{action}_server", method="POST")
        with STATE_LOCK:
            STATE["minecraft"]["crafty_error"] = None
            if action == "backup":
                STATE["minecraft"]["last_backup_triggered_at"] = time.time()
    except Exception as exc:
        with STATE_LOCK:
            STATE["minecraft"]["crafty_error"] = str(exc)
    finally:
        with STATE_LOCK:
            STATE["minecraft"]["crafty_action_pending"] = None
        try:
            crafty_get_stats()
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
            DEBUG["last_qbit_error"] = f"login HTTP {status}: {text.strip()[:200]!r}"
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
            if not _QBIT_LOGGED_IN and not qbit_login():
                with STATE_LOCK:
                    STATE["qbittorrent"]["online"] = False
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


def mc_poll_thread():
    """Polls the Minecraft server directly (Java Server List Ping), not via
    Crafty. On success, refreshes every field. On failure (server/container
    stopped, port unreachable, timeout), only flips online/error/updated_at
    so the widget can show "last seen" using the previous snapshot rather
    than blanking everything out."""
    server = JavaServer(MC_HOST, MC_PORT, timeout=5)
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
                mc = STATE["minecraft"]
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
                mc = STATE["minecraft"]
                mc["online"] = False
                mc["updated_at"] = time.time()
                mc["error"] = str(exc)
                mc["empty_since"] = None  # "empty while running" doesn't apply once offline

        if CRAFTY_ENABLED:
            try:
                crafty_get_stats()
            except Exception as exc:
                DEBUG["last_mc_error"] = repr(exc)
                with STATE_LOCK:
                    STATE["minecraft"]["crafty_error"] = str(exc)
            try:
                crafty_get_logs()
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
    """Compares the running MC version against Mojang's public release
    manifest. Only meaningful for vanilla/unmodified version strings --
    for a modpack, 'update available' here means a new Minecraft release
    exists, not that mod updates are available (mcstatus/Crafty have no
    signal for the latter)."""
    while True:
        try:
            req = urllib.request.Request(
                "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                manifest = json.loads(resp.read())
            latest_release = manifest.get("latest", {}).get("release")

            with STATE_LOCK:
                current = STATE["minecraft"].get("version")
                STATE["minecraft"]["mc_latest_release"] = latest_release

            current_tuple = _parse_version_tuple(current)
            latest_tuple = _parse_version_tuple(latest_release)
            with STATE_LOCK:
                if current_tuple and latest_tuple:
                    # Mojang moved from "1.X.Y" to a year-based "YY.N" scheme in 2026.
                    # A raw tuple compare across that boundary (e.g. (1,21,1) vs (26,2))
                    # is always "true" but means nothing -- old and new numbering aren't
                    # on the same scale, so treat a scheme mismatch as unknown instead.
                    old_scheme = lambda t: t[0] == 1
                    if old_scheme(current_tuple) != old_scheme(latest_tuple):
                        STATE["minecraft"]["update_available"] = None
                    else:
                        STATE["minecraft"]["update_available"] = latest_tuple > current_tuple
                else:
                    STATE["minecraft"]["update_available"] = None
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
                        backup_tasks.append({
                            "name": (t.get("description") or "Cloud Sync").split(" - ")[0][:40],
                            "direction": t.get("direction"),
                            "state": (job.get("state") or "UNKNOWN").upper(),
                            "finished": str(finished)[:16] if finished else None,
                        })
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
        if self.path == "/stats":
            with STATE_LOCK:
                payload = dict(STATE)
            with UPGRADE_LOCK:
                payload["app_upgrading"] = list(UPGRADING)
                payload["app_upgrade_results"] = dict(UPGRADE_RESULTS)
            self._send_json(payload)
        elif self.path == "/mc-stats":
            with STATE_LOCK:
                self._send_json(STATE["minecraft"])
        elif self.path == "/qbit-stats":
            with STATE_LOCK:
                self._send_json(STATE["qbittorrent"])
        elif self.path == "/debug":
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
            with STATE_LOCK:
                already_pending = STATE["minecraft"]["crafty_action_pending"]
            if already_pending:
                self._send_json({"started": False, "reason": "action already in progress"})
                return
            threading.Thread(target=crafty_send_action, args=(action,), daemon=True).start()
            self._send_json({"started": True, "action": action})
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
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    threading.Thread(target=realtime_thread, daemon=True).start()
    threading.Thread(target=slow_poll_thread, daemon=True).start()
    if MC_ENABLED:
        threading.Thread(target=mc_poll_thread, daemon=True).start()
        threading.Thread(target=version_check_thread, daemon=True).start()
    if QBIT_ENABLED:
        threading.Thread(target=qbit_poll_thread, daemon=True).start()
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Serving TrueNAS stats on http://127.0.0.1:{PORT}/stats  (debug: /debug)")
        if MC_ENABLED:
            print(f"Minecraft ({MC_HOST}:{MC_PORT}) polled every {MC_POLL_SECONDS}s -> /mc-stats")
        if CRAFTY_ENABLED:
            print(f"Crafty control enabled ({CRAFTY_BASE_URL}, server {CRAFTY_SERVER_ID}) -> POST /mc-action")
        else:
            print("Crafty control disabled -- set minecraft.crafty.server_id and api_token in config.json to enable start/stop")
        if QBIT_ENABLED:
            print(f"qBittorrent ({QBIT_BASE_URL}) polled every {QBIT_POLL_SECONDS}s -> /qbit-stats")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
