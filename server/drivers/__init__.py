"""Driver registry: brand name -> Driver instance."""
from __future__ import annotations

from server.config import Config
from server.db import Database
from server.drivers.base import Driver
from server.drivers.broadlink_driver import BroadlinkDriver
from server.drivers.dummy import DummyDriver
from server.drivers.tuya import TuyaDriver
from server.drivers.zigbee import ZigbeeDriver


def load_drivers(cfg: Config, db: Database) -> dict[str, Driver]:
    drivers: dict[str, Driver] = {
        DummyDriver.name: DummyDriver(),
        BroadlinkDriver.name: BroadlinkDriver(cfg),
        TuyaDriver.name: TuyaDriver(db),
        ZigbeeDriver.name: ZigbeeDriver(cfg, db),
    }
    # generic = discovered but unsupported: recordable, not controllable
    generic = DummyDriver()
    generic.name = "generic"
    drivers["generic"] = generic
    return drivers
