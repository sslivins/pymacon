"""Secure async REST/WSS client for one Macon Heat Pump Controller."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import random
import ssl
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

import aiohttp
from yarl import URL

from .exceptions import (
    ArcticAuthenticationError,
    ArcticCertificateError,
    ArcticCommandConflictError,
    ArcticCommandValidationError,
    ArcticConnectionError,
    ArcticControlUnavailableError,
    ArcticPairingError,
    ArcticProtocolError,
)
from .models import (
    PROTOCOL_VERSION,
    ClientStatus,
    CommandResult,
    ControllerCapabilities,
    HelloMessage,
    OtaReleaseInfo,
    OtaStatus,
    PairingResult,
    StateSnapshot,
)

SnapshotCallback = Callable[[StateSnapshot], Awaitable[None] | None]
StatusCallback = Callable[[ClientStatus], Awaitable[None] | None]
CapabilitiesCallback = Callable[
    [ControllerCapabilities], Awaitable[None] | None
]
_LOGGER = logging.getLogger(__name__)


def _normalize_fingerprint(value: str | bytes) -> tuple[str, bytes]:
    if isinstance(value, bytes):
        raw = value
        text = value.hex()
    else:
        text = value.replace(":", "").lower()
        try:
            raw = bytes.fromhex(text)
        except ValueError as error:
            raise ValueError("fingerprint must be hexadecimal") from error
    if len(raw) != 32:
        raise ValueError("fingerprint must contain 32 SHA-256 bytes")
    return text, raw


def _normalize_token(value: str) -> str:
    if len(value) != 64:
        raise ValueError("token must contain 64 hexadecimal characters")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(
            "token must contain 64 hexadecimal characters"
        ) from error
    return value.lower()


def _tofu_ssl_context() -> ssl.SSLContext:
    """Build a context that completes the handshake without verifying.

    Pairing uses trust-on-first-use: we cannot verify the controller's
    self-signed certificate against a CA, so we accept the handshake to
    capture the certificate it presents, then pin the pairing request to
    that certificate and cross-check it against the fingerprint the
    controller signs into its pairing response body.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _fetch_peer_fingerprint(
    host: str, port: int, timeout: float
) -> str:
    """Capture the SHA-256 of the certificate the controller presents."""
    connect = asyncio.open_connection(
        host, port, ssl=_tofu_ssl_context(), server_hostname=host
    )
    _reader, writer = await asyncio.wait_for(connect, timeout)
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = (
            ssl_object.getpeercert(binary_form=True)
            if ssl_object is not None
            else None
        )
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
    if not der:
        raise ArcticCertificateError(
            "could not read the controller certificate during pairing"
        )
    return hashlib.sha256(der).hexdigest()


