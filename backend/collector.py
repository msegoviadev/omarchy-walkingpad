#!/usr/bin/env python3
"""WalkingPad step collector daemon.

Idles in a BLE scan loop until a walking pad powers on and starts
advertising, then connects and records every status update to an append-only
JSONL history. The pad's own last-run memory is volatile (lost on power cut),
so continuous recording is what guarantees sessions are never lost.

Supports KingSmith pads speaking WiLink (A1/C1/C2/R1/S1, ...) and FTMS
(Z1, MC-21, ...), plus Sperax P3 Max, via the walkingpad-controller library.

Data layout:
  ~/.local/share/walkingpad/history.jsonl  today's uncompacted records, one
      JSON record per line; older days are compacted into rollup.json and
      dropped from this file
  ~/.local/share/walkingpad/status.json    live state, rewritten atomically
  ~/.local/share/walkingpad/devices.json   candidate pads seen while scanning
  ~/.local/share/walkingpad/rollup.json    per-day stats for all but today,
      rebuilt at startup and day rollover (see rollup.py)

History record types:
  {"ts", "session", "steps", "dist_m", "time_s", "speed", "state"}  live poll
  {"ts", "type": "final", "steps", "dist_m", "time_s"}              pad's stored
      last-run summary, fetched once per connect; the stats helper ignores it
      when live records exist nearby (means we recorded that run ourselves).

Config at ~/.config/walkingpad/config.json (all keys optional):
  {"device_address": "AA:BB:CC:DD:EE:FF",  # pin to one pad; absent = auto,
                                           # strongest candidate wins
   "device_name_contains": "walkingpad",   # extra case-insensitive matcher
   "poll_interval": 1.0}                   # seconds between status polls
The file is watched: changes apply within seconds, no restart needed.
"""

import asyncio
import json
import logging
import signal
import time
import uuid
from datetime import date
from pathlib import Path

import rollup
import safeio
from bleak import BleakScanner
from walkingpad_controller import WalkingPadController

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad"
HISTORY_FILE = DATA_DIR / "history.jsonl"
STATUS_FILE = DATA_DIR / "status.json"
DEVICES_FILE = DATA_DIR / "devices.json"
CONFIG_FILE = Path.home() / ".config" / "walkingpad" / "config.json"

SCAN_TIMEOUT = 10.0
RETRY_DELAY = 5.0
SESSION_END_AFTER = 15.0  # seconds of inactive belt before closing a session
STATUS_MIN_INTERVAL = 1.0  # throttle status.json writes
DEVICE_FORGET_AFTER = 24 * 3600  # drop pads not seen for a day
MAX_STATE_BYTES = 64 * 1024  # status, devices, config are all tiny by design
MAX_NAME_LENGTH = 64  # BLE names are short; cap before storage and QML

# BLE name prefixes of supported pads that do not contain "walkingpad"
# and are not covered by the generic "KS-" KingSmith prefix.
NAME_PREFIXES = ("KS-HD-", "KS-MC21-", "KS-SMC21C-", "ZP-ZEALR1-", "SPERAX_P3MAX")

# Service UUID -> protocol hint, used for the picker's display.
PROTOCOL_BY_SERVICE = {
    "0000fe00-0000-1000-8000-00805f9b34fb": "wilink",
    "00001826-0000-1000-8000-00805f9b34fb": "ftms",
    "0000fff0-0000-1000-8000-00805f9b34fb": "sperax",
}

log = logging.getLogger("walkingpad-collector")


def load_config() -> dict:
    config = safeio.read_json(CONFIG_FILE, MAX_STATE_BYTES)
    return config if isinstance(config, dict) else {}


def sanitize_name(name) -> str:
    """BLE names come from the radio; keep only printable characters and
    cap the length before they reach storage and QML."""
    cleaned = "".join(ch for ch in str(name or "") if ch.isprintable())
    return cleaned[:MAX_NAME_LENGTH]


def config_mtime() -> float:
    try:
        return CONFIG_FILE.stat().st_mtime
    except OSError:
        return 0.0


def protocol_hint(service_uuids) -> str:
    for service_uuid in service_uuids or []:
        hint = PROTOCOL_BY_SERVICE.get(str(service_uuid).lower())
        if hint:
            return hint
    return ""


