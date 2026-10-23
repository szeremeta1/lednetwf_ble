"""Keep-connected mode: commands should not pay for a Bluetooth connect.

Without it the device connects on demand and drops the link after `disconnect_delay` idle seconds,
so the first command after a pause waits for connect + notification setup (0.2-4 s on a Pi 5,
measured; much longer when another device is connecting at the same time).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import RADIO, FakeHass, Radio

import custom_components.lednetwf_ble as integration
from custom_components.lednetwf_ble import device as device_mod
from custom_components.lednetwf_ble.const import DOMAIN

MAC = "08:65:F0:28:47:08"
IDLE = 0.05  # disconnect_delay used by the tests (seconds)


@pytest.fixture(autouse=True)
def fresh_radio(monkeypatch):
    RADIO.__dict__.update(Radio().__dict__)
    RADIO.attempt_times = []
    real = Radio.establish_connection

    async def timed(self, *a, **kw):
        RADIO.attempt_times.append(time.monotonic())
        return await real(self, *a, **kw)

    monkeypatch.setattr(Radio, "establish_connection", timed)
    monkeypatch.setattr(device_mod, "KEEP_CONNECTED_RETRY_MIN", 0.02, raising=False)
    monkeypatch.setattr(device_mod, "KEEP_CONNECTED_RETRY_MAX", 0.2, raising=False)
    yield


def run(test):
    async def main():
        hass = FakeHass(asyncio.get_running_loop())
        dev = device_mod.LEDNetWFDevice(hass, MAC, "Sun Lamp", 0x27, disconnect_delay=IDLE)
        try:
            await test(hass, dev)
        finally:
            await dev.stop()
            await asyncio.sleep(0)

    asyncio.run(main())


async def until(cond, timeout=1.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def connected(dev):
    """Linked and done with connection setup (notifications on)."""
    return (dev._client is not None and dev._client.is_connected
            and not dev._connect_lock.locked())


async def send(dev):
    return await dev._send_command(bytearray(b"\x00\x00\x80\x00\x00\x0d\x0e\x0b\x3b\xa1"))


def test_default_mode_still_disconnects_when_idle():
    async def t(hass, dev):
        assert await send(dev)
        assert RADIO.attempts == 1 and connected(dev)
        await asyncio.sleep(IDLE * 4)
        assert not connected(dev)
        assert RADIO.attempts == 1, "default mode must not reconnect by itself"

    run(t)


def test_keep_connected_connects_in_background_and_stays():
    async def t(hass, dev):
        dev.set_keep_connected(True)
        assert dev.keep_connected
        await until(lambda: connected(dev))
        assert RADIO.attempts == 1 and dev._client.notifying
        assert await send(dev)
        await asyncio.sleep(IDLE * 4)
        assert connected(dev), "keep-connected must not drop the link when idle"
        assert RADIO.attempts == 1
        assert len(RADIO.clients[0].writes) == 1

    run(t)


def test_command_does_not_wait_for_connect_when_kept_connected():
    async def t(hass, dev):
        RADIO.connect_delay = 0.3
        t0 = time.monotonic()
        assert await send(dev)  # on demand: pays the connect
        on_demand = time.monotonic() - t0
        await asyncio.sleep(IDLE * 4)  # idle -> link dropped
        assert not connected(dev)

        dev.set_keep_connected(True)
        await until(lambda: connected(dev), timeout=2)
        await asyncio.sleep(IDLE * 4)  # a long pause before the next "button press"
        t0 = time.monotonic()
        assert await send(dev)
        kept = time.monotonic() - t0
        assert on_demand >= 0.3
        assert kept < 0.05, f"command waited {kept:.3f} s with the link already up"

    run(t)


def test_keep_connected_reconnects_after_the_link_drops():
    async def t(hass, dev):
        dev.set_keep_connected(True)
        await until(lambda: connected(dev))
        RADIO.clients[0].drop()  # e.g. supervision timeout
        assert not connected(dev)
        await until(lambda: connected(dev))
        assert RADIO.attempts == 2 and dev._client is RADIO.clients[1]

    run(t)


def test_link_lost_during_connection_setup_is_retried():
    async def t(hass, dev):
        RADIO.drop_during_setup = 1
        dev.set_keep_connected(True)
        await until(lambda: connected(dev), timeout=2)
        assert RADIO.attempts == 2

    run(t)


def test_command_fails_cleanly_if_link_is_lost_during_setup():
    async def t(hass, dev):
        RADIO.drop_during_setup = 1
        assert await send(dev) is False  # a failed send, not an AttributeError on None
        assert await send(dev) is True
        assert RADIO.attempts == 2

    run(t)


def test_failed_reconnects_back_off():
    async def t(hass, dev):
        RADIO.fail_next = 2
        dev.set_keep_connected(True)
        await until(lambda: connected(dev), timeout=2)
        assert RADIO.attempts == 3
        gaps = [b - a for a, b in zip(RADIO.attempt_times, RADIO.attempt_times[1:])]
        assert gaps[0] >= 0.015 and gaps[1] >= 0.035, gaps

    run(t)


def test_absent_device_waits_for_its_next_advertisement():
    async def t(hass, dev):
        RADIO.present = False
        RADIO.advertising = False
        dev.set_keep_connected(True)
        await asyncio.sleep(0.1)
        assert RADIO.attempts == 0, "no radio time spent on a device that isn't advertising"
        RADIO.present = True
        RADIO.advertising = True
        dev.on_advertisement()
        await until(lambda: connected(dev))
        assert RADIO.attempts == 1

    run(t)


def test_unplugged_lamp_gets_no_blind_reconnects():
    """After the link drops, connect only once the lamp is heard again.

    HA keeps a device "present" for minutes after its last advert; connecting to a lamp
    that is actually unplugged holds the adapter's connect slot for up to a minute per try,
    which delays every other Bluetooth device on the same radio.
    """
    async def t(hass, dev):
        dev.set_keep_connected(True)
        await until(lambda: connected(dev))
        RADIO.advertising = False  # unplugged
        RADIO.clients[0].drop()
        await asyncio.sleep(0.3)
        assert RADIO.attempts == 1, "no connect attempts while it isn't advertising"
        RADIO.advertising = True  # plugged back in
        dev.on_advertisement()
        await until(lambda: connected(dev))
        assert RADIO.attempts == 2

    run(t)


def test_advertisements_do_not_bypass_the_backoff():
    async def t(hass, dev):
        device_mod.KEEP_CONNECTED_RETRY_MIN = 0.3
        RADIO.fail_next = 1
        dev.set_keep_connected(True)
        await until(lambda: RADIO.attempts == 1)
        await asyncio.sleep(0.01)
        for _ in range(20):
            dev.on_advertisement()
            await asyncio.sleep(0.005)
        assert RADIO.attempts == 1
        await until(lambda: connected(dev), timeout=2)
        assert RADIO.attempts == 2

    run(t)


def test_advertisement_is_ignored_in_default_mode():
    async def t(hass, dev):
        dev.on_advertisement()
        await asyncio.sleep(0.05)
        assert RADIO.attempts == 0

    run(t)


def test_stop_disconnects_and_does_not_reconnect():
    async def t(hass, dev):
        dev.set_keep_connected(True)
        await until(lambda: connected(dev))
        client = dev._client
        await dev.stop()
        await asyncio.sleep(0.1)
        assert not client.is_connected
        assert RADIO.attempts == 1

    run(t)


def test_stop_during_a_background_connect_leaves_no_link():
    async def t(hass, dev):
        RADIO.connect_delay = 0.2
        dev.set_keep_connected(True)
        await asyncio.sleep(0.05)  # connect in flight
        await dev.stop()
        await asyncio.sleep(0.3)
        assert not any(c.is_connected for c in RADIO.clients)
        assert RADIO.attempts == 1

    run(t)


def test_turning_keep_connected_off_restores_the_idle_disconnect():
    async def t(hass, dev):
        dev.set_keep_connected(True)
        await until(lambda: connected(dev))
        dev.set_keep_connected(False)
        assert not dev.keep_connected
        await asyncio.sleep(IDLE * 4)
        assert not connected(dev)
        await asyncio.sleep(0.1)
        assert RADIO.attempts == 1

    run(t)


def test_command_during_background_connect_uses_that_connection():
    async def t(hass, dev):
        RADIO.connect_delay = 0.1
        dev.set_keep_connected(True)
        await asyncio.sleep(0.02)  # background connect in flight
        assert await send(dev)
        assert RADIO.attempts == 1
        assert len(RADIO.clients[0].writes) == 1

    run(t)


class FakeEntry:
    def __init__(self, options):
        self.entry_id = "e1"
        self.data = {"mac": MAC, "name": "Sun Lamp", "product_id": 0x27}
        self.options = options
        self.unloads = []

    def async_on_unload(self, fn):
        self.unloads.append(fn)

    def add_update_listener(self, fn):
        return lambda: None


class SetupHass(FakeHass):
    def __init__(self, loop):
        super().__init__(loop)
        hass = self

        class _Entries:
            async def async_forward_entry_setups(self, entry, platforms):
                return True

            async def async_unload_platforms(self, entry, platforms):
                return True

        self.config_entries = _Entries()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_setup_and_options_update_apply_keep_connected():
    async def main():
        hass = SetupHass(asyncio.get_running_loop())
        entry = FakeEntry({"disconnect_delay": 30, "keep_connected": True})
        assert await integration.async_setup_entry(hass, entry)
        dev = hass.data[DOMAIN][entry.entry_id]
        try:
            assert dev.keep_connected
            await until(lambda: connected(dev))
            entry.options = {"disconnect_delay": 30, "keep_connected": False}
            await integration.async_options_updated(hass, entry)
            assert not dev.keep_connected
            entry.options = {"disconnect_delay": 30}
            await integration.async_options_updated(hass, entry)
            assert not dev.keep_connected, "missing option means off (the old behaviour)"
        finally:
            await dev.stop()

    asyncio.run(main())
