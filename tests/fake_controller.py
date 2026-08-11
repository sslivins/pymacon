"""Deterministic pinned-TLS Macon controller used by client tests."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class FakeController:
    def __init__(
        self,
        temp_path: Path,
        *,
        # The arctic-* identifier is the firmware's legacy wire identity.
        device_id: str = "arctic-001122334455",
        token: str = "a" * 64,
        pairing_code: str = "123456",
    ) -> None:
        self.device_id = device_id
        self.token = token
        self.pairing_code = pairing_code
        self.boot_id = "1" * 32
        self.revision = 1
        self.tank_temperature = 40.0
        self.state_requests = 0
        self.websocket_connections = 0
        self.state_request_started = asyncio.Event()
        self.release_state_request = asyncio.Event()
        self.release_state_request.set()
        self.hold_state_response = False
        self.commands: dict[str, tuple[str, tuple[tuple[str, Any], ...]]] = {}
        self.command_requests: list[dict[str, Any]] = []
        self._temp_path = temp_path
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._websockets: set[web.WebSocketResponse] = set()
        self.host = "127.0.0.1"
        self.port = 0
        self.fingerprint = ""

    async def start(self) -> FakeController:
        cert_path, key_path, self.fingerprint = self._create_identity()
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_path, key_path)

        app = web.Application()
        app.router.add_post("/api/v1/pair", self._pair)
        app.router.add_get("/api/v1/capabilities", self._capabilities)
        app.router.add_get("/api/v1/state", self._state)
        app.router.add_get("/api/v1/events", self._events)
        app.router.add_put("/api/v1/control/power", self._power)
        app.router.add_put("/api/v1/control/mode", self._mode)
        app.router.add_put("/api/v1/control/setpoint", self._setpoint)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self.host, 0, ssl_context=ssl_context
        )
        await self._site.start()
        sockets = self._site._server.sockets
        self.port = sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        await self.close_websockets()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def push_state(self) -> None:
        message = {"type": "snapshot", "snapshot": self.snapshot()}
        dead: list[web.WebSocketResponse] = []
        for websocket in self._websockets:
            try:
                await websocket.send_json(message)
            except ConnectionError:
                dead.append(websocket)
        for websocket in dead:
            self._websockets.discard(websocket)

    async def change_temperature(
        self, temperature: float, *, revision_step: int = 1
    ) -> None:
        self.tank_temperature = temperature
        self.revision += revision_step
        await self.push_state()

    async def reboot(self) -> None:
        self.boot_id = "2" * 32
        self.revision = 1
        await self.push_state()

    async def close_websockets(self) -> None:
        websockets = tuple(self._websockets)
        self._websockets.clear()
        for websocket in websockets:
            await websocket.close()

    async def send_raw(self, payload: str) -> None:
        for websocket in tuple(self._websockets):
            await websocket.send_str(payload)

    def snapshot(self) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "revision": self.revision,
            "captured_at_ms": self.revision * 100,
            "state": {
                "connected": True,
                "unit_on": True,
                "mode": "heating",
                "operation": "heating",
                "defrosting": False,
                "components": {
                    "compressor": True,
                    "fan": True,
                    "fan_level": 2,
                    "water_pump": True,
                    "backup_heater": False,
                    "reversing_valve_request": False,
                },
                "temperatures_c": {
                    "tank": self.tank_temperature,
                    "outlet": 42,
                    "inlet": 38,
                    "outdoor_ambient": 7,
                    "discharge": 60,
                    "suction": 4,
                    "outdoor_coil": 3,
                    "indoor_coil": 35,
                    "ipm": 44,
                },
                "setpoints_c": {
                    "cooling": 12,
                    "heating": 40,
                    "hot_water": 50,
                },
                "readings": {
                    "compressor_frequency_hz": 45,
                    "fan_rpm": 800,
                    "power_w": 1200,
                    "thermal_w": 4100,
                    "cop": 3.42,
                },
                "error": {"active": False, "description": None},
            },
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "device_id": self.device_id,
            "model": "Macon Heat Pump Controller",
            "firmware_version": "test",
            "transports": {"rest": True, "websocket": True},
            "capabilities": {
                "read_state": True,
                "control_power": True,
                "control_mode": True,
                "control_setpoints": True,
                "advanced_parameters": False,
                "raw_registers": False,
                "supported_modes": [
                    "cooling",
                    "floor_heating",
                    "fan_coil_heating",
                    "hot_water",
                    "auto",
                ],
                "setpoint_controls": {
                    "cooling": True,
                    "heating": True,
                    "hot_water": True,
                },
            },
            "setpoint_limits_c": {
                "cooling": {"min": 5, "max": 30},
                "heating": {"min": 20, "max": 60},
                "hot_water": {"min": 20, "max": 60},
            },
        }

    def _authorized(self, request: web.Request) -> bool:
        expected = f"Bearer {self.token}"
        return request.headers.get("Authorization") == expected

    async def _pair(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("code") != self.pairing_code:
            return web.json_response(
                {"error": "invalid pairing code"}, status=401
            )
        return web.json_response(
            {
                "protocol_version": 1,
                "device_id": self.device_id,
                "sha256_fingerprint": self.fingerprint,
                "token": self.token,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _capabilities(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.capabilities())

    async def _state(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self.state_requests += 1
        snapshot = self.snapshot()
        if self.hold_state_response:
            self.hold_state_response = False
            self.release_state_request.clear()
            self.state_request_started.set()
            await self.release_state_request.wait()
        return web.json_response(snapshot)

    async def _command(
        self,
        request: web.Request,
        operation: str,
        payload: tuple,
        body: dict[str, Any],
    ) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        command_id = body.get("command_id")
        if not isinstance(command_id, str):
            return web.json_response({"error": "invalid command"}, status=422)
        self.command_requests.append(body)
        existing = self.commands.get(command_id)
        if existing is not None:
            if existing != (operation, payload):
                return web.json_response(
                    {"error": "command_id conflict"}, status=409
                )
        else:
            self.commands[command_id] = (operation, payload)
        return web.json_response(
            {
                "accepted": True,
                "command_id": command_id,
                "status": "accepted_waiting_for_reported_state",
            },
            status=202,
        )

    async def _power(self, request: web.Request) -> web.Response:
        body = await request.json()
        return await self._command(
            request, "power", (("on", body.get("on")),), body
        )

    async def _mode(self, request: web.Request) -> web.Response:
        body = await request.json()
        return await self._command(
            request, "mode", (("mode", body.get("mode")),), body
        )

    async def _setpoint(self, request: web.Request) -> web.Response:
        body = await request.json()
        return await self._command(
            request,
            "setpoint",
            (("kind", body.get("kind")), ("value", body.get("value"))),
            body,
        )

    async def _events(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        websocket = web.WebSocketResponse(autoping=True)
        await websocket.prepare(request)
        self.websocket_connections += 1
        self._websockets.add(websocket)
        snapshot = self.snapshot()
        await websocket.send_json(
            {
                "type": "hello",
                "protocol_version": 1,
                "device_id": self.device_id,
                "boot_id": self.boot_id,
                "revision": self.revision,
            }
        )
        await websocket.send_json(
            {"type": "snapshot", "snapshot": snapshot}
        )
        try:
            async for _ in websocket:
                pass
        finally:
            self._websockets.discard(websocket)
        return websocket

    def _create_identity(self) -> tuple[Path, Path, str]:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, self.device_id)]
        )
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address(self.host))]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path = self._temp_path / f"{self.device_id}.crt"
        key_path = self._temp_path / f"{self.device_id}.key"
        cert_der = certificate.public_bytes(serialization.Encoding.DER)
        cert_path.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM)
        )
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path, hashlib.sha256(cert_der).hexdigest()


async def wait_for(
    predicate: Any, *, timeout: float = 2.0, interval: float = 0.01
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(interval)
