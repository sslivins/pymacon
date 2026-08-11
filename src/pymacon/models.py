"""Typed protocol models for the Macon Heat Pump Controller integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .exceptions import ArcticProtocolError

PROTOCOL_VERSION = 1


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArcticProtocolError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArcticProtocolError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArcticProtocolError(f"{name} must be an integer")
    if value < minimum:
        raise ArcticProtocolError(f"{name} must be at least {minimum}")
    return int(value)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArcticProtocolError(f"{name} must be a number")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ArcticProtocolError(f"{name} must be a boolean")
    return value


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ArcticProtocolError(f"{name} must be an array of strings")
    return tuple(value)


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


@dataclass(frozen=True, slots=True)
class PairingResult:
    """Credential and identity returned by a successful physical claim."""

    protocol_version: int
    device_id: str
    fingerprint: str
    token: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PairingResult:
        return cls(
            protocol_version=_integer(
                data.get("protocol_version"),
                "protocol_version",
                minimum=1,
            ),
            device_id=_string(data.get("device_id"), "device_id"),
            fingerprint=_string(
                data.get("sha256_fingerprint"),
                "sha256_fingerprint",
            ),
            token=_string(data.get("token"), "token"),
        )


@dataclass(frozen=True, slots=True)
class ClientStatus:
    """Current reachability and push-stream state for one controller."""

    available: bool
    stream_connected: bool
    last_error: Exception | None


@dataclass(frozen=True, slots=True)
class SetpointRange:
    minimum: float
    maximum: float

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], name: str) -> SetpointRange:
        minimum = _number(data.get("min"), f"{name}.min")
        maximum = _number(data.get("max"), f"{name}.max")
        if minimum > maximum:
            raise ArcticProtocolError(f"{name}.min exceeds {name}.max")
        return cls(minimum=minimum, maximum=maximum)


@dataclass(frozen=True, slots=True)
class SetpointCapabilities:
    cooling: bool
    heating: bool
    hot_water: bool


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Acknowledgement that a controller accepted a command for execution."""

    accepted: bool
    command_id: str
    status: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CommandResult:
        return cls(
            accepted=_boolean(data.get("accepted"), "accepted"),
            command_id=_string(data.get("command_id"), "command_id"),
            status=_string(data.get("status"), "status"),
        )


