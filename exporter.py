"""Prometheus exporter for an Anker SOLIX F2000 used as a UPS, over Bluetooth.

Uses SolixBLE to talk to the unit, keeps the latest readings, and serves them on /metrics using the
same metric names as the NUT exporter where the meaning matches. It can also turn the AC outlets back
on. It can never turn anything off: see Device._send_command.
"""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field

from bleak import BleakScanner
from SolixBLE import F2000
from SolixBLE.const import TELEMETRY_PATTERN_A
from SolixBLE.constructs import Parameters
from SolixBLE.devices.f2600 import (
    CMD_AC_OUTPUT,
    CMD_GET_STATUS,
    CMD_RESPONSE_GET_STATUS,
    PARAMETERS_GET_STATUS,
    PARAMETERS_ON,
)

_LOGGER = logging.getLogger("anker_solix_exporter")

# The unit waits this long for its button once it has announced a window (180 s on firmware 5.2.3).
NEGOTIATION_DEADLINE_SECONDS = 240
# A quick retry after a failed handshake got replies that wouldn't decrypt; ~40 s of quiet worked.
SETTLE_SECONDS = 45
LINK_CHECK_SECONDS = 5

STATUS_AWAITING_BUTTON = 0x09
SECONDS_PER_REMAINING_UNIT = 360  # a4 counts tenths of an hour

# bc: 0 none, 1 solar, 2 AC, 3 both. Only AC means mains is present.
AC_INPUT_STATES = (2, 3)
BATTERY_DISCHARGING = 1
BATTERY_CHARGING = 2


