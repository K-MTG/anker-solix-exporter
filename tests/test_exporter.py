"""Tests that need no Bluetooth. Field encodings are from a real F2000 on firmware 5.2.3; the serial is made up."""

import asyncio
from types import SimpleNamespace

import pytest
from bleak.backends.device import BLEDevice
from SolixBLE.devices import f2600
from SolixBLE.devices.f2000 import F2000

import exporter


class Param:
    def __init__(self, hex_value):
        self.value_legacy = bytes.fromhex(hex_value)


def params(**fields):
    return {key: Param(value) for key, value in fields.items()}


ON_MAINS = params(a4="02311e", a5="020000", a6="020000", b0="020000", b3="020b02", bb="020100", bc="0102", bd="0120",
                  bf="0100", c1="0164", c3="0164", d0="01" + b"TESTSERIAL000000".hex())
ON_BATTERY = params(a4="02f301", a6="029801", b0="029801", bb="020100", bc="0100", bf="0101", c1="0163")
STATUS_REPLY = params(bb="020100", d2="020000", db="0100")


def config(**overrides):
    return exporter.Config(mac="AA:BB", ups_name="fh-sc-ups1", client_token="t", **overrides)


def metrics(*packets):
    readings = {}
    for packet in packets:
        readings.update(exporter.decode(packet))
    return exporter.render_metrics("fh-sc-ups1", readings, exporter.Stats(), True, 1.0)


def test_decode_real_values():
    r = exporter.decode(ON_MAINS)
    assert r["battery_percent"] == 100
    assert r["temperature"] == 32
    assert r["ac_output_on"] == 1
    assert r["input_state"] == 2
    assert r["firmware"] == 523
    assert r["serial"] == "TESTSERIAL000000"
    assert "power_saving" not in r


def test_negative_temperature():
    assert exporter.read_int(params(bd="01f6"), "bd", signed=True) == -10


def test_on_mains_mirrors_the_nut_shape_with_an_anker_prefix():
    text = metrics(ON_MAINS)
    assert 'anker_status{ups="fh-sc-ups1"} 1' in text
    assert 'anker_ups_status{status="OL",ups="fh-sc-ups1"} 1' in text
    assert 'anker_ups_status{status="OB",ups="fh-sc-ups1"} 0' in text
    assert 'anker_battery_charge{ups="fh-sc-ups1"} 1.0' in text
    assert 'ups_firmware="5.2.3"' in text


def test_time_remaining_only_as_runtime_on_battery():
    assert "anker_battery_runtime_seconds" not in metrics(ON_MAINS)
    assert 'anker_battery_runtime_seconds{ups="fh-sc-ups1"} 179' in metrics(ON_BATTERY)


def test_on_battery_raises_the_flag_the_alert_uses():
    text = metrics(ON_BATTERY)
    assert 'anker_ups_status{status="OB",ups="fh-sc-ups1"} 1' in text
    assert 'anker_status{ups="fh-sc-ups1"} 2' in text
    assert 'anker_ups_status{status="DISCHRG",ups="fh-sc-ups1"} 1' in text


def test_solar_only_counts_as_on_battery():
    assert 'anker_ups_status{status="OB",ups="fh-sc-ups1"} 1' in metrics(params(bc="0101"))


def test_zero_watts_is_not_reported_as_real_power():
    assert "anker_real_power_watts" not in metrics(ON_MAINS)
    assert 'anker_real_power_watts{ups="fh-sc-ups1"} 408' in metrics(ON_BATTERY)


def test_no_replace_battery_flag_is_invented():
    assert 'status="RB"' not in metrics(ON_MAINS, ON_BATTERY)


def test_settings_only_after_a_status_reply():
    assert "anker_power_saving_enabled" not in metrics(ON_MAINS)
    assert 'anker_power_saving_enabled{ups="fh-sc-ups1"} 0' in metrics(ON_MAINS, STATUS_REPLY)


def make_device(monkeypatch):
    sent = []

    async def fake_send(self, cmd=None, parameters=None, **kwargs):
        sent.append(cmd)

    monkeypatch.setattr(F2000, "_send_command", fake_send)
    device = exporter.Device.__new__(exporter.Device)
    device._ac_on_allowed = False
    return device, sent


