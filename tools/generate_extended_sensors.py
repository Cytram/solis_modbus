"""Generate sensor_data/hybrid_extended_sensors.py from the Solis hybrid protocol sheet.

Usage (from the repo root):

    uv run --with openpyxl python tools/generate_extended_sensors.py \
        "RS485_MODBUS(ESINV-33000ID) Hybrid Inverter.xlsx"
    uv run ruff format custom_components/solis_modbus/sensor_data/hybrid_extended_sensors.py

Every row of the "Input Parameter 04" and "Setting Parameter 03" sheets whose
registers are not already used by a curated entity in hybrid_sensors.py becomes
a read-only sensor. Nothing generated here is writable: settings and grid-code
parameters are mirrored for inspection only.

The groups are gated behind InverterFeature.EXTENDED (the "Extended registers"
option, off by default) because they add a few dozen Modbus frames.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.solis_modbus.data.enums import Category  # noqa: E402
from custom_components.solis_modbus.sensor_data.hybrid_sensors import hybrid_sensors  # noqa: E402

OUTPUT = ROOT / "custom_components/solis_modbus/sensor_data/hybrid_extended_sensors.py"
SHEETS = {"Input Parameter 04": "input", "Setting Parameter 03": "setting"}

# Spec: at most 50 registers per read frame.
MAX_BLOCK = 50
# Gaps are read through as "reserve" registers (as hybrid_sensors.py does).
# Documented gaps (incl. "Reserved" rows) may be longer; undocumented ones are
# kept short. A register the inverter rejects is isolated and dropped at
# runtime by the block-recovery bisect in data_retrieval.py.
MAX_DOCUMENTED_GAP = 20
MAX_UNDOCUMENTED_GAP = 10

# Created disabled in the entity registry (still polled, enable to see them).
OFF_BY_DEFAULT = {
    Category.GRID_CODE_INFORMATION,
    Category.FUNCTIONAL_INFORMATION,
    Category.BATTERY_INFORMATION,
    Category.HISTORICAL_DATA,
}
# Read once at startup: static parameters. The whole Setting sheet is also read
# once - it is configuration, not telemetry; reload the entry to refresh it.
STATIC = {Category.GRID_CODE_INFORMATION, Category.GRID_CODE_SETTING, Category.BASIC_INFORMATION}

UNITS = {
    # unit: (unit expr, device class expr, measurement allowed)
    "V": ("UnitOfElectricPotential.VOLT", "SensorDeviceClass.VOLTAGE"),
    "A": ("UnitOfElectricCurrent.AMPERE", "SensorDeviceClass.CURRENT"),
    "mA": ("UnitOfElectricCurrent.MILLIAMPERE", "SensorDeviceClass.CURRENT"),
    "W": ("UnitOfPower.WATT", "SensorDeviceClass.POWER"),
    "kW": ("UnitOfPower.KILO_WATT", "SensorDeviceClass.POWER"),
    "VA": ("UnitOfApparentPower.VOLT_AMPERE", "SensorDeviceClass.APPARENT_POWER"),
    "Var": ("UnitOfReactivePower.VOLT_AMPERE_REACTIVE", "SensorDeviceClass.REACTIVE_POWER"),
    "kWh": ("UnitOfEnergy.KILO_WATT_HOUR", "SensorDeviceClass.ENERGY"),
    "Hz": ("UnitOfFrequency.HERTZ", "SensorDeviceClass.FREQUENCY"),
    "℃": ("UnitOfTemperature.CELSIUS", "SensorDeviceClass.TEMPERATURE"),
    "DegC": ("UnitOfTemperature.CELSIUS", "SensorDeviceClass.TEMPERATURE"),
    "%": ("PERCENTAGE", None),
    "ms": ("UnitOfTime.MILLISECONDS", "SensorDeviceClass.DURATION"),
    "s": ("UnitOfTime.SECONDS", "SensorDeviceClass.DURATION"),
    "S": ("UnitOfTime.SECONDS", "SensorDeviceClass.DURATION"),
    "min": ("UnitOfTime.MINUTES", "SensorDeviceClass.DURATION"),
    "hour": ("UnitOfTime.HOURS", "SensorDeviceClass.DURATION"),
    "day": ("UnitOfTime.DAYS", "SensorDeviceClass.DURATION"),
    "AH": ('"Ah"', None),
}
CATEGORY_BY_NAME = {c.value.lower(): c for c in Category}


@dataclass
class Row:
    sheet: str
    reg: int
    count: int
    name: str
    category: Category
    data_type: str
    gain: float
    unit: str | None

    @property
    def end(self) -> int:
        return self.reg + self.count - 1


def _clean(text) -> str:
    return " ".join(str(text).split())


def _gain(value) -> float:
    m = re.match(r"\s*([0-9.]+)", str(value or ""))
    if not m:
        return 1.0
    try:
        return float(m.group(1)) or 1.0
    except ValueError:
        return 1.0


def _slug(text: str) -> str:
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")
    # "data_type" in a unique id trips the dict-stringified guard (#452 test).
    return slug.replace("data_type", "datatype")[:60]


def curated_registers() -> tuple[set[int], set[str]]:
    regs: set[int] = set()
    names: set[str] = set()
    for group in hybrid_sensors:
        if group.get("feature_requirement") and "extended" in str(group.get("feature_requirement")):
            continue
        for entity in group.get("entities", []):
            if entity.get("type") == "reserve":
                continue
            regs.update(int(r) for r in entity.get("register", []))
            if entity.get("name"):
                names.add(entity["name"].lower())
    return regs, names


def load_rows(path: str) -> tuple[list[Row], dict[str, set[int]]]:
    covered, _ = curated_registers()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows: list[Row] = []
    documented: dict[str, set[int]] = {}
    for sheet, kind in SHEETS.items():
        doc = documented.setdefault(kind, set())
        for raw in wb[sheet].iter_rows(min_row=2, values_only=True):
            try:
                reg, count = int(raw[0]), int(raw[1] or 1)
            except (TypeError, ValueError):
                continue
            if raw[2] is None:
                continue
            doc.update(range(reg, reg + count))
            name = re.sub(r"^\d{5}\s+", "", _clean(raw[2]))
            if "reserved" in name.lower():
                continue
            if set(range(reg, reg + count)) & covered:
                continue
            category = CATEGORY_BY_NAME.get(_clean(raw[3]).lower(), Category.OTHER_INFORMATION)
            unit = _clean(raw[7]) if raw[7] not in (None, "") else None
            rows.append(Row(kind, reg, count, name, category, _clean(raw[4]).upper(), _gain(raw[6]), unit))
    rows.sort(key=lambda r: r.reg)
    # Drop rows overlapping an earlier row (sheet typos) so groups stay contiguous.
    clean: list[Row] = []
    for row in rows:
        if clean and row.reg <= clean[-1].end:
            continue
        clean.append(row)
    return clean, documented


def build_blocks(rows: list[Row], documented: dict[str, set[int]]) -> list[list[Row]]:
    blocks: list[list[Row]] = []
    for row in rows:
        if blocks:
            last = blocks[-1]
            start, end = last[0].reg, last[-1].end
            gap = range(end + 1, row.reg)
            fits = row.end - start + 1 <= MAX_BLOCK and last[0].sheet == row.sheet
            gap_ok = (len(gap) <= MAX_DOCUMENTED_GAP and all(g in documented[row.sheet] for g in gap)) or len(gap) <= MAX_UNDOCUMENTED_GAP
            if fits and gap_ok:
                last.append(row)
                continue
        blocks.append([row])
    return blocks


def entity_source(row: Row, taken_names: set[str]) -> str:
    name = row.name[:80]
    if name.lower() in taken_names:
        name = f"{name} ({row.reg})"
    taken_names.add(name.lower())
    regs = ", ".join(f'"{r}"' for r in range(row.reg, row.end + 1))
    lines = [
        f'"name": {name!r}',
        f'"unique": "solis_modbus_inverter_ext_{row.reg}_{_slug(row.name)}"',
        f'"category": Category.{row.category.name}',
        f'"register": [{regs}]',
    ]
    if row.data_type == "ASCII" or row.data_type == "STR":
        lines.append('"data_type": DataType.STRING')
        lines.append('"multiplier": 0')
    else:
        if row.data_type in ("S16", "U32", "S32"):
            lines.append(f'"data_type": DataType.{row.data_type}')
        gain = row.gain
        lines.append(f'"multiplier": {int(gain) if gain >= 1 and gain == int(gain) else gain!r}')
        unit = UNITS.get(row.unit or "")
        is_input = row.sheet == "input"
        if unit:
            lines.append(f'"unit_of_measurement": {unit[0]}')
            if unit[1]:
                lines.append(f'"device_class": {unit[1]}')
        elif row.unit and row.unit.lower() != "none":
            lines.append(f'"unit_of_measurement": {row.unit!r}')
        if is_input and row.category != Category.HISTORICAL_DATA and unit:
            if row.unit == "kWh":
                lines.append('"state_class": SensorStateClass.TOTAL_INCREASING')
            else:
                lines.append('"state_class": SensorStateClass.MEASUREMENT')
    if row.sheet == "setting" or row.category in OFF_BY_DEFAULT:
        lines.append('"registry_enabled": False')
    return "{" + ", ".join(lines) + "}"


def render(blocks: list[list[Row]]) -> str:
    _, taken = curated_registers()
    out = [
        '"""Read-only sensors for the rest of the hybrid protocol sheet.',
        "",
        "GENERATED by tools/generate_extended_sensors.py - do not edit by hand.",
        "Source: RS485_MODBUS(ESINV-33000ID) Hybrid Inverter protocol sheet.",
        "",
        "Only loaded with the 'Extended registers' option (InverterFeature.EXTENDED).",
        '"""',
        "",
        "from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass",
        "from homeassistant.const import (",
        "    PERCENTAGE,",
        "    UnitOfApparentPower,",
        "    UnitOfElectricCurrent,",
        "    UnitOfElectricPotential,",
        "    UnitOfEnergy,",
        "    UnitOfFrequency,",
        "    UnitOfPower,",
        "    UnitOfReactivePower,",
        "    UnitOfTemperature,",
        "    UnitOfTime,",
        ")",
        "",
        "from custom_components.solis_modbus.data.enums import Category, DataType, InverterFeature, PollSpeed",
        "",
        "hybrid_extended_sensors = [",
    ]
    for block in blocks:
        static = all(r.sheet == "setting" or r.category in STATIC for r in block)
        entities = []
        prev_end = None
        for row in block:
            if prev_end is not None:
                for gap in range(prev_end + 1, row.reg):
                    entities.append(f'{{"type": "reserve", "register": ["{gap}"]}}')
            entities.append(entity_source(row, taken))
            prev_end = row.end
        speed = "PollSpeed.ONCE" if static else "PollSpeed.SLOW"
        out.append(
            "{"
            f'"register_start": {block[0].reg}, '
            f'"poll_speed": {speed}, '
            '"feature_requirement": [InverterFeature.EXTENDED], '
            '"entities": [' + ", ".join(entities) + "]},"
        )
    out.append("]")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx")
    args = parser.parse_args()
    rows, documented = load_rows(args.xlsx)
    blocks = build_blocks(rows, documented)
    OUTPUT.write_text(render(blocks), encoding="utf-8")
    regs = sum(b[-1].end - b[0].reg + 1 for b in blocks)
    print(f"{len(rows)} entities in {len(blocks)} groups ({regs} registers polled) -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
