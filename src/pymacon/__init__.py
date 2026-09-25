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
    CommandResult,
    ComponentState,
    ControllerCapabilities,
    ControllerDiagnostics,
    ControllerState,
    ErrorState,
    OtaReleaseInfo,
    OtaStatus,
    PairingResult,
    ReadingState,
    SetpointCapabilities,
    SetpointRange,
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
    "ArcticControlUnavailableError",
    "ArcticControllerClient",
    "ArcticControllerError",
    "ArcticPairingError",
    "ArcticProtocolError",
    "ClientStatus",
    "CommandResult",
    "ComponentState",
    "ControllerCapabilities",
    "ControllerDiagnostics",
    "ControllerState",
    "ErrorState",
    "MaconAuthenticationError",
    "MaconCertificateError",
    "MaconClient",
    "MaconCommandConflictError",
    "MaconCommandValidationError",
    "MaconConnectionError",
    "MaconControlUnavailableError",
    "MaconControllerClient",
    "MaconControllerError",
    "MaconPairingError",
    "MaconProtocolError",
    "OtaReleaseInfo",
    "OtaStatus",
    "PairingResult",
    "ReadingState",
    "SetpointCapabilities",
    "SetpointRange",
    "SetpointState",
    "SnapshotCallback",
    "StateSnapshot",
    "StatusCallback",
    "TemperatureState",
]