@pytest.mark.parametrize(
    ("cmd", "parameters"),
    [
        (f2600.CMD_AC_OUTPUT, f2600.PARAMETERS_OFF),
        (f2600.CMD_AC_OUTPUT, f2600.PARAMETERS_ON),  # on, but not through turn_ac_on()
        (f2600.CMD_DC_OUTPUT, f2600.PARAMETERS_OFF),
        (f2600.CMD_DC_OUTPUT, f2600.PARAMETERS_ON),
        (f2600.CMD_POWER_SAVING_MODE, f2600.PARAMETERS_ON),
        (f2600.CMD_POWER_SAVING_MODE, f2600.PARAMETERS_OFF),
        (f2600.CMD_LIGHT_MODE, f2600.PARAMETERS_OFF),
    ],
)
def test_everything_else_is_refused(monkeypatch, cmd, parameters):
    device, sent = make_device(monkeypatch)
    with pytest.raises(PermissionError):
        asyncio.run(device._send_command(cmd=cmd, parameters=parameters))
    assert sent == []


def test_ac_off_refused_even_right_after_ac_on_was_allowed(monkeypatch):
    device, sent = make_device(monkeypatch)
    device._ac_on_allowed = True
    with pytest.raises(PermissionError):
        asyncio.run(device._send_command(cmd=f2600.CMD_AC_OUTPUT, parameters=f2600.PARAMETERS_OFF))
    assert sent == []


def test_turn_ac_on_sends_on_once(monkeypatch):
    device, sent = make_device(monkeypatch)
    asyncio.run(device.turn_ac_on())
    assert sent == [f2600.CMD_AC_OUTPUT]
    with pytest.raises(PermissionError):
        asyncio.run(device._send_command(cmd=f2600.CMD_AC_OUTPUT, parameters=f2600.PARAMETERS_ON))


def test_status_request_is_allowed(monkeypatch):
    device, sent = make_device(monkeypatch)
    asyncio.run(device._send_command(cmd=f2600.CMD_GET_STATUS, parameters=f2600.PARAMETERS_GET_STATUS))
    assert sent == [f2600.CMD_GET_STATUS]


def test_exporter_never_imports_an_off_command():
    assert not hasattr(exporter, "PARAMETERS_OFF")


def test_ac_on_waits_for_the_grace_period_then_backs_off():
    c = config(ac_keep_on=True, ac_on_after_seconds=60)
    s = exporter.Stats()
    now = 1000
    assert not exporter.ac_on_due(c, s, 0, True, now)
    assert not exporter.ac_on_due(c, s, 0, True, now + 59)
    now += 60
    # Attempts then gaps of 5 minutes, 15 minutes, and an hour from there on.
    for gap in (300, 900, 3600, 3600, 3600):
        assert exporter.ac_on_due(c, s, 0, True, now)
        s.ac_on_last_sent = now
        s.ac_on_attempts += 1
        assert not exporter.ac_on_due(c, s, 0, True, now + gap - 1)
        now += gap


def test_backoff_starts_over_once_the_outlets_have_come_back():
    c = config(ac_keep_on=True, ac_on_after_seconds=60)
    s = exporter.Stats(ac_off_since=1000, ac_on_last_sent=2000, ac_on_attempts=4)
    assert not exporter.ac_on_due(c, s, 1, True, 2001)
    assert s.ac_on_attempts == 0
    assert s.ac_on_last_sent is None
    assert not exporter.ac_on_due(c, s, 0, True, 2002)
    assert exporter.ac_on_due(c, s, 0, True, 2062)


def test_ac_on_defaults_to_a_one_minute_wait():
    c = exporter.Config.from_env({"ANKER_MAC": "aa:bb", "ANKER_UPS_NAME": "u", "ANKER_CLIENT_TOKEN": "t"})
    assert c.ac_on_after_seconds == 60
    assert c.mac == "AA:BB"
    assert not c.ac_keep_on