@dataclass(frozen=True, slots=True)
class ControllerCapabilities:
    protocol_version: int
    device_id: str
    model: str
    firmware_version: str
    rest: bool
    websocket: bool
    read_state: bool
    control_power: bool
    control_mode: bool
    control_setpoints: bool
    supported_modes: tuple[str, ...]
    setpoint_controls: SetpointCapabilities
    cooling_range: SetpointRange
    heating_range: SetpointRange
    hot_water_range: SetpointRange

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> ControllerCapabilities:
        transports = _mapping(data.get("transports"), "transports")
        capabilities = _mapping(
            data.get("capabilities"), "capabilities"
        )
        limits = _mapping(
            data.get("setpoint_limits_c"), "setpoint_limits_c"
        )
        setpoint_controls_data = _mapping(
            capabilities.get("setpoint_controls"),
            "capabilities.setpoint_controls",
        )
        return cls(
            protocol_version=_integer(
                data.get("protocol_version"),
                "protocol_version",
                minimum=1,
            ),
            device_id=_string(data.get("device_id"), "device_id"),
            model=_string(data.get("model"), "model"),
            firmware_version=_string(
                data.get("firmware_version"), "firmware_version"
            ),
            rest=_boolean(transports.get("rest"), "transports.rest"),
            websocket=_boolean(
                transports.get("websocket"), "transports.websocket"
            ),
            read_state=_boolean(
                capabilities.get("read_state"), "capabilities.read_state"
            ),
            control_power=_boolean(
                capabilities.get("control_power"),
                "capabilities.control_power",
            ),
            control_mode=_boolean(
                capabilities.get("control_mode"),
                "capabilities.control_mode",
            ),
            control_setpoints=_boolean(
                capabilities.get("control_setpoints"),
                "capabilities.control_setpoints",
            ),
            supported_modes=_string_tuple(
                capabilities.get("supported_modes", []),
                "capabilities.supported_modes",
            ),
            setpoint_controls=SetpointCapabilities(
                cooling=_boolean(
                    setpoint_controls_data.get("cooling"),
                    "capabilities.setpoint_controls.cooling",
                ),
                heating=_boolean(
                    setpoint_controls_data.get("heating"),
                    "capabilities.setpoint_controls.heating",
                ),
                hot_water=_boolean(
                    setpoint_controls_data.get("hot_water"),
                    "capabilities.setpoint_controls.hot_water",
                ),
            ),
            cooling_range=SetpointRange.from_dict(
                _mapping(limits.get("cooling"), "setpoint_limits_c.cooling"),
                "setpoint_limits_c.cooling",
            ),
            heating_range=SetpointRange.from_dict(
                _mapping(limits.get("heating"), "setpoint_limits_c.heating"),
                "setpoint_limits_c.heating",
            ),
            hot_water_range=SetpointRange.from_dict(
                _mapping(
                    limits.get("hot_water"),
                    "setpoint_limits_c.hot_water",
                ),
                "setpoint_limits_c.hot_water",
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentState:
    compressor: bool
    fan: bool
    fan_level: int
    water_pump: bool
    backup_heater: bool
    reversing_valve_request: bool


@dataclass(frozen=True, slots=True)
class TemperatureState:
    tank: float
    outlet: float
    inlet: float
    outdoor_ambient: float
    discharge: float
    suction: float
    outdoor_coil: float
    indoor_coil: float
    ipm: float


@dataclass(frozen=True, slots=True)
class SetpointState:
    cooling: float
    heating: float
    hot_water: float


@dataclass(frozen=True, slots=True)
class ReadingState:
    compressor_frequency_hz: float
    fan_rpm: float
    power_w: float
    thermal_w: float
    cop: float | None


@dataclass(frozen=True, slots=True)
class ErrorState:
    active: bool
    description: str | None


@dataclass(frozen=True, slots=True)
class ControllerState:
    connected: bool
    unit_on: bool
    mode: str
    operation: str
    defrosting: bool
    components: ComponentState
    temperatures_c: TemperatureState
    setpoints_c: SetpointState
    readings: ReadingState
    error: ErrorState

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ControllerState:
        components = _mapping(data.get("components"), "state.components")
        temperatures = _mapping(
            data.get("temperatures_c"), "state.temperatures_c"
        )
        setpoints = _mapping(
            data.get("setpoints_c"), "state.setpoints_c"
        )
        readings = _mapping(data.get("readings"), "state.readings")
        error = _mapping(data.get("error"), "state.error")
        return cls(
            connected=_boolean(data.get("connected"), "state.connected"),
            unit_on=_boolean(data.get("unit_on"), "state.unit_on"),
            mode=_string(data.get("mode"), "state.mode"),
            operation=_string(data.get("operation"), "state.operation"),
            defrosting=_boolean(
                data.get("defrosting"), "state.defrosting"
            ),
            components=ComponentState(
                compressor=_boolean(
                    components.get("compressor"),
                    "state.components.compressor",
                ),
                fan=_boolean(components.get("fan"), "state.components.fan"),
                fan_level=_integer(
                    components.get("fan_level"),
                    "state.components.fan_level",
                ),
                water_pump=_boolean(
                    components.get("water_pump"),
                    "state.components.water_pump",
                ),
                backup_heater=_boolean(
                    components.get("backup_heater"),
                    "state.components.backup_heater",
                ),
                reversing_valve_request=_boolean(
                    components.get("reversing_valve_request"),
                    "state.components.reversing_valve_request",
                ),
            ),
            temperatures_c=TemperatureState(
                tank=_number(
                    temperatures.get("tank"), "state.temperatures_c.tank"
                ),
                outlet=_number(
                    temperatures.get("outlet"),
                    "state.temperatures_c.outlet",
                ),
                inlet=_number(
                    temperatures.get("inlet"), "state.temperatures_c.inlet"
                ),
                outdoor_ambient=_number(
                    temperatures.get("outdoor_ambient"),
                    "state.temperatures_c.outdoor_ambient",
                ),
                discharge=_number(
                    temperatures.get("discharge"),
                    "state.temperatures_c.discharge",
                ),
                suction=_number(
                    temperatures.get("suction"),
                    "state.temperatures_c.suction",
                ),
                outdoor_coil=_number(
                    temperatures.get("outdoor_coil"),
                    "state.temperatures_c.outdoor_coil",
                ),
                indoor_coil=_number(
                    temperatures.get("indoor_coil"),
                    "state.temperatures_c.indoor_coil",
                ),
                ipm=_number(
                    temperatures.get("ipm"), "state.temperatures_c.ipm"
                ),
            ),
            setpoints_c=SetpointState(
                cooling=_number(
                    setpoints.get("cooling"), "state.setpoints_c.cooling"
                ),
                heating=_number(
                    setpoints.get("heating"), "state.setpoints_c.heating"
                ),
                hot_water=_number(
                    setpoints.get("hot_water"),
                    "state.setpoints_c.hot_water",
                ),
            ),
            readings=ReadingState(
                compressor_frequency_hz=_number(
                    readings.get("compressor_frequency_hz"),
                    "state.readings.compressor_frequency_hz",
                ),
                fan_rpm=_number(
                    readings.get("fan_rpm"), "state.readings.fan_rpm"
                ),
                power_w=_number(
                    readings.get("power_w"), "state.readings.power_w"
                ),
                thermal_w=_number(
                    readings.get("thermal_w"), "state.readings.thermal_w"
                ),
                cop=_optional_number(
                    readings.get("cop"), "state.readings.cop"
                ),
            ),
            error=ErrorState(
                active=_boolean(error.get("active"), "state.error.active"),
                description=_optional_string(
                    error.get("description"), "state.error.description"
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    protocol_version: int
    device_id: str
    boot_id: str
    revision: int
    captured_at_ms: int
    state: ControllerState

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StateSnapshot:
        return cls(
            protocol_version=_integer(
                data.get("protocol_version"),
                "protocol_version",
                minimum=1,
            ),
            device_id=_string(data.get("device_id"), "device_id"),
            boot_id=_string(data.get("boot_id"), "boot_id"),
            revision=_integer(
                data.get("revision"), "revision", minimum=1
            ),
            captured_at_ms=_integer(
                data.get("captured_at_ms"), "captured_at_ms"
            ),
            state=ControllerState.from_dict(
                _mapping(data.get("state"), "state")
            ),
        )


@dataclass(frozen=True, slots=True)
class HelloMessage:
    protocol_version: int
    device_id: str
    boot_id: str
    revision: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HelloMessage:
        if data.get("type") != "hello":
            raise ArcticProtocolError("first WebSocket message must be hello")
        return cls(
            protocol_version=_integer(
                data.get("protocol_version"),
                "hello.protocol_version",
                minimum=1,
            ),
            device_id=_string(data.get("device_id"), "hello.device_id"),
            boot_id=_string(data.get("boot_id"), "hello.boot_id"),
            revision=_integer(
                data.get("revision"), "hello.revision", minimum=1
            ),
        )
