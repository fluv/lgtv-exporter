#!/usr/bin/env python3
"""Prometheus exporter for LG WebOS TVs via SSAP WebSocket.

Subscribes to TV state changes via bscpylgtv and serves metrics on :9095.
Reconnects automatically when the TV goes into standby or reboots.

Environment variables:
  LGTV_IP           TV hostname or IP address (required)
  LGTV_CLIENT_KEY   SSAP client key obtained via --pair (required in server mode)
  PORT              HTTP port to serve /metrics on (default: 9095)
  RECONNECT_DELAY   Seconds between reconnect attempts (default: 30)

Run with --pair to perform the one-time pairing handshake and print the client key.
"""

import asyncio
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "9095"))
TV_IP = os.environ.get("LGTV_IP", "")
CLIENT_KEY = os.environ.get("LGTV_CLIENT_KEY", "")
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "30"))

STATES = [
    "system_info",
    "software_info",
    "power",
    "current_app",
    "muted",
    "volume",
    "inputs",
    "current_channel",
    "channel_info",
    "sound_output",
    "picture_settings",
]

_lock = Lock()
_connected = False
_tv: dict[str, Any] = {}


def _update_tv(client: Any) -> None:
    update: dict[str, Any] = {}
    for attr in (
        "power_state",
        "current_appId",
        "volume",
        "muted",
        "sound_output",
        "picture_settings",
        "current_channel",
        "channel_info",
        "software_info",
        "system_info",
        "inputs",
    ):
        v = getattr(client, attr, None)
        if v is not None:
            update[attr] = v
    with _lock:
        _tv.update(update)


def _render() -> bytes:
    with _lock:
        tv = dict(_tv)
        connected = _connected

    lines: list[str] = []

    def gauge(name: str, help_text: str, value: Any, **labels: str) -> None:
        if value is None:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lbl = ""
        if labels:
            parts = ",".join(f'{k}="{v}"' for k, v in labels.items() if v)
            if parts:
                lbl = "{" + parts + "}"
        lines.append(f"{name}{lbl} {value}")

    def info(name: str, help_text: str, **labels: str) -> None:
        populated = {k: v for k, v in labels.items() if v}
        if not populated:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        parts = ",".join(f'{k}="{v}"' for k, v in populated.items())
        lines.append(f"{name}{{{parts}}} 1")

    gauge("lgtv_connected", "1 if the exporter has an active SSAP connection to the TV", int(connected))

    if not connected:
        return ("\n".join(lines) + "\n").encode()

    # Power state
    ps = tv.get("power_state")
    if isinstance(ps, dict):
        ps = ps.get("state", "")
    is_on = 1 if ps == "Active" else 0
    gauge("lgtv_on", "1 when TV is in Active power state", is_on)
    info("lgtv_power_info", "Current power state string", state=str(ps or ""))

    # Volume
    gauge("lgtv_volume", "Current volume level (0-100)", tv.get("volume"))
    muted = tv.get("muted")
    if muted is not None:
        gauge("lgtv_muted", "1 if audio is muted", int(bool(muted)))

    # Foreground app — this is the primary way to tell what's happening
    info("lgtv_app_info", "Current foreground application ID", app=str(tv.get("current_appId") or ""))

    # Sound output
    info("lgtv_sound_output_info", "Current audio output device", output=str(tv.get("sound_output") or ""))

    # Active input — match by appId first (covers HDMI inputs),
    # fall back to connected flag, then synthesise for the live TV tuner
    # which is not represented in the external inputs list.
    inputs = tv.get("inputs")
    current_app = str(tv.get("current_appId") or "")
    if isinstance(inputs, list):
        matched = None
        for inp in inputs:
            if isinstance(inp, dict) and inp.get("appId") == current_app:
                matched = inp
                break
        if matched is None:
            for inp in inputs:
                if isinstance(inp, dict) and inp.get("connected"):
                    matched = inp
                    break
        if matched:
            info(
                "lgtv_input_info",
                "Currently connected input source",
                id=str(matched.get("id") or ""),
                label=str(matched.get("label") or matched.get("name") or ""),
            )
        elif current_app == "com.webos.app.livetv":
            info("lgtv_input_info", "Currently connected input source",
                 id="livetv", label="Live TV")

    # Picture settings — emit all numeric values plus mode info
    ps_dict = tv.get("picture_settings")
    if isinstance(ps_dict, dict):
        for k, v in ps_dict.items():
            if isinstance(v, (int, float)):
                safe_key = "".join(c if c.isalnum() else "_" for c in k).lower().strip("_")
                if safe_key and safe_key[0].isdigit():
                    safe_key = "_" + safe_key
                gauge(f"lgtv_picture_{safe_key}", f"Picture setting: {k}", v)
        info(
            "lgtv_picture_info",
            "Current picture mode and HDR configuration",
            mode=str(ps_dict.get("pictureMode") or ps_dict.get("picture_mode") or ""),
            hdr=str(
                ps_dict.get("hdrDynamicToneMapping")
                or ps_dict.get("hdr_dynamic_tone_mapping")
                or ps_dict.get("hdrMode")
                or ""
            ),
        )

    # Current channel — only populated when on live TV.
    # current_channel carries name/number; channel_info carries programme name.
    ch = tv.get("current_channel") or {}
    ch_info = tv.get("channel_info") or {}
    if not isinstance(ch, dict):
        ch = {}
    if not isinstance(ch_info, dict):
        ch_info = {}
    channel_name = str(ch.get("channelName") or "")
    if channel_name:
        info(
            "lgtv_channel_info",
            "Current broadcast channel and programme (live TV only)",
            channel=channel_name,
            number=str(ch.get("channelNumber") or ""),
            program=str(ch_info.get("programName") or ch.get("programName") or ""),
        )

    # Build info
    si = tv.get("software_info") or {}
    sys_i = tv.get("system_info") or {}
    if not isinstance(si, dict):
        si = {}
    if not isinstance(sys_i, dict):
        sys_i = {}
    info(
        "lgtv_build_info",
        "TV model and firmware version",
        model=str(si.get("model_name") or sys_i.get("modelName") or ""),
        webos=str(si.get("webos_build_id") or si.get("major_ver") or ""),
        firmware=str(si.get("firmware_version") or ""),
    )

    return ("\n".join(lines) + "\n").encode()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        body = _render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


