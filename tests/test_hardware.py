"""Opt-in physical-controller test for the standalone Macon client."""

from __future__ import annotations

import asyncio
import os

import aiohttp
import pytest

from pymacon import MaconClient

pytestmark = pytest.mark.skipif(
    os.environ.get("MACON_CLIENT_HARDWARE") != "1",
    reason="set MACON_CLIENT_HARDWARE=1 for physical-controller tests",
)


@pytest.mark.asyncio
async def test_physical_controller_rest_and_push():
    http_url = os.environ.get("MACON_URL", "http://192.168.1.21")
    host = os.environ.get("MACON_HOST", "192.168.1.21")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            http_url + "/api/test/ha-token"
        ) as response:
            response.raise_for_status()
            token = (await response.json())["token"]
        async with session.get(
            http_url + "/api/test/ha-identity"
        ) as response:
            response.raise_for_status()
            fingerprint = (await response.json())[
                "sha256_fingerprint"
            ]

        client = MaconClient(
            host,
            token,
            fingerprint,
            session=session,
            reconciliation_interval=60,
            fallback_poll_interval=1,
            reconnect_min_delay=0.1,
            reconnect_max_delay=1,
        )
        pushed = asyncio.Event()
        changed = None

        def handle_snapshot(current):
            if (
                changed is not None
                and current.state.temperatures_c.tank == changed
            ):
                pushed.set()

        client.subscribe(handle_snapshot)
        try:
            snapshot = await client.start()
            async with asyncio.timeout(10):
                while not client.stream_connected:
                    await asyncio.sleep(0.05)
            assert client.available
            assert client.capabilities is not None
            assert client.device_id == snapshot.device_id
            original = snapshot.state.temperatures_c.tank
            changed = 41 if original != 41 else 42
            async with session.post(
                http_url + "/api/test/set-demo-field",
                json={"water_tank_temp": changed},
            ) as response:
                response.raise_for_status()
            await asyncio.wait_for(pushed.wait(), timeout=5)
        finally:
            if "original" in locals():
                async with session.post(
                    http_url + "/api/test/set-demo-field",
                    json={"water_tank_temp": original},
                ):
                    pass
            await client.stop()