def test_watcher_turns_ac_on_within_about_a_minute(monkeypatch):
    calls = []

    class Fake:
        readings = {"ac_output_on": 0}
        link_up = True

        async def turn_ac_on(self):
            calls.append("on")

    async def scenario():
        e = exporter.Exporter(config(ac_keep_on=True, ac_on_after_seconds=60))
        e.device = Fake()
        now = [1000.0]
        monkeypatch.setattr(exporter.time, "monotonic", lambda: now[0])
        await e.keep_ac_on(e.device)
        assert calls == []
        now[0] = 1059.0
        await e.keep_ac_on(e.device)
        assert calls == []
        now[0] = 1060.0
        await e.keep_ac_on(e.device)
        assert calls == ["on"]
        assert e.stats.ac_on_commands == 1

    asyncio.run(scenario())


def test_ac_on_timer_resets_when_outlets_come_back():
    c = config(ac_keep_on=True, ac_on_after_seconds=300)
    s = exporter.Stats()
    exporter.ac_on_due(c, s, 0, True, 1000)
    assert not exporter.ac_on_due(c, s, 1, True, 1100)
    assert s.ac_off_since is None
    assert not exporter.ac_on_due(c, s, 0, True, 1350)


@pytest.mark.parametrize(
    ("keep_on", "state", "link"),
    [(False, 0, True), (True, 1, True), (True, 0, False), (True, None, True)],
)
def test_ac_on_never_sent_when_off_unknown_or_disabled(keep_on, state, link):
    c = config(ac_keep_on=keep_on, ac_on_after_seconds=0)
    assert not exporter.ac_on_due(c, exporter.Stats(), state, link, 1000)


def test_metrics_fail_once_nothing_has_arrived_for_too_long():
    c = config(stale_seconds=120)
    recent = SimpleNamespace(readings={"x": 1}, last_telemetry=1000)
    empty = SimpleNamespace(readings={}, last_telemetry=1000)
    never = SimpleNamespace(readings={"x": 1}, last_telemetry=None)
    assert exporter.metrics_healthy(recent, c, 1119)
    assert not exporter.metrics_healthy(recent, c, 1120)
    assert not exporter.metrics_healthy(empty, c, 1000)
    assert not exporter.metrics_healthy(never, c, 1000)
    assert not exporter.metrics_healthy(None, c, 1000)


class FakeScanner:
    scans = 0

    @classmethod
    async def discover(cls, timeout, return_adv):
        cls.scans += 1
        await asyncio.sleep(0.01)  # a real scan yields to the event loop
        return {"AA:BB": (object(), object())}


class FakeDevice:
    built = []

    def __init__(self, ble_device, advertisement, client_token, previous=None):
        FakeDevice.built.append(self)
        self.negotiated = True
        self.connected = True
        self.awaiting_button = False
        self.announcement = None
        self.link_down_since = None
        self.last_telemetry = None
        self.readings = {}
        self.dropped = False

    @property
    def link_up(self):
        return self.negotiated

    async def connect(self):
        return True

    async def disconnect(self):
        self.dropped = True


@pytest.fixture
def fake_ble(monkeypatch):
    FakeDevice.built = []
    FakeScanner.scans = 0
    monkeypatch.setattr(exporter, "BleakScanner", FakeScanner)
    monkeypatch.setattr(exporter, "Device", FakeDevice)
    monkeypatch.setattr(exporter, "LINK_CHECK_SECONDS", 0.01)


def supervise_until(change, config_overrides, ticks=30):
    async def scenario():
        e = exporter.Exporter(config(**config_overrides))
        task = asyncio.create_task(e.supervise())
        await asyncio.sleep(0.05)
        change(FakeDevice.built[0])
        await asyncio.sleep(ticks * 0.01)
        e.stopping.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_rescans_when_bluez_has_forgotten_the_device(fake_ble):
    def bluetoothd_restarted(device):
        device.negotiated = False
        device.connected = False
        device.link_down_since = 0

    supervise_until(bluetoothd_restarted, {"rediscover_after_seconds": 60})
    assert FakeScanner.scans >= 2
    assert len(FakeDevice.built) >= 2
    assert FakeDevice.built[0].dropped


def test_no_rescan_before_the_link_has_been_down_long_enough(fake_ble):
    def just_dropped(device):
        device.negotiated = False
        device.link_down_since = exporter.time.monotonic()

    supervise_until(just_dropped, {"rediscover_after_seconds": 60})
    assert FakeScanner.scans == 1
    assert not FakeDevice.built[0].dropped