def is_candidate(name: str | None, extra_needle: str = "") -> bool:
    """Name-based detection; intentionally conservative so neighboring FTMS
    fitness gear (bikes, rowers) is never auto-selected."""
    if not name:
        return False
    lowered = name.lower()
    if lowered.startswith("ks-") or "walkingpad" in lowered:
        return True
    if extra_needle and extra_needle in lowered:
        return True
    return any(name.startswith(prefix) for prefix in NAME_PREFIXES)


class Collector:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.config_mtime = config_mtime()
        self.controller: WalkingPadController | None = None
        self.session_id: str | None = None
        self.session_start = 0.0
        self.last_active = 0.0
        self.last_steps = 0
        self.last_status_write = 0.0
        self.last_history_key: tuple | None = None
        self.rollup_day: date | None = None

    @property
    def poll_interval(self) -> float:
        return float(self.config.get("poll_interval", 1.0))

    @property
    def pinned_address(self) -> str:
        return str(self.config.get("device_address", "")).strip().lower()

    def reload_config_if_changed(self) -> bool:
        mtime = config_mtime()
        if mtime == self.config_mtime:
            return False
        self.config_mtime = mtime
        self.config = load_config()
        log.info("Config reloaded: %s", self.config)
        return True

    # --- persistence ---

    def append_history(self, record: dict) -> None:
        try:
            safeio.append_line(HISTORY_FILE, json.dumps(record, separators=(",", ":")))
        except OSError:
            log.exception("Failed to append history")

    def write_status(self, status=None, connected=False, force=False) -> None:
        now = time.time()
        if not force and now - self.last_status_write < STATUS_MIN_INTERVAL:
            return
        self.last_status_write = now
        payload = {
            "enabled": True,
            "connected": connected,
            "device_name": sanitize_name(self.controller.name or "") if self.controller else "",
            "address": self.controller.address if self.controller else "",
            "state": status.belt_state if status else 0,
            "walking": bool(status and status.belt_state == 1),
            "steps": status.steps if status else 0,
            "dist_m": status.distance if status else 0,
            "time_s": status.duration if status else 0,
            "speed": status.speed if status else 0.0,
            "session_start": self.session_start if self.session_id else None,
            "ts": now,
        }
        try:
            safeio.atomic_write(STATUS_FILE, json.dumps(payload, separators=(",", ":")))
        except OSError:
            log.exception("Failed to write status")

    def remember_devices(self, candidates: list) -> None:
        """Update the seen-pads registry consumed by the widget's picker."""
        now = time.time()
        existing = safeio.read_json(DEVICES_FILE, MAX_STATE_BYTES)
        devices = existing if isinstance(existing, dict) else {}
        for device, adv in candidates:
            devices[device.address] = {
                "name": sanitize_name(device.name or ""),
                "address": device.address,
                "rssi": adv.rssi,
                "protocol": protocol_hint(adv.service_uuids),
                "last_seen": now,
            }
        devices = {
            address: info
            for address, info in devices.items()
            if now - float(info.get("last_seen", 0)) < DEVICE_FORGET_AFTER
        }
        try:
            safeio.atomic_write(DEVICES_FILE, json.dumps(devices, separators=(",", ":")))
        except OSError:
            log.exception("Failed to write devices")

    # --- rollup ---

    def maybe_rebuild_rollup(self) -> None:
        """Compact history older than today into rollup.json once per day.

        Runs at startup (rollup may be missing or stale after downtime) and
        whenever the calendar day changes while the daemon is up.
        """
        today = date.today()
        if self.rollup_day == today:
            return
        try:
            if rollup.rebuild_needed(today):
                built, dropped = rollup.rebuild_rollup(today)
                log.info(
                    "History rollup rebuilt: %d days through %s, %d history lines compacted",
                    len(built["days"]),
                    built["through"],
                    dropped,
                )
            self.rollup_day = today
        except Exception:
            log.exception("Rollup rebuild failed")

    # --- sessions ---

    def open_session(self) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.session_start = time.time()
        log.info("Session %s started", self.session_id)

    def close_session(self) -> None:
        if self.session_id:
            log.info("Session %s ended", self.session_id)
        self.session_id = None

    # --- status handling ---

    def on_status(self, status) -> None:
        now = time.time()
        active = status.belt_state == 1

        if active:
            counter_reset = self.last_steps > 0 and status.steps < self.last_steps
            if self.session_id is None or counter_reset:
                self.open_session()
            self.last_active = now
            self.last_steps = status.steps

            # Skip history lines that carry no new information.
            key = (status.steps, status.distance, status.duration, status.belt_state)
            if key != self.last_history_key:
                self.last_history_key = key
                self.append_history(
                    {
                        "ts": now,
                        "session": self.session_id,
                        "steps": status.steps,
                        "dist_m": status.distance,
                        "time_s": status.duration,
                        "speed": status.speed,
                        "state": status.belt_state,
                    }
                )
        elif self.session_id and now - self.last_active > SESSION_END_AFTER:
            self.close_session()

        self.write_status(status, connected=True)

    def on_final_record(self, _sender, record) -> None:
        """Handle the pad's stored last-run summary (WiLink ask_hist reply)."""
        log.info(
            "Last-run record from pad: steps=%s dist=%s time=%s",
            record.steps,
            record.dist,
            record.time,
        )
        if record.steps <= 0:
            return
        self.append_history(
            {
                "ts": time.time(),
                "type": "final",
                "steps": record.steps,
                # WiLink last-run distance is in 10m units, like current status.
                "dist_m": record.dist * 10,
                "time_s": record.time,
            }
        )

    # --- connection lifecycle ---

    async def scan_for_pad(self):
        log.info("Scanning for walking pad...")
        needle = str(self.config.get("device_name_contains", "walkingpad")).lower()
        devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
        candidates = [
            (device, adv)
            for device, adv in devices.values()
            if is_candidate(device.name, needle)
        ]
        self.remember_devices(candidates)

        pinned = self.pinned_address
        if pinned:
            for device, _adv in candidates:
                if device.address.lower() == pinned:
                    log.info("Found pinned pad: %s (%s)", device.name, device.address)
                    return device
            return None
        if not candidates:
            return None
        device, adv = max(candidates, key=lambda pair: pair[1].rssi or -999)
        log.info("Found pad: %s (%s, %s dBm)", device.name, device.address, adv.rssi)
        return device

    async def fetch_last_run(self) -> None:
        """Ask the pad for its stored last-run summary (WiLink only).

        Reaches through to the ph4-walkingpad controller underneath the
        unified wrapper; no-op on FTMS devices or if the internals change.
        """
        try:
            ph4 = self.controller._wilink._controller  # noqa: SLF001
        except AttributeError:
            return
        try:
            ph4.handler_last_status = self.on_final_record
            await ph4.ask_hist()
        except Exception:
            log.warning("Could not fetch last-run record", exc_info=True)

    async def run_connected(self, device) -> None:
        self.controller = WalkingPadController(ble_device=device)
        self.controller.register_status_callback(self.on_status)
        self.controller.register_disconnect_callback(self.on_disconnect)
        await self.controller.connect()
        log.info("Connected via %s", self.controller.protocol.value)
        self.write_status(force=True, connected=True)
        await self.fetch_last_run()

        while self.controller.connected:
            self.maybe_rebuild_rollup()
            if self.reload_config_if_changed() and not self.is_selected(device):
                log.info("Selection changed, dropping %s", device.address)
                return
            try:
                # Polls the pad on WiLink; fires a synthetic update from the
                # notification cache on FTMS.
                await self.controller.update_state()
            except Exception:
                log.warning("Status poll failed", exc_info=True)
                if not self.controller.connected:
                    break
            await asyncio.sleep(self.poll_interval)

    def is_selected(self, device) -> bool:
        pinned = self.pinned_address
        return not pinned or device.address.lower() == pinned

    def on_disconnect(self) -> None:
        log.info("Pad disconnected")
        self.close_session()
        self.write_status(force=True, connected=False)

    async def cleanup(self) -> None:
        self.close_session()
        if self.controller:
            try:
                await self.controller.disconnect()
            except Exception:
                log.debug("Disconnect failed", exc_info=True)
        self.controller = None
        self.last_steps = 0
        self.last_history_key = None
        self.write_status(force=True, connected=False)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.maybe_rebuild_rollup()
            self.reload_config_if_changed()
            try:
                device = await self.scan_for_pad()
                if device is None:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                await self.run_connected(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Connection cycle failed")
            finally:
                await self.cleanup()
            if not stop.is_set():
                await asyncio.sleep(RETRY_DELAY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    collector = Collector(load_config())

    async def amain() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        task = asyncio.create_task(collector.run(stop))
        await stop.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await collector.cleanup()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    log.info("Collector stopped")


if __name__ == "__main__":
    main()
