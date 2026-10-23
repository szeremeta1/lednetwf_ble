"""Minimal stand-ins for Home Assistant and bleak so the device logic can be tested without them.

Only what custom_components.lednetwf_ble.__init__ and .device import is provided. Tests drive a real
asyncio loop; FakeClient behaves like a bleak client (disconnect() fires the disconnected callback).
"""
from __future__ import annotations

import asyncio
import enum
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class BleakError(Exception):
    pass


class BleakClient:
    pass


class BLEDevice:
    def __init__(self, address: str) -> None:
        self.address = address


class FakeClient:
    """Connected bleak client double. disconnect() and drop() fire the disconnected callback."""

    def __init__(self, ble_device, disconnected_callback) -> None:
        self.ble_device = ble_device
        self.is_connected = True
        self.notifying = False
        self.writes: list[bytes] = []
        self._cb = disconnected_callback

    async def start_notify(self, uuid, handler) -> None:
        self.notifying = True
        if RADIO.drop_during_setup:
            RADIO.drop_during_setup -= 1
            self.drop()

    async def stop_notify(self, uuid) -> None:
        self.notifying = False

    async def write_gatt_char(self, uuid, data, response=False) -> None:
        if not self.is_connected:
            raise BleakError("Not connected")
        self.writes.append(bytes(data))

    async def disconnect(self) -> None:
        self.drop()

    def drop(self) -> None:
        """The link went away (power cut, range, or our own disconnect)."""
        if self.is_connected:
            self.is_connected = False
            self._cb(self)


class Radio:
    """What establish_connection and HA's bluetooth helpers see: presence and scripted failures."""

    def __init__(self) -> None:
        self.present = True       # HA still has the device in its cache
        self.advertising = True   # adverts are arriving right now
        self.fail_next = 0
        self.connect_delay = 0.0
        self.drop_during_setup = 0
        self.attempts = 0
        self.clients: list[FakeClient] = []

    async def establish_connection(self, client_class, ble_device, name, disconnected_callback=None, **kw):
        self.attempts += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail_next:
            self.fail_next -= 1
            raise BleakError(f"{name}: Failed to connect after 3 attempt(s): TimeoutError")
        client = FakeClient(ble_device, disconnected_callback)
        self.clients.append(client)
        return client

    def ble_device_from_address(self, hass, address, connectable=True):
        return BLEDevice(address) if self.present else None

    def last_service_info(self, hass, address, connectable=True):
        if not self.present:
            return None
        age = 0.0 if self.advertising else 3600.0  # HA keeps the last advert around
        return types.SimpleNamespace(time=time.monotonic() - age)


RADIO = Radio()


async def _establish_connection(*a, **kw):
    return await RADIO.establish_connection(*a, **kw)


class Platform(str, enum.Enum):
    LIGHT = "light"
    NUMBER = "number"


class BluetoothChange(enum.Enum):
    ADVERTISEMENT = 1


_module("bleak", BleakClient=BleakClient)
_module("bleak.backends")
_module("bleak.backends.device", BLEDevice=BLEDevice)
_module("bleak.exc", BleakError=BleakError)
_module("bleak_retry_connector", establish_connection=_establish_connection,
        BleakClientWithServiceCache=FakeClient)
_module("homeassistant")
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.const", CONF_MAC="mac", CONF_NAME="name", Platform=Platform)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.components")
_module("homeassistant.components.bluetooth",
        async_ble_device_from_address=lambda hass, address, connectable=True:
            RADIO.ble_device_from_address(hass, address, connectable),
        async_address_present=lambda hass, address, connectable=True: RADIO.present,
        async_last_service_info=lambda hass, address, connectable=True:
            RADIO.last_service_info(hass, address, connectable),
        async_register_callback=lambda *a, **kw: (lambda: None),
        BluetoothCallbackMatcher=dict, BluetoothChange=BluetoothChange,
        BluetoothServiceInfoBleak=object)
sys.modules["homeassistant.components"].bluetooth = sys.modules["homeassistant.components.bluetooth"]


class FakeHass:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict = {}
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, target, name, eager_start=True):
        task = self.loop.create_task(target, name=name)
        self.tasks.append(task)
        return task
