"""Tests for the standalone async Macon client."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from fake_controller import FakeController, wait_for

from pymacon import (
    ControllerCapabilities,
    MaconAuthenticationError,
    MaconCertificateError,
    MaconClient,
    MaconCommandConflictError,
    MaconPairingError,
    MaconProtocolError,
)


@pytest.fixture
async def controller(tmp_path):
    fake = await FakeController(tmp_path).start()
    try:
        yield fake
    finally:
        await fake.stop()


def make_client(
    controller: FakeController,
    **kwargs,
) -> MaconClient:
    options = {
        "device_id": controller.device_id,
        "port": controller.port,
        "reconciliation_interval": 0.15,
        "fallback_poll_interval": 0.05,
        "reconnect_min_delay": 0.02,
        "reconnect_max_delay": 0.05,
        "reconnect_jitter": 0,
    }
    options.update(kwargs)
    return MaconClient(
        controller.host,
        controller.token,
        controller.fingerprint,
        **options,
    )


@pytest.mark.asyncio
async def test_pairing_claims_token_and_verifies_identity(controller):
    result = await MaconClient.pair(
        controller.host,
        controller.pairing_code,
        port=controller.port,
    )
    assert result.device_id == controller.device_id
    assert result.fingerprint == controller.fingerprint
    assert result.token == controller.token

    with pytest.raises(MaconPairingError):
        await MaconClient.pair(
            controller.host,
            "000000",
            port=controller.port,
        )


@pytest.mark.asyncio
async def test_pairing_rejects_forged_fingerprint(controller):
    controller.reported_fingerprint = "0" * 64
    with pytest.raises(MaconCertificateError):
        await MaconClient.pair(
            controller.host,
            controller.pairing_code,
            port=controller.port,
        )


@pytest.mark.asyncio
async def test_tls_pin_and_authentication_are_strict(controller):
    wrong_pin = "0" * 64
    client = MaconClient(
        controller.host,
        controller.token,
        wrong_pin,
        device_id=controller.device_id,
        port=controller.port,
    )
    with pytest.raises(MaconCertificateError):
        await client.async_setup()
    await client.stop()

    client = MaconClient(
        controller.host,
        "0" * 64,
        controller.fingerprint,
        device_id=controller.device_id,
        port=controller.port,
    )
    with pytest.raises(MaconAuthenticationError):
        await client.async_setup()
    assert client._session is None


@pytest.mark.asyncio
async def test_setup_returns_typed_capabilities_and_state(controller):
    client = make_client(controller)
    snapshot = await client.async_setup()
    assert client.capabilities is not None
    assert client.capabilities.websocket is True
    assert client.capabilities.cooling_range.minimum == 5
    assert snapshot.device_id == controller.device_id
    assert snapshot.state.temperatures_c.tank == 40
    assert snapshot.state.readings.cop == 3.42
    await client.stop()


def test_capabilities_require_per_setpoint_flags(controller):
    data = controller.capabilities()
    del data["capabilities"]["setpoint_controls"]

    with pytest.raises(MaconProtocolError):
        ControllerCapabilities.from_dict(data)


@pytest.mark.asyncio
async def test_reconciliation_refreshes_dynamic_capabilities(controller):
    client = make_client(controller)
    changes = []
    client.subscribe_capabilities(changes.append)
    await client.start()
    await wait_for(lambda: client.stream_connected)
    assert client.capabilities is not None
    assert client.capabilities.control_mode is True

    original_capabilities = controller.capabilities

    def passive_capabilities():
        data = original_capabilities()
        data["capabilities"]["control_power"] = False
        data["capabilities"]["control_mode"] = False
        data["capabilities"]["control_setpoints"] = False
        data["capabilities"]["supported_modes"] = []
        data["capabilities"]["setpoint_controls"] = {
            "cooling": False,
            "heating": False,
            "hot_water": False,
        }
        return data

    controller.capabilities = passive_capabilities
    await wait_for(
        lambda: client.capabilities is not None
        and not client.capabilities.control_mode
    )
    assert changes[-1].supported_modes == ()
    await client.stop()


@pytest.mark.asyncio
async def test_push_ordering_reconciliation_and_reboot(controller):
    client = make_client(controller)
    accepted = []
    client.subscribe(accepted.append)
    await client.start()
    await wait_for(lambda: client.stream_connected)

    await controller.change_temperature(41)
    await wait_for(
        lambda: client.snapshot is not None
        and client.snapshot.state.temperatures_c.tank == 41
    )
    accepted_count = len(accepted)
    await controller.push_state()
    await asyncio.sleep(0.05)
    assert len(accepted) == accepted_count

    state_requests = controller.state_requests
    await controller.change_temperature(43, revision_step=2)
    await wait_for(lambda: controller.state_requests > state_requests)
    assert client.snapshot is not None
    assert client.snapshot.revision == controller.revision

    await controller.reboot()
    await wait_for(
        lambda: client.snapshot is not None
        and client.snapshot.boot_id == controller.boot_id
    )
    assert client.snapshot is not None
    assert client.snapshot.revision == 1
    await client.stop()


@pytest.mark.asyncio
async def test_disconnect_uses_fallback_polling_and_reconnects(controller):
    client = make_client(controller)
    statuses = []
    client.subscribe_status(statuses.append)
    await client.start()
    await wait_for(lambda: client.stream_connected)
    assert client.available
    await controller.close_websockets()
    await wait_for(lambda: not client.stream_connected)

    controller.tank_temperature = 47
    controller.revision += 1
    await wait_for(
        lambda: client.snapshot is not None
        and client.snapshot.state.temperatures_c.tank == 47
    )
    await wait_for(lambda: client.stream_connected)
    await client.stop()
    assert not client.running
    assert statuses[-1].available is False


@pytest.mark.asyncio
async def test_external_session_remains_owned_by_caller(controller):
    async with aiohttp.ClientSession() as session:
        client = make_client(controller, session=session)
        await client.start()
        await wait_for(lambda: client.stream_connected)
        await client.stop()
        await client.stop()
        assert not session.closed


@pytest.mark.asyncio
async def test_rotated_credential_stops_client_for_reauthentication(
    controller,
):
    client = make_client(controller)
    await client.start()
    await wait_for(lambda: client.stream_connected)
    controller.token = "b" * 64
    await controller.close_websockets()
    await wait_for(lambda: not client.running)
    assert isinstance(client.last_error, MaconAuthenticationError)
    assert client.available is False
    await client.stop()


@pytest.mark.asyncio
async def test_poll_authentication_failure_closes_active_stream(controller):
    client = make_client(controller)
    await client.start()
    await wait_for(lambda: client.stream_connected)
    controller.token = "b" * 64
    await wait_for(lambda: not client.running)
    await wait_for(lambda: not controller._websockets)
    assert isinstance(client.last_error, MaconAuthenticationError)
    assert client.stream_connected is False
    await client.stop()


@pytest.mark.asyncio
async def test_malformed_websocket_json_reconnects(controller):
    client = make_client(controller)
    await client.start()
    await wait_for(lambda: client.stream_connected)
    connections = controller.websocket_connections
    await controller.send_raw("{")
    await wait_for(
        lambda: controller.websocket_connections > connections
        and client.stream_connected
    )
    await client.stop()


@pytest.mark.asyncio
async def test_delayed_old_boot_rest_cannot_overwrite_reboot(controller):
    client = make_client(
        controller,
        reconciliation_interval=60,
        fallback_poll_interval=60,
    )
    await client.start()
    await wait_for(lambda: client.stream_connected)
    controller.hold_state_response = True
    delayed = asyncio.create_task(client.fetch_state())
    await controller.state_request_started.wait()

    await controller.reboot()
    await wait_for(
        lambda: client.snapshot is not None
        and client.snapshot.boot_id == controller.boot_id
    )
    controller.release_state_request.set()
    returned = await delayed
    assert client.snapshot is not None
    assert client.snapshot.boot_id == controller.boot_id
    assert client.snapshot.revision == 1
    assert returned.boot_id == controller.boot_id
    await client.stop()


@pytest.mark.asyncio
async def test_multiple_controllers_have_independent_state(tmp_path):
    first = await FakeController(
        tmp_path,
        device_id="arctic-001122334455",
        token="a" * 64,
    ).start()
    second = await FakeController(
        tmp_path,
        device_id="arctic-aabbccddeeff",
        token="b" * 64,
    ).start()
    first_client = make_client(first)
    second_client = make_client(second)
    try:
        await asyncio.gather(first_client.start(), second_client.start())
        await wait_for(
            lambda: first_client.stream_connected
            and second_client.stream_connected
        )
        await first.change_temperature(51)
        await second.change_temperature(33)
        await wait_for(
            lambda: first_client.snapshot is not None
            and first_client.snapshot.state.temperatures_c.tank == 51
        )
        await wait_for(
            lambda: second_client.snapshot is not None
            and second_client.snapshot.state.temperatures_c.tank == 33
        )
        assert first_client.device_id != second_client.device_id
        assert first_client.fingerprint != second_client.fingerprint
        assert first_client.snapshot.state.temperatures_c.tank == 51
        assert second_client.snapshot.state.temperatures_c.tank == 33
    finally:
        await asyncio.gather(
            first_client.stop(),
            second_client.stop(),
            first.stop(),
            second.stop(),
        )


@pytest.mark.asyncio
async def test_wrong_device_and_malformed_messages_are_rejected(controller):
    client = make_client(controller)
    await client.async_setup()
    controller.device_id = "arctic-deadbeef0000"
    with pytest.raises(MaconProtocolError):
        await client.fetch_state()
    await client.stop()


@pytest.mark.asyncio
async def test_commands_require_idempotent_ids_and_deduplicate(controller):
    client = make_client(controller)
    await client.async_setup()

    first = await client.async_set_power(True, command_id="same-command")
    second = await client.async_set_power(True, command_id="same-command")
    assert first == second
    assert len(controller.command_requests) == 2

    with pytest.raises(MaconCommandConflictError):
        await client.async_set_power(False, command_id="same-command")

    generated = await client.async_set_cooling_setpoint(24)
    assert len(generated.command_id) == 36
    assert generated.accepted is True
    await client.stop()


@pytest.mark.asyncio
async def test_client_rejects_invalid_command_values_before_http(controller):
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(ValueError):
        await client.async_set_mode("")
    with pytest.raises(ValueError):
        await client.async_set_setpoint("unsupported", 20)
    with pytest.raises(ValueError):
        await client.async_set_cooling_setpoint(20, command_id="")
    await client.stop()
