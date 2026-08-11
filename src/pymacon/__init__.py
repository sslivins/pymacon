"""Async client for the Macon Heat Pump Controller."""

from .client import (
    ArcticControllerClient,
    SnapshotCallback,
    StatusCallback,
)
from .exceptions import (
    ArcticAuthenticationError,
    ArcticCertificateError,
    ArcticCommandConflictError,
    ArcticCommandValidationError,
    ArcticConnectionError,
    ArcticControllerError,
    ArcticControlUnavailableError,
    ArcticPairingError,
    ArcticProtocolError,
)
from .models import (
    PROTOCOL_VERSION,
    ClientStatus,
    ComponentState,
    ControllerCapabilities,
    ControllerState,
    CommandResult,
    ErrorState,
    PairingResult,
    ReadingState,
    SetpointRange,
    SetpointCapabilities,
    SetpointState,
    StateSnapshot,
    TemperatureState,
)

# Keep the protocol-era names available while presenting Macon names to new
# integrations and applications.
MaconClient = ArcticControllerClient
MaconControllerClient = ArcticControllerClient
MaconAuthenticationError = ArcticAuthenticationError
MaconCertificateError = ArcticCertificateError
MaconCommandConflictError = ArcticCommandConflictError
MaconCommandValidationError = ArcticCommandValidationError
MaconConnectionError = ArcticConnectionError
MaconControllerError = ArcticControllerError
MaconControlUnavailableError = ArcticControlUnavailableError
MaconPairingError = ArcticPairingError
MaconProtocolError = ArcticProtocolError

__all__ = [
    "PROTOCOL_VERSION",
    "ArcticAuthenticationError",
    "ArcticCertificateError",
    "ArcticCommandConflictError",
    "ArcticCommandValidationError",
    "ArcticConnectionError",
    "ArcticControllerClient",
    "ArcticControllerError",
    "ArcticControlUnavailableError",
    "CommandResult",
    "ArcticPairingError",
    "ArcticProtocolError",
    "MaconAuthenticationError",
    "MaconCertificateError",
    "MaconClient",
    "MaconCommandConflictError",
    "MaconCommandValidationError",
    "MaconConnectionError",
    "MaconControllerClient",
    "MaconControllerError",
    "MaconControlUnavailableError",
    "MaconPairingError",
    "MaconProtocolError",
    "ComponentState",
    "ClientStatus",
    "ControllerCapabilities",
    "ControllerState",
    "ErrorState",
    "PairingResult",
    "ReadingState",
    "SetpointRange",
    "SetpointCapabilities",
    "SetpointState",
    "SnapshotCallback",
    "StateSnapshot",
    "StatusCallback",
    "TemperatureState",
]