class ArcticControllerClient:
    """Manage one controller's secure transport and ordered state."""

    def __init__(
        self,
        host: str,
        token: str,
        fingerprint: str | bytes,
        *,
        device_id: str | None = None,
        port: int = 8443,
        session: aiohttp.ClientSession | None = None,
        request_timeout: float = 10.0,
        reconciliation_interval: float = 60.0,
        fallback_poll_interval: float = 10.0,
        reconnect_min_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        reconnect_jitter: float = 0.25,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if reconciliation_interval <= 0:
            raise ValueError("reconciliation_interval must be positive")
        if fallback_poll_interval <= 0:
            raise ValueError("fallback_poll_interval must be positive")
        if reconnect_min_delay <= 0:
            raise ValueError("reconnect_min_delay must be positive")
        if reconnect_max_delay < reconnect_min_delay:
            raise ValueError(
                "reconnect_max_delay must be at least reconnect_min_delay"
            )
        self._host = host
        self._port = port
        if not 0 <= reconnect_jitter < 1:
            raise ValueError("reconnect_jitter must be between 0 and 1")
        self._token = _normalize_token(token)
        self._fingerprint_text, fingerprint_bytes = (
            _normalize_fingerprint(fingerprint)
        )
        self._ssl = aiohttp.Fingerprint(fingerprint_bytes)
        self._device_id = device_id
        self._session = session
        self._owns_session = session is None
        self._request_timeout = aiohttp.ClientTimeout(
            total=request_timeout
        )
        self._reconciliation_interval = reconciliation_interval
        self._fallback_poll_interval = fallback_poll_interval
        self._reconnect_min_delay = reconnect_min_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._reconnect_jitter = reconnect_jitter

        base = URL.build(scheme="https", host=host, port=port)
        self._base_url = base
        self._events_url = base.with_scheme("wss").join(
            URL("/api/v1/events")
        )
        self._callbacks: set[SnapshotCallback] = set()
        self._status_callbacks: set[StatusCallback] = set()
        self._capabilities_callbacks: set[CapabilitiesCallback] = set()
        self._snapshot: StateSnapshot | None = None
        self._capabilities: ControllerCapabilities | None = None
        self._status = ClientStatus(False, False, None)
        self._state_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._poll_wakeup = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._running = False

    @property
    def device_id(self) -> str | None:
        return self._device_id

    @property
    def fingerprint(self) -> str:
        return self._fingerprint_text

    @property
    def snapshot(self) -> StateSnapshot | None:
        return self._snapshot

    @property
    def capabilities(self) -> ControllerCapabilities | None:
        return self._capabilities

    @property
    def stream_connected(self) -> bool:
        return self._status.stream_connected

    @property
    def available(self) -> bool:
        return self._status.available

    @property
    def last_error(self) -> Exception | None:
        return self._status.last_error

    @property
    def status(self) -> ClientStatus:
        return self._status

    @property
    def running(self) -> bool:
        return self._running

    @classmethod
    async def pair(
        cls,
        host: str,
        code: str,
        *,
        port: int = 8443,
        session: aiohttp.ClientSession | None = None,
        request_timeout: float = 10.0,
    ) -> PairingResult:
        """Claim the physically displayed one-time pairing code.

        Uses trust-on-first-use: the controller's self-signed certificate is
        captured from the pairing connection and cross-checked against the
        fingerprint the controller signs into its response body. The verified
        fingerprint is returned so the caller can pin it for all future
        connections. No fingerprint needs to be supplied or typed by a user.
        """
        if len(code) != 6 or not code.isdigit():
            raise ValueError("pairing code must contain exactly six digits")
        owns_session = session is None
        active_session = session or aiohttp.ClientSession()
        url = URL.build(scheme="https", host=host, port=port).join(
            URL("/api/v1/pair")
        )
        try:
            observed_fingerprint = await _fetch_peer_fingerprint(
                host, port, request_timeout
            )
            observed_bytes = bytes.fromhex(observed_fingerprint)
            async with active_session.post(
                url,
                json={"code": code},
                ssl=aiohttp.Fingerprint(observed_bytes),
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as response:
                if response.status != 200:
                    message = await cls._error_message(response)
                    raise ArcticPairingError(
                        f"pairing failed with HTTP {response.status}: "
                        f"{message}"
                    )
                result = PairingResult.from_dict(await response.json())
        except (ArcticPairingError, ArcticCertificateError):
            raise
        except aiohttp.ServerFingerprintMismatch as error:
            raise ArcticCertificateError(
                "controller certificate changed during pairing"
            ) from error
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ArcticConnectionError(
                f"could not pair with controller at {host}"
            ) from error
        finally:
            if owns_session:
                await active_session.close()

        if result.protocol_version != PROTOCOL_VERSION:
            raise ArcticProtocolError(
                f"unsupported protocol version {result.protocol_version}"
            )
        try:
            claimed_text, _ = _normalize_fingerprint(result.fingerprint)
        except ValueError as error:
            raise ArcticProtocolError(
                "pairing response fingerprint is invalid"
            ) from error
        if observed_fingerprint != claimed_text:
            raise ArcticCertificateError(
                "controller certificate does not match the pairing response"
            )
        try:
            _normalize_token(result.token)
        except ValueError as error:
            raise ArcticProtocolError(
                "pairing response token is invalid"
            ) from error
        return result

    def subscribe(self, callback: SnapshotCallback) -> Callable[[], None]:
        """Receive each accepted snapshot; return an unsubscribe callback."""
        self._callbacks.add(callback)

        def unsubscribe() -> None:
            self._callbacks.discard(callback)

        return unsubscribe

    def subscribe_status(
        self, callback: StatusCallback
    ) -> Callable[[], None]:
        """Receive availability and stream-state changes."""
        self._status_callbacks.add(callback)

        def unsubscribe() -> None:
            self._status_callbacks.discard(callback)

        return unsubscribe

    def subscribe_capabilities(
        self, callback: CapabilitiesCallback
    ) -> Callable[[], None]:
        """Receive changes to the controller's dynamic control surface."""
        self._capabilities_callbacks.add(callback)

        def unsubscribe() -> None:
            self._capabilities_callbacks.discard(callback)

        return unsubscribe

    async def async_setup(self) -> StateSnapshot:
        """Load capabilities and the initial coherent REST snapshot."""
        created_session = self._session is None
        try:
            await self._ensure_session()
            capabilities = await self.fetch_capabilities()
            snapshot = await self.fetch_state()
            if capabilities.device_id != snapshot.device_id:
                raise ArcticProtocolError(
                    "capabilities and state device IDs do not match"
                )
            return snapshot
        except Exception as error:
            await self._set_status(
                available=False,
                stream_connected=False,
                last_error=error,
            )
            if (
                created_session
                and self._owns_session
                and self._session is not None
            ):
                await self._session.close()
                self._session = None
            raise

    async def start(self) -> StateSnapshot:
        """Initialize state and start WSS/reconciliation background tasks."""
        async with self._lifecycle_lock:
            if self._running:
                if self._snapshot is None:
                    raise ArcticProtocolError(
                        "running client has no initial snapshot"
                    )
                return self._snapshot
            initial = await self.async_setup()
            self._stop_event.clear()
            self._poll_wakeup.clear()
            self._running = True
            self._tasks = {
                asyncio.create_task(
                    self._stream_supervisor(),
                    name=f"macon-wss-{self._device_id}",
                ),
                asyncio.create_task(
                    self._poll_loop(),
                    name=f"macon-poll-{self._device_id}",
                ),
            }
            return initial

    async def stop(self) -> None:
        """Cancel background work and close an internally owned session."""
        async with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
            self._poll_wakeup.set()
            tasks = tuple(self._tasks)
            self._tasks.clear()
            current = asyncio.current_task()
            siblings = tuple(task for task in tasks if task is not current)
            for task in siblings:
                task.cancel()
            if siblings:
                await asyncio.gather(*siblings, return_exceptions=True)
            await self._set_status(
                available=False,
                stream_connected=False,
                last_error=None,
            )
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None

    async def fetch_capabilities(self) -> ControllerCapabilities:
        data = await self._get_json("/api/v1/capabilities")
        capabilities = ControllerCapabilities.from_dict(data)
        self._validate_identity(
            capabilities.protocol_version, capabilities.device_id
        )
        changed = capabilities != self._capabilities
        self._capabilities = capabilities
        if changed:
            for callback in tuple(self._capabilities_callbacks):
                try:
                    result = callback(capabilities)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    _LOGGER.exception(
                        "Macon capabilities callback failed"
                    )
        await self._set_status(available=True, last_error=None)
        return capabilities

    async def fetch_state(self) -> StateSnapshot:
        async with self._state_lock:
            current = self._snapshot
            request_marker = (
                None
                if current is None
                else (current.boot_id, current.revision)
            )
        data = await self._get_json("/api/v1/state")
        snapshot = StateSnapshot.from_dict(data)
        accepted = await self._accept_snapshot(
            snapshot, request_marker=request_marker
        )
        await self._set_status(available=True, last_error=None)
        if not accepted and self._snapshot is not None:
            return self._snapshot
        return snapshot

    async def async_set_power(
        self, on: bool, *, command_id: str | None = None
    ) -> CommandResult:
        """Ask the controller to change power; state remains push-confirmed."""
        if not isinstance(on, bool):
            raise TypeError("on must be a boolean")
        return await self._put_command(
            "/api/v1/control/power",
            {"command_id": self._command_id(command_id), "on": on},
        )

    async def async_set_mode(
        self, mode: str, *, command_id: str | None = None
    ) -> CommandResult:
        """Ask the controller to select one exact Macon working mode."""
        if not isinstance(mode, str) or not mode:
            raise ValueError("mode must be a non-empty string")
        return await self._put_command(
            "/api/v1/control/mode",
            {"command_id": self._command_id(command_id), "mode": mode},
        )

    async def async_set_setpoint(
        self,
        kind: str,
        value: int,
        *,
        command_id: str | None = None,
    ) -> CommandResult:
        """Ask the controller to change one named setpoint."""
        if kind not in {"cooling", "heating", "hot_water"}:
            raise ValueError("unsupported setpoint kind")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("setpoint value must be an integer")
        return await self._put_command(
            "/api/v1/control/setpoint",
            {
                "command_id": self._command_id(command_id),
                "kind": kind,
                "value": value,
            },
        )

    async def async_set_cooling_setpoint(
        self, value: int, *, command_id: str | None = None
    ) -> CommandResult:
        return await self.async_set_setpoint(
            "cooling", value, command_id=command_id
        )

    async def async_set_heating_setpoint(
        self, value: int, *, command_id: str | None = None
    ) -> CommandResult:
        return await self.async_set_setpoint(
            "heating", value, command_id=command_id
        )

    async def async_set_hot_water_setpoint(
        self, value: int, *, command_id: str | None = None
    ) -> CommandResult:
        return await self.async_set_setpoint(
            "hot_water", value, command_id=command_id
        )

    async def async_check_updates(self) -> OtaReleaseInfo:
        """Ask the controller to check GitHub for a newer firmware release.

        Triggers a live check against the controller's configured release
        source and returns whether an update is available. The controller
        caches the result so a subsequent :meth:`async_start_update` can act on
        it.
        """
        data = await self._get_json("/api/ota/releases")
        return OtaReleaseInfo.from_dict(data)

    async def async_ota_status(self) -> OtaStatus:
        """Return the controller's current firmware-update progress.

        Safe to poll; reports the update state (idle/downloading/verifying/
        ready_to_reboot/failed), a 0-100 percentage, and version details.
        """
        data = await self._get_json("/api/ota/status")
        return OtaStatus.from_dict(data)

    async def async_start_update(self) -> None:
        """Ask the controller to download and install the latest release.

        Requires a prior :meth:`async_check_updates` that reported an
        available, download-ready update (the controller acts on its cached
        release info). Poll :meth:`async_ota_status` afterwards for progress.

        Raises:
            ArcticCommandValidationError: no update is available to install.
            ArcticCommandConflictError: an update is already in progress.
        """
        await self._post_json("/api/ota/github")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if not self._owns_session:
                raise ArcticConnectionError(
                    "the externally supplied HTTP session is closed"
                )
            self._session = aiohttp.ClientSession()
        return self._session

    @staticmethod
    def _command_id(command_id: str | None) -> str:
        value = str(uuid.uuid4()) if command_id is None else command_id
        if not isinstance(value, str) or not 0 < len(value) <= 64:
            raise ValueError("command_id must be 1 to 64 characters")
        if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
            raise ValueError("command_id must contain printable ASCII")
        return value

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get_json(self, path: str) -> Mapping[str, Any]:
        session = await self._ensure_session()
        try:
            async with session.get(
                self._base_url.join(URL(path)),
                headers=self._headers(),
                ssl=self._ssl,
                timeout=self._request_timeout,
            ) as response:
                if response.status == 401:
                    raise ArcticAuthenticationError(
                        "integration credential was rejected"
                    )
                if response.status != 200:
                    message = await self._error_message(response)
                    raise ArcticConnectionError(
                        f"GET {path} failed with HTTP {response.status}: "
                        f"{message}"
                    )
                data = await response.json()
        except (ArcticAuthenticationError, ArcticConnectionError):
            raise
        except aiohttp.ServerFingerprintMismatch as error:
            raise ArcticCertificateError(
                "controller certificate fingerprint changed"
            ) from error
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ArcticConnectionError(
                f"GET {path} could not reach the controller"
            ) from error
        if not isinstance(data, Mapping):
            raise ArcticProtocolError(f"GET {path} returned non-object JSON")
        return data

    async def _put_command(
        self, path: str, payload: Mapping[str, Any]
    ) -> CommandResult:
        session = await self._ensure_session()
        try:
            async with session.put(
                self._base_url.join(URL(path)),
                headers=self._headers(),
                json=dict(payload),
                ssl=self._ssl,
                timeout=self._request_timeout,
            ) as response:
                if response.status == 401:
                    raise ArcticAuthenticationError(
                        "integration credential was rejected"
                    )
                if response.status == 409:
                    raise ArcticCommandConflictError(
                        await self._error_message(response)
                    )
                if response.status == 422:
                    raise ArcticCommandValidationError(
                        await self._error_message(response)
                    )
                if response.status == 503:
                    raise ArcticControlUnavailableError(
                        await self._error_message(response)
                    )
                if response.status != 202:
                    message = await self._error_message(response)
                    raise ArcticConnectionError(
                        f"PUT {path} failed with HTTP {response.status}: "
                        f"{message}"
                    )
                data = await response.json()
        except (
            ArcticAuthenticationError,
            ArcticCommandConflictError,
            ArcticCommandValidationError,
            ArcticControlUnavailableError,
            ArcticConnectionError,
        ):
            raise
        except aiohttp.ServerFingerprintMismatch as error:
            raise ArcticCertificateError(
                "controller certificate fingerprint changed"
            ) from error
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ArcticConnectionError(
                f"PUT {path} could not reach the controller"
            ) from error
        if not isinstance(data, Mapping):
            raise ArcticProtocolError(f"PUT {path} returned non-object JSON")
        result = CommandResult.from_dict(data)
        if not result.accepted or result.command_id != payload["command_id"]:
            raise ArcticProtocolError(
                f"PUT {path} returned an invalid command acknowledgement"
            )
        return result

    async def _post_json(
        self, path: str, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        session = await self._ensure_session()
        try:
            async with session.post(
                self._base_url.join(URL(path)),
                headers=self._headers(),
                json=dict(payload) if payload is not None else None,
                ssl=self._ssl,
                timeout=self._request_timeout,
            ) as response:
                if response.status == 401:
                    raise ArcticAuthenticationError(
                        "integration credential was rejected"
                    )
                if response.status == 409:
                    raise ArcticCommandConflictError(
                        await self._error_message(response)
                    )
                if response.status in (400, 422):
                    raise ArcticCommandValidationError(
                        await self._error_message(response)
                    )
                if response.status not in (200, 202):
                    message = await self._error_message(response)
                    raise ArcticConnectionError(
                        f"POST {path} failed with HTTP {response.status}: "
                        f"{message}"
                    )
                data = await response.json()
        except (
            ArcticAuthenticationError,
            ArcticCommandConflictError,
            ArcticCommandValidationError,
            ArcticConnectionError,
        ):
            raise
        except aiohttp.ServerFingerprintMismatch as error:
            raise ArcticCertificateError(
                "controller certificate fingerprint changed"
            ) from error
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ArcticConnectionError(
                f"POST {path} could not reach the controller"
            ) from error
        if not isinstance(data, Mapping):
            raise ArcticProtocolError(f"POST {path} returned non-object JSON")
        return data

    async def _stream_supervisor(self) -> None:
        delay = self._reconnect_min_delay
        while self._running:
            stream_error: Exception | None = None
            try:
                await self._run_stream()
                delay = self._reconnect_min_delay
            except asyncio.CancelledError:
                raise
            except ArcticAuthenticationError as error:
                stream_error = error
                await self._shutdown_background(error)
                return
            except (TimeoutError, ArcticConnectionError, ArcticProtocolError, aiohttp.ClientError) as error:
                stream_error = error
            finally:
                if (
                    self._status.stream_connected
                    or stream_error is not None
                ):
                    await self._set_status(
                        stream_connected=False,
                        last_error=stream_error,
                    )
                    self._poll_wakeup.set()

            if not self._running:
                return
            jitter = random.uniform(
                1.0 - self._reconnect_jitter,
                1.0 + self._reconnect_jitter,
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=delay * jitter
                )
            except TimeoutError:
                pass
            delay = min(delay * 2, self._reconnect_max_delay)

    async def _run_stream(self) -> None:
        session = await self._ensure_session()
        try:
            websocket = await session.ws_connect(
                self._events_url,
                headers=self._headers(),
                ssl=self._ssl,
                timeout=aiohttp.ClientWSTimeout(ws_receive=45),
                autoclose=True,
                autoping=True,
            )
        except aiohttp.WSServerHandshakeError as error:
            if error.status == 401:
                raise ArcticAuthenticationError(
                    "integration credential was rejected"
                ) from error
            raise ArcticConnectionError(
                f"WebSocket upgrade failed with HTTP {error.status}"
            ) from error
        except aiohttp.ServerFingerprintMismatch as error:
            raise ArcticCertificateError(
                "controller certificate fingerprint changed"
            ) from error

        async with websocket:
            hello_data = await self._receive_json(websocket, timeout=10)
            hello = HelloMessage.from_dict(hello_data)
            self._validate_identity(
                hello.protocol_version, hello.device_id
            )

            first_data = await self._receive_json(websocket, timeout=10)
            first = self._snapshot_from_message(first_data)
            if (
                hello.boot_id != first.boot_id
                or hello.revision != first.revision
            ):
                raise ArcticProtocolError(
                    "hello and initial snapshot are not coherent"
                )
            await self._accept_snapshot(first)
            await self._set_status(
                available=True,
                stream_connected=True,
                last_error=None,
            )
            self._poll_wakeup.set()

            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = message.json()
                    except (TypeError, ValueError) as error:
                        raise ArcticProtocolError(
                            "WebSocket message contains invalid JSON"
                        ) from error
                    await self._accept_snapshot(
                        self._snapshot_from_message(data)
                    )
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                }:
                    return
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise ArcticConnectionError(
                        "WebSocket receive failed"
                    ) from websocket.exception()
                else:
                    raise ArcticProtocolError(
                        f"unexpected WebSocket message type {message.type}"
                    )

    async def _poll_loop(self) -> None:
        while self._running:
            interval = (
                self._reconciliation_interval
                if self._status.stream_connected
                else self._fallback_poll_interval
            )
            try:
                await asyncio.wait_for(
                    self._poll_wakeup.wait(), timeout=interval
                )
                self._poll_wakeup.clear()
            except TimeoutError:
                pass

            if not self._running:
                return
            try:
                await self.fetch_capabilities()
                await self.fetch_state()
            except ArcticAuthenticationError as error:
                await self._shutdown_background(error)
                return
            except (
                ArcticConnectionError,
                ArcticProtocolError,
            ) as error:
                if not self._status.stream_connected:
                    await self._set_status(
                        available=False, last_error=error
                    )
                continue

    async def _accept_snapshot(
        self,
        snapshot: StateSnapshot,
        *,
        request_marker: tuple[str, int] | None = None,
    ) -> bool:
        self._validate_identity(
            snapshot.protocol_version, snapshot.device_id
        )
        callbacks: tuple[SnapshotCallback, ...] = ()
        reconcile = False
        async with self._state_lock:
            current = self._snapshot
            current_marker = (
                None
                if current is None
                else (current.boot_id, current.revision)
            )
            if (
                request_marker is not None
                and current_marker != request_marker
                and current is not None
                and snapshot.boot_id != current.boot_id
            ):
                return False
            if current is not None and snapshot.boot_id == current.boot_id:
                if snapshot.revision <= current.revision:
                    return False
                reconcile = snapshot.revision > current.revision + 1
            elif current is not None:
                reconcile = True
            self._snapshot = snapshot
            callbacks = tuple(self._callbacks)

        for callback in callbacks:
            try:
                result = callback(snapshot)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _LOGGER.exception("Macon snapshot callback failed")
        if reconcile:
            self._poll_wakeup.set()
        return True

    async def _shutdown_background(self, error: Exception) -> None:
        self._running = False
        self._stop_event.set()
        self._poll_wakeup.set()
        current = asyncio.current_task()
        siblings = tuple(
            task
            for task in self._tasks
            if task is not current and not task.done()
        )
        for task in siblings:
            task.cancel()
        if siblings:
            await asyncio.gather(*siblings, return_exceptions=True)
        await self._set_status(
            available=False,
            stream_connected=False,
            last_error=error,
        )

    async def _set_status(
        self,
        *,
        available: bool | None = None,
        stream_connected: bool | None = None,
        last_error: Exception | None = None,
    ) -> None:
        callbacks: tuple[StatusCallback, ...] = ()
        async with self._status_lock:
            current = self._status
            updated = ClientStatus(
                available=(
                    current.available if available is None else available
                ),
                stream_connected=(
                    current.stream_connected
                    if stream_connected is None
                    else stream_connected
                ),
                last_error=last_error,
            )
            if updated == current:
                return
            self._status = updated
            callbacks = tuple(self._status_callbacks)
        for callback in callbacks:
            try:
                result = callback(updated)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _LOGGER.exception("Macon status callback failed")

    def _validate_identity(
        self, protocol_version: int, device_id: str
    ) -> None:
        if protocol_version != PROTOCOL_VERSION:
            raise ArcticProtocolError(
                f"unsupported protocol version {protocol_version}"
            )
        if self._device_id is None:
            self._device_id = device_id
        elif device_id != self._device_id:
            raise ArcticProtocolError(
                f"expected device {self._device_id}, received {device_id}"
            )

    @staticmethod
    def _snapshot_from_message(data: Any) -> StateSnapshot:
        if not isinstance(data, Mapping) or data.get("type") != "snapshot":
            raise ArcticProtocolError(
                "WebSocket message must contain a snapshot"
            )
        payload = data.get("snapshot")
        if not isinstance(payload, Mapping):
            raise ArcticProtocolError(
                "WebSocket snapshot payload must be an object"
            )
        return StateSnapshot.from_dict(payload)

    @staticmethod
    async def _receive_json(
        websocket: aiohttp.ClientWebSocketResponse,
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        try:
            data = await websocket.receive_json(timeout=timeout)
        except (TypeError, ValueError) as error:
            raise ArcticProtocolError(
                "WebSocket message contains invalid JSON"
            ) from error
        if not isinstance(data, Mapping):
            raise ArcticProtocolError(
                "WebSocket message must contain a JSON object"
            )
        return data

    @staticmethod
    async def _error_message(response: aiohttp.ClientResponse) -> str:
        with suppress(aiohttp.ClientError, ValueError):
            data = await response.json()
            if isinstance(data, Mapping):
                error = data.get("error")
                if isinstance(error, str):
                    return error
        text = await response.text()
        return text[:200] or "unknown error"