async def _connect_once() -> None:
    global _connected

    from bscpylgtv import WebOsClient

    client = await WebOsClient.create(
        TV_IP,
        client_key=CLIENT_KEY or None,
        states=STATES,
        ping_interval=None,
        key_file_path="/tmp/.aiopylgtv.sqlite",
    )

    async def on_state(cl: Any) -> None:
        _update_tv(cl)

    await client.register_state_update_callback(on_state)
    await client.connect()

    with _lock:
        _connected = True
    log.info("connected to %s", TV_IP)

    try:
        while True:
            await asyncio.sleep(30)
            await client.get_volume()  # lightweight keepalive; raises on disconnect
            # getCurrentChannel subscription only fires once on some WebOS firmware;
            # poll explicitly so channel changes are reflected within one keepalive cycle.
            try:
                await client.get_current_channel()
                await client.get_channel_info()
            except Exception as e:
                log.debug("channel poll skipped: %s", e)
            _update_tv(client)
    finally:
        with _lock:
            _connected = False
        try:
            await client.disconnect()
        except Exception:
            pass


async def _watch() -> None:
    while True:
        try:
            await _connect_once()
        except Exception as e:
            log.warning("TV unavailable: %s", e)
            with _lock:
                global _connected
                _connected = False
        log.info("reconnecting in %ds", RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


async def _pair() -> None:
    from bscpylgtv import WebOsClient

    log.info("pairing with %s — accept the prompt on your TV", TV_IP)
    client = await WebOsClient.create(
        TV_IP, client_key=None, ping_interval=None, key_file_path="/tmp/.aiopylgtv.sqlite"
    )
    await client.connect()
    key = getattr(client, "client_key", None)
    await client.disconnect()

    if key:
        print(key)
        print(
            f"\nTo store as a Kubernetes secret:\n"
            f"  kubectl create secret generic lgtv-client-key"
            f" --from-literal=client-key={key} -n lifestyle",
            file=sys.stderr,
        )
    else:
        log.error("pairing failed — no key returned")
        sys.exit(1)


if __name__ == "__main__":
    if not TV_IP:
        log.error("LGTV_IP environment variable required")
        sys.exit(1)

    if "--pair" in sys.argv:
        asyncio.run(_pair())
        sys.exit(0)

    if not CLIENT_KEY:
        log.error("LGTV_CLIENT_KEY required; run with --pair to pair first")
        sys.exit(1)

    server = HTTPServer(("", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("lgtv-exporter listening on :%d", PORT)

    asyncio.run(_watch())
