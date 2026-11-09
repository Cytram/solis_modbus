"""Opt-in "Extended registers" (InverterFeature.EXTENDED).

The generated groups in hybrid_extended_sensors.py mirror the rest of the
hybrid protocol sheet. They must stay read-only, opt-in, protocol-sized, and
must never shadow a curated entity.
"""

from copy import deepcopy
from unittest.mock import MagicMock

from custom_components.solis_modbus.data.enums import DataType, InverterFeature, PollSpeed
from custom_components.solis_modbus.data.solis_config import SOLIS_INVERTERS, inverter_options_from_config
from custom_components.solis_modbus.helpers import group_in_poll_profile
from custom_components.solis_modbus.sensor_data.hybrid_extended_sensors import hybrid_extended_sensors
from custom_components.solis_modbus.sensor_data.hybrid_sensors import hybrid_sensors
from custom_components.solis_modbus.sensors.solis_base_sensor import SolisSensorGroup
from custom_components.solis_modbus.sensors.solis_sensor import SolisSensor

WRITE_KEYS = {"editable", "switch", "on_value", "off_value", "bit_position"}


def _entities(groups):
    for group in groups:
        for entity in group["entities"]:
            if entity.get("type") != "reserve":
                yield entity


def _curated_registers():
    regs = set()
    for entity in _entities(g for g in hybrid_sensors if g not in hybrid_extended_sensors):
        regs.update(int(r) for r in entity["register"])
    return regs


def test_extended_groups_are_appended_and_gated():
    assert hybrid_extended_sensors
    for group in hybrid_extended_sensors:
        assert group in hybrid_sensors
        assert group["feature_requirement"] == [InverterFeature.EXTENDED]


def test_extended_feature_is_off_by_default():
    template = next(inv for inv in SOLIS_INVERTERS if inv.model == "S6-EH3P")
    off = template.clone_with_options(inverter_options_from_config({}, template), "S2_WL_ST")
    on = template.clone_with_options(inverter_options_from_config({"has_extended": True}, template), "S2_WL_ST")
    assert InverterFeature.EXTENDED not in off.features
    assert InverterFeature.EXTENDED in on.features


def test_extended_groups_skip_reduced_poll_profiles():
    for group in hybrid_extended_sensors:
        assert not group_in_poll_profile(group, "essential")
        assert not group_in_poll_profile(group, "extreme", include_battery=True, include_smart_port=True)


def test_extended_entities_are_read_only():
    for entity in _entities(hybrid_extended_sensors):
        assert not WRITE_KEYS & entity.keys(), entity["unique"]


def test_extended_groups_fit_one_modbus_frame():
    """Spec: at most 50 registers per read frame."""
    for group in hybrid_extended_sensors:
        regs = [int(r) for e in group["entities"] for r in e["register"]]
        assert max(regs) - min(regs) + 1 <= 50, group["register_start"]


def test_extended_never_shadows_curated_entities():
    curated = _curated_registers()
    for entity in _entities(hybrid_extended_sensors):
        assert not {int(r) for r in entity["register"]} & curated, entity["unique"]


def test_extended_settings_are_read_once_and_off_by_default():
    for group in hybrid_extended_sensors:
        if group["register_start"] >= 40000:
            assert group["poll_speed"] == PollSpeed.ONCE
            for entity in _entities([group]):
                assert entity.get("registry_enabled") is False, entity["unique"]


def _sensor(definition):
    controller = MagicMock()
    controller.device_serial_number = "SN_EXT"
    controller.identification = None
    controller.host = "10.0.0.1"
    controller.inverter_config = MagicMock()
    controller.inverter_config.model = "S6-EH3P"
    controller.inverter_config.features = set()
    controller.inverter_config.wattage_chosen = 10000
    controller.poll_speed = {PollSpeed.SLOW: 30}
    group = SolisSensorGroup(
        hass=MagicMock(),
        definition={"register_start": int(definition["register"][0]), "poll_speed": PollSpeed.SLOW, "entities": [deepcopy(definition)]},
        controller=controller,
    )
    return group.sensors[0]


def test_ascii_string_registers_decode_as_text():
    entity = next(e for e in _entities(hybrid_extended_sensors) if e.get("data_type") == DataType.STRING)
    sensor = _sensor(entity)
    words = [0x5046, 0x2D31, 0x2E30, 0, 0, 0, 0, 0][: len(entity["register"])]
    assert sensor.convert_value(words) == "PF-1.0"


def test_registry_disabled_sensor_stays_available():
    entity = next(e for e in _entities(hybrid_extended_sensors) if e.get("registry_enabled") is False)
    base = _sensor(entity)
    sensor = SolisSensor(MagicMock(), base)
    assert sensor.entity_registry_enabled_default is False
    assert sensor.available is True
