"""Exceptions raised by the Macon Heat Pump Controller client."""


class ArcticControllerError(Exception):
    """Base class for Macon Heat Pump Controller client errors."""


class ArcticConnectionError(ArcticControllerError):
    """The controller could not be reached or returned an HTTP error."""


class ArcticCertificateError(ArcticConnectionError):
    """The controller certificate does not match the configured pin."""


class ArcticAuthenticationError(ArcticControllerError):
    """The integration credential was rejected."""


class ArcticPairingError(ArcticControllerError):
    """The physical pairing claim was rejected."""


class ArcticProtocolError(ArcticControllerError):
    """The controller returned an invalid or unsupported protocol message."""


class ArcticCommandConflictError(ArcticControllerError):
    """A command ID was reused for a different operation or payload."""


class ArcticCommandValidationError(ArcticControllerError):
    """The controller rejected a command body or value."""


class ArcticControlUnavailableError(ArcticControllerError):
    """The requested control is unavailable in the active runtime."""