def test_no_rescan_while_the_unit_waits_for_its_button(fake_ble):
    def waiting(device):
        device.negotiated = False
        device.awaiting_button = True
        device.link_down_since = 0

    supervise_until(waiting, {"rediscover_after_seconds": 0})
    assert FakeScanner.scans == 1
    assert not FakeDevice.built[0].dropped


def test_replacement_device_keeps_readings_and_the_stale_timer():
    ble = BLEDevice("AA:BB", "x", None)
    first = SimpleNamespace(readings={"battery_percent": 99}, last_telemetry=7.0, link_down_since=5.0)
    device = exporter.Device(ble, None, "token", previous=first)
    assert device.readings == {"battery_percent": 99}
    assert device.link_down_since == 5.0
    assert device._client_token == "token"


def test_no_metric_uses_the_nut_prefix():
    text = metrics(ON_MAINS, ON_BATTERY, STATUS_REPLY)
    assert all(line.startswith("anker_") for line in text.splitlines())


def test_rescans_when_the_link_looks_up_but_has_gone_quiet(fake_ble):
    def quiet(device):
        device.last_telemetry = exporter.time.monotonic() - 1000

    supervise_until(quiet, {"stale_seconds": 0})
    assert FakeScanner.scans >= 2
    assert FakeDevice.built[0].dropped


def test_no_rescan_while_data_keeps_arriving(fake_ble):
    def fresh(device):
        device.last_telemetry = exporter.time.monotonic()

    supervise_until(fresh, {"stale_seconds": 60})
    assert FakeScanner.scans == 1


def test_scan_errors_are_survived_while_bluez_is_down(fake_ble, monkeypatch):
    class Failing:
        calls = 0

        @classmethod
        async def discover(cls, timeout, return_adv):
            cls.calls += 1
            if cls.calls == 1:
                raise RuntimeError("org.bluez.Error.NotReady")
            return {"AA:BB": (object(), object())}

    monkeypatch.setattr(exporter, "BleakScanner", Failing)
    monkeypatch.setattr(exporter, "SETTLE_SECONDS", 0)

    async def scenario():
        e = exporter.Exporter(config())
        await asyncio.wait_for(e.connect(), 2)
        assert e.device is FakeDevice.built[0]

    asyncio.run(scenario())
    assert Failing.calls == 2


def test_a_crashed_loop_ends_the_process_so_docker_restarts_it(fake_ble, monkeypatch):
    async def boom(self):
        raise RuntimeError("bug")

    monkeypatch.setattr(exporter.Exporter, "poll", boom)

    async def scenario():
        e = exporter.Exporter(config(listen_port=0))
        return await asyncio.wait_for(e.run(), 3)

    assert asyncio.run(scenario()) == 1


def test_clean_stop_exits_zero(fake_ble):
    async def scenario():
        e = exporter.Exporter(config(listen_port=0))
        task = asyncio.create_task(e.run())
        await asyncio.sleep(0.05)
        e.stopping.set()
        return await asyncio.wait_for(task, 3)

    assert asyncio.run(scenario()) == 0


def test_status_listener_is_registered_before_the_request_goes_out(monkeypatch):
    order = []

    async def fake_listen(self, pattern, cmd, timeout=10):
        order.append("listen")
        await asyncio.sleep(0.01)
        return None

    async def fake_send(self, cmd=None, parameters=None, **kwargs):
        order.append("send")

    monkeypatch.setattr(F2000, "_send_command", fake_send)
    monkeypatch.setattr(exporter.Device, "_listen_for_packet", fake_listen)
    device = exporter.Device.__new__(exporter.Device)
    device._ac_on_allowed = False
    with pytest.raises(TimeoutError):
        asyncio.run(device.request_status())
    assert order == ["listen", "send"]


def test_off_time_and_failures_are_exported():
    s = exporter.Stats(ac_off_since=exporter.time.monotonic() - 90, ac_on_failures=2)
    text = exporter.render_metrics("u", {}, s, True, None)
    assert 'anker_ac_off_seconds{ups="u"} 90' in text
    assert 'anker_ac_on_failures_total{ups="u"} 2' in text
