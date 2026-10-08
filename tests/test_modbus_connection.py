"""Tests for the Modbus connection migration."""

import sys
from pathlib import Path
from types import ModuleType
from unittest import IsolatedAsyncioTestCase, TestCase

from modbus_connection import ModbusConnectionError

# Import the unit under test without executing integration setup, which depends
# on shared-unit APIs that are newer than the PyPI Home Assistant package.
integration_package = ModuleType("custom_components.idm_heatpump")
integration_package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components" / "idm_heatpump")
]
sys.modules.setdefault("custom_components.idm_heatpump", integration_package)

from custom_components.idm_heatpump.const import CircuitMode, HeatPumpStatus  # noqa: E402
from custom_components.idm_heatpump.idm_heatpump import IdmHeatpump  # noqa: E402
from custom_components.idm_heatpump.sensor_addresses import (  # noqa: E402
    _BitFieldSensorAddress,
    _EnumSensorAddress,
    _FloatSensorAddress,
    _UCharSensorAddress,
    _WordSensorAddress,
)


class SensorAddressCodecTests(TestCase):
    """Verify the raw value formats used by the heat pump."""

    def test_float32_uses_little_word_order(self):
        """Preserve the IDM low-word-first float encoding."""
        address = _FloatSensorAddress(address=10, name="float", unit=None)

        self.assertEqual(address.decode([0x0000, 0x4148]), (True, 12.5))
        self.assertEqual(address.encode(12.5), [0x0000, 0x4148])

    def test_signed_unsigned_and_enum_values(self):
        """Round-trip signed, unsigned, and enum register formats."""
        signed = _WordSensorAddress(address=10, name="signed", unit=None)
        unsigned = _UCharSensorAddress(address=11, name="unsigned", unit=None)
        enum = _EnumSensorAddress(
            address=12,
            name="mode",
            enum=CircuitMode,
        )

        self.assertEqual(signed.decode([0xFF85]), (True, -123))
        self.assertEqual(signed.encode(-123), [0xFF85])
        self.assertEqual(unsigned.decode([0xBEEF]), (True, 0xBEEF))
        self.assertEqual(unsigned.encode(0xBEEF), [0xBEEF])
        self.assertEqual(enum.decode([3]), (True, CircuitMode.ECO))
        self.assertEqual(enum.encode(CircuitMode.ECO), [3])

    def test_flag_values_and_sentinel(self):
        """Decode defined flag values and the unavailable sentinel."""
        address = _BitFieldSensorAddress(
            address=10,
            name="status",
            flag=HeatPumpStatus,
        )

        self.assertEqual(address.decode([1]), (True, HeatPumpStatus.HEATING))
        self.assertEqual(address.encode(HeatPumpStatus.HEATING), [1])
        self.assertEqual(address.decode([0xFFFF]), (False, HeatPumpStatus.OFF))


class FakeModbusUnit:
    """Small ModbusUnit test double."""

    def __init__(self, registers: list[int]) -> None:
        """Store the scripted input-register response."""
        self.registers = registers
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, list[int]]] = []
        self.fail_next_read = False

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        """Return the scripted registers or fail once when requested."""
        self.reads.append((address, count))
        if self.fail_next_read:
            self.fail_next_read = False
            raise ModbusConnectionError("temporary connection failure")
        return self.registers

    async def write_registers(self, address: int, values: list[int]) -> None:
        """Record a holding-register write."""
        self.writes.append((address, values))


class IdmHeatpumpModbusTests(IsolatedAsyncioTestCase):
    """Exercise the ModbusUnit boundary without connecting to a device."""

    async def asyncSetUp(self) -> None:
        """Create a heat pump backed by the fake unit."""
        self.unit = FakeModbusUnit([0x0000, 0x4148])
        self.heatpump = IdmHeatpump(
            unit=self.unit,
            circuits=[],
            zones=[],
            no_groups=True,
            max_power_usage=None,
        )
        self.address = _FloatSensorAddress(address=10, name="float", unit=None)
        self.group = IdmHeatpump._SensorGroup(
            start=self.address.address,
            count=self.address.size,
            sensors=[self.address],
        )

    async def test_group_read_retries_and_decodes(self):
        """Retry one connection error and decode the subsequent response."""
        self.unit.fail_next_read = True

        data = await self.heatpump._fetch_sensors(self.group)

        self.assertEqual(data, {"float": 12.5})
        self.assertEqual(self.unit.reads, [(10, 2), (10, 2)])

    async def test_write_uses_multiple_register_operation(self):
        """Write encoded words through ModbusUnit's FC16 operation."""
        await self.heatpump.async_write_value(self.address, 12.5)

        self.assertEqual(self.unit.writes, [(10, [0x0000, 0x4148])])