@dataclass
class Config:
    mac: str
    ups_name: str
    client_token: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 9996
    poll_seconds: int = 60
    stale_seconds: int = 120
    rediscover_after_seconds: int = 60
    ac_keep_on: bool = False
    ac_on_after_seconds: int = 60
    ac_on_retry_seconds: int = 300

    @classmethod
    def from_env(cls, env=os.environ):
        return cls(
            mac=env["ANKER_MAC"].upper(),
            ups_name=env["ANKER_UPS_NAME"],
            client_token=env["ANKER_CLIENT_TOKEN"],
            listen_host=env.get("ANKER_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(env.get("ANKER_LISTEN_PORT", "9996")),
            poll_seconds=int(env.get("ANKER_POLL_SECONDS", "60")),
            stale_seconds=int(env.get("ANKER_STALE_SECONDS", "120")),
            rediscover_after_seconds=int(env.get("ANKER_REDISCOVER_AFTER_SECONDS", "60")),
            ac_keep_on=env.get("ANKER_AC_KEEP_ON", "false").lower() == "true",
            ac_on_after_seconds=int(env.get("ANKER_AC_ON_AFTER_SECONDS", "60")),
            ac_on_retry_seconds=int(env.get("ANKER_AC_ON_RETRY_SECONDS", "300")),
        )


@dataclass
class Stats:
    status_ok: int = 0
    status_failed: int = 0
    ac_on_commands: int = 0
    ac_off_since: float | None = None
    ac_on_last_sent: float | None = None
    connected_once: bool = field(default=False)


def read_int(parameters, key, signed=False):
    """Read a numeric field: one type byte, then a little-endian value. None if absent or empty."""
    if key not in parameters:
        return None
    raw = parameters[key].value_legacy
    if len(raw) < 2:
        return None
    return int.from_bytes(raw[1:], "little", signed=signed)


def read_text(parameters, key):
    if key not in parameters:
        return None
    return parameters[key].value_legacy[1:].decode("ascii", "ignore").rstrip("\0")


def decode(parameters):
    """Pick the fields we use out of a telemetry or status packet. Absent fields are left out."""
    fields = {
        "time_remaining": read_int(parameters, "a4"),
        "input_watts": read_int(parameters, "a5"),
        "ac_output_watts": read_int(parameters, "a6"),
        "total_output_watts": read_int(parameters, "b0"),
        "firmware": read_int(parameters, "b3"),
        "ac_output_on": read_int(parameters, "bb"),
        "input_state": read_int(parameters, "bc"),
        "temperature": read_int(parameters, "bd", signed=True),
        "battery_state": read_int(parameters, "bf"),
        "battery_percent": read_int(parameters, "c1"),
        "battery_health": read_int(parameters, "c3"),
        "serial": read_text(parameters, "d0"),
        # Only in status replies, not in pushed telemetry.
        "auto_off_minutes": read_int(parameters, "d2"),
        "power_saving": read_int(parameters, "db"),
    }
    return {name: value for name, value in fields.items() if value is not None}


class Device(F2000):
    """The F2000, limited to what this exporter needs."""

    def __init__(self, ble_device, advertisement, client_token, previous=None):
        super().__init__(ble_device, advertisement)
        self._client_token = client_token
        # A replacement after a rescan carries on from the old device, so a brief recovery doesn't blank the
        # metrics, and the stale timer keeps counting if the unit is really gone.
        self.readings = previous.readings if previous else {}
        self.last_telemetry = previous.last_telemetry if previous else None
        self.link_down_since = (previous.link_down_since if previous else None) or time.monotonic()
        self._ac_on_allowed = False

    @property
    def awaiting_button(self):
        return getattr(self.announcement, "registration_status", None) == STATUS_AWAITING_BUTTON

    def _negotiation_should_restart(self):
        # Restarting the handshake while the unit waits for its button would void a press in progress.
        return False if self.awaiting_button else super()._negotiation_should_restart()

    def _negotiation_deadline(self):
        return NEGOTIATION_DEADLINE_SECONDS

    async def _send_command(self, cmd=None, parameters=None, **kwargs):
        # This unit is a UPS: a stray "off" would cut power to whatever it protects. Only the two
        # exact commands below can leave this process, and "AC on" only from turn_ac_on().
        ac_on = self._ac_on_allowed and cmd == CMD_AC_OUTPUT and parameters is PARAMETERS_ON
        status = cmd == CMD_GET_STATUS and parameters is PARAMETERS_GET_STATUS
        if not (ac_on or status):
            _LOGGER.error("Refused to send command %s", cmd)
            raise PermissionError(f"command {cmd} is not allowed")
        self._ac_on_allowed = False
        return await super()._send_command(cmd=cmd, parameters=parameters, **kwargs)

    async def turn_ac_on(self):
        self._ac_on_allowed = True
        await self._send_command(cmd=CMD_AC_OUTPUT, parameters=PARAMETERS_ON)

    async def request_status(self):
        await self._send_command(cmd=CMD_GET_STATUS, parameters=PARAMETERS_GET_STATUS)
        payload = await self._listen_for_packet(
            bytes.fromhex(TELEMETRY_PATTERN_A),
            bytes.fromhex(CMD_RESPONSE_GET_STATUS),
        )
        if not payload:
            raise TimeoutError("no reply to the status request")
        await self._process_telemetry(Parameters.parse(payload))

    async def _process_telemetry(self, parameters, *args, **kwargs):
        self.readings.update(decode(parameters))
        self.last_telemetry = time.monotonic()
        return await super()._process_telemetry(parameters, *args, **kwargs)

    @property
    def link_up(self):
        up = self.negotiated
        if up:
            self.link_down_since = None
        elif self.link_down_since is None:
            self.link_down_since = time.monotonic()
        return up


def ac_on_due(config, stats, ac_output_on, link_up, now):
    """Decide whether to send AC on, tracking how long the outlets have been off."""
    if ac_output_on != 0:
        stats.ac_off_since = None
        return False
    if stats.ac_off_since is None:
        stats.ac_off_since = now
    if not config.ac_keep_on or not link_up:
        return False
    if now - stats.ac_off_since < config.ac_on_after_seconds:
        return False
    return stats.ac_on_last_sent is None or now - stats.ac_on_last_sent >= config.ac_on_retry_seconds


def _line(name, value, **labels):
    if labels:
        body = ",".join(
            '{}="{}"'.format(key, str(val).replace("\\", "\\\\").replace('"', '\\"'))
            for key, val in sorted(labels.items())
        )
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def render_metrics(ups, readings, stats, link_up, telemetry_age):
    """Render the readings. NUT names are used only where they mean the same as for the other UPSes."""
    r = readings
    out = []

    def nut(name, value, **labels):
        out.append(_line(name, value, ups=ups, **labels))

    def add(name, key, scale=1):
        if key in r:
            nut(name, r[key] * scale)

    info = {"manufacturer": "Anker", "model": "SOLIX F2000", "description": "Anker SOLIX F2000", "device_type": "ups"}
    if "serial" in r:
        info["serial"] = r["serial"]
    if "firmware" in r:
        info["ups_firmware"] = ".".join(str(r["firmware"]))
    nut("nut_ups_info", 1, **info)

    if "input_state" in r:
        on_mains = r["input_state"] in AC_INPUT_STATES
        nut("nut_status", 1 if on_mains else 2)
        nut("nut_ups_status", 1 if on_mains else 0, status="OL")
        nut("nut_ups_status", 0 if on_mains else 1, status="OB")
    if "battery_state" in r:
        nut("nut_ups_status", 1 if r["battery_state"] == BATTERY_CHARGING else 0, status="CHRG")
        nut("nut_ups_status", 1 if r["battery_state"] == BATTERY_DISCHARGING else 0, status="DISCHRG")
    if "battery_percent" in r:
        nut("nut_battery_charge", r["battery_percent"] / 100)
    # Time remaining is noise on mains at 100%, and time to full while charging.
    if r.get("battery_state") == BATTERY_DISCHARGING and "time_remaining" in r:
        nut("nut_battery_runtime_seconds", r["time_remaining"] * SECONDS_PER_REMAINING_UNIT)
    # 0 here means below what the unit reports (about 27 W read 0), not "no load", so leave it out.
    if r.get("ac_output_watts"):
        nut("nut_real_power_watts", r["ac_output_watts"])

    add("anker_input_watts", "input_watts")
    add("anker_ac_output_watts", "ac_output_watts")
    add("anker_total_output_watts", "total_output_watts")
    add("anker_ac_output_on", "ac_output_on")
    add("anker_input_state", "input_state")
    add("anker_battery_state", "battery_state")
    add("anker_temperature_celsius", "temperature")
    add("anker_power_saving_enabled", "power_saving")
    add("anker_auto_off_minutes", "auto_off_minutes")
    add("anker_time_remaining_seconds", "time_remaining", SECONDS_PER_REMAINING_UNIT)
    if "battery_health" in r:
        nut("anker_battery_health_ratio", r["battery_health"] / 100)

    nut("anker_ble_connected", 1 if link_up else 0)
    if telemetry_age is not None:
        nut("anker_last_telemetry_age_seconds", round(telemetry_age, 1))
    nut("anker_status_requests_total", stats.status_ok, result="ok")
    nut("anker_status_requests_total", stats.status_failed, result="failed")
    nut("anker_ac_on_commands_total", stats.ac_on_commands)
    return "\n".join(out) + "\n"


def metrics_healthy(device, config, now):
    """False once the link has been down too long, so the scrape fails and `up` goes to 0."""
    if device is None or not device.readings:
        return False
    if device.link_up:
        return True
    return device.link_down_since is not None and now - device.link_down_since < config.stale_seconds


class Exporter:
    def __init__(self, config):
        self.config = config
        self.stats = Stats()
        self.device = None
        self.stopping = asyncio.Event()

    async def supervise(self):
        """Stay connected, starting over with a fresh scan when SolixBLE's own reconnects aren't working.

        SolixBLE reconnects using the device object from the first scan. A bluetoothd restart (e.g. an
        unattended bluez upgrade) makes BlueZ forget every device, and that object then fails with
        "not found" until something scans again, which SolixBLE never does.
        """
        while not self.stopping.is_set():
            await self.connect()
            await self.wait_until_link_lost()
            if not self.stopping.is_set():
                _LOGGER.warning(
                    "Link down for over %d s: scanning again", self.config.rediscover_after_seconds,
                )
                await self.drop(self.device)

    async def connect(self):
        """Keep trying until the unit accepts us."""
        while not self.stopping.is_set():
            found = await BleakScanner.discover(timeout=30, return_adv=True)
            hit = found.get(self.config.mac)
            if hit is None:
                _LOGGER.warning("%s not seen in this scan", self.config.mac)
                continue
            device = Device(hit[0], hit[1], self.config.client_token, previous=self.device)
            self.device = device
            try:
                connected = await device.connect()
            except Exception:
                _LOGGER.exception("Connecting failed")
                connected = False
            if connected and device.negotiated:
                _LOGGER.info("Connected to %s", self.config.mac)
                self.stats.connected_once = True
                return
            await self.drop(device)
            await asyncio.sleep(SETTLE_SECONDS)

    async def wait_until_link_lost(self):
        device = self.device
        while not self.stopping.is_set():
            # While connected and waiting for the button, a rescan would void a press in progress.
            waiting = device.connected and device.awaiting_button
            if not device.link_up and not waiting:
                if time.monotonic() - device.link_down_since >= self.config.rediscover_after_seconds:
                    return
            await asyncio.sleep(LINK_CHECK_SECONDS)

    async def drop(self, device):
        """Stop a device's reconnect task and link. Also what lets the unit advertise again."""
        try:
            await asyncio.wait_for(device.disconnect(), 5)
        except Exception as error:
            _LOGGER.warning("Disconnecting failed: %s", error)

    async def announce_button(self):
        told = False
        while True:
            device = self.device
            waiting = device is not None and device.connected and device.awaiting_button
            if waiting and not told:
                window = getattr(device.announcement, "confirmation_window", None)
                _LOGGER.warning(
                    "New client token: hold the pairing button on the unit while its light is solid (window %s s)",
                    window,
                )
            told = waiting
            await asyncio.sleep(1)

    async def poll(self):
        """Ask for a full status now and then: settings only come this way, and values re-read."""
        while not self.stopping.is_set():
            device = self.device
            if device is not None and device.link_up:
                try:
                    await device.request_status()
                    self.stats.status_ok += 1
                except Exception as error:
                    self.stats.status_failed += 1
                    _LOGGER.warning("Status request failed: %s", error)
            try:
                await asyncio.wait_for(self.stopping.wait(), self.config.poll_seconds)
            except TimeoutError:
                pass

    async def watch_ac(self):
        """Check often, not just at each status poll: the unit pushes AC changes as they happen."""
        while not self.stopping.is_set():
            if self.device is not None:
                await self.keep_ac_on(self.device)
            await asyncio.sleep(LINK_CHECK_SECONDS)

    async def keep_ac_on(self, device):
        now = time.monotonic()
        if not ac_on_due(self.config, self.stats, device.readings.get("ac_output_on"), device.link_up, now):
            return
        _LOGGER.warning("AC outlets have been off for %.0f s: turning them on", now - self.stats.ac_off_since)
        self.stats.ac_on_last_sent = now
        self.stats.ac_on_commands += 1
        try:
            await device.turn_ac_on()
        except Exception as error:
            _LOGGER.warning("AC on failed: %s", error)

    async def handle_http(self, reader, writer):
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            path = request.split()[1].decode() if len(request.split()) > 1 else ""
            status, body = self.respond(path)
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; version=0.0.4\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            writer.close()

    def respond(self, path):
        device = self.device
        now = time.monotonic()
        if path == "/metrics":
            if not metrics_healthy(device, self.config, now):
                return "503 Service Unavailable", "no recent data from the unit\n"
            age = now - device.last_telemetry if device.last_telemetry else None
            return "200 OK", render_metrics(self.config.ups_name, device.readings, self.stats, device.link_up, age)
        if path == "/health":
            state = {
                "connected": bool(device and device.link_up),
                "awaiting_button": bool(device and device.awaiting_button),
                "have_data": bool(device and device.readings),
            }
            return "200 OK", json.dumps(state) + "\n"
        return "404 Not Found", "try /metrics or /health\n"

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        server = await asyncio.start_server(self.handle_http, self.config.listen_host, self.config.listen_port)
        tasks = [
            asyncio.create_task(self.supervise()),
            asyncio.create_task(self.poll()),
            asyncio.create_task(self.watch_ac()),
            asyncio.create_task(self.announce_button()),
        ]
        await self.stopping.wait()
        for task in tasks:
            task.cancel()
        server.close()
        # Drop the link cleanly, or the unit stays hidden from the next scan.
        if self.device is not None:
            await self.drop(self.device)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("SolixBLE").setLevel(logging.WARNING)
    asyncio.run(Exporter(Config.from_env()).run())


if __name__ == "__main__":
    main()
