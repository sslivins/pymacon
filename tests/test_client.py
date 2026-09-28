"""Tests for the standalone async Macon client."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from fake_controller import FakeController, wait_for

from pymacon import (
    ControllerCapabilities,
    ControllerDiagnostics,
    ControllerState,
    MaconAuthenticationError,
    MaconCertificateError,
    MaconClient,
    MaconCommandConflictError,
    MaconCommandValidationError,
    MaconConnectionError,
    MaconControlUnavailableError,
    MaconPairingError,
    MaconProtocolError,
    OtaReleaseInfo,
    OtaStatus,
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


def test_capabilities_tolerate_missing_setpoint_controls(controller):
    data = controller.capabilities()
    del data["capabilities"]["setpoint_controls"]

    caps = ControllerCapabilities.from_dict(data)

    assert caps.setpoint_controls.cooling is False
    assert caps.setpoint_controls.heating is False
    assert caps.setpoint_controls.hot_water is False


def test_readings_tolerate_firmware_without_the_new_keys(controller):
    """Older firmware omits these keys entirely; parsing must not fail."""
    state = ControllerState.from_dict(controller.snapshot()["state"])

    assert state.readings.primary_eev is None
    assert state.readings.ac_voltage is None
    assert state.readings.ac_current is None
    assert state.readings.dc_voltage is None
    # The pre-existing fields must still parse normally.
    assert state.readings.cop == 3.42


def test_readings_accept_explicit_nulls(controller):
    """The controller sends null when a register has not been read."""
    data = controller.snapshot()["state"]
    data["readings"].update(
        primary_eev=None, ac_voltage=None, ac_current=None, dc_voltage=None
    )

    state = ControllerState.from_dict(data)

    assert state.readings.primary_eev is None
    assert state.readings.dc_voltage is None


def test_readings_parse_the_new_keys(controller):
    data = controller.snapshot()["state"]
    data["readings"].update(
        primary_eev=350, ac_voltage=236, ac_current=5, dc_voltage=380.0
    )

    state = ControllerState.from_dict(data)

    assert state.readings.primary_eev == 350
    assert state.readings.ac_voltage == 236
    assert state.readings.ac_current == 5
    assert state.readings.dc_voltage == 380.0


def test_zero_eev_is_a_real_value_not_unknown(controller):
    """0 steps means the valve is fully CLOSED, which is not the same as
    'not read'. The two must never collapse onto each other."""
    data = controller.snapshot()["state"]
    data["readings"]["primary_eev"] = 0

    state = ControllerState.from_dict(data)

    assert state.readings.primary_eev == 0
    assert state.readings.primary_eev is not None


def test_capabilities_default_missing_setpoint_flags(controller):
    data = controller.capabilities()
    data["capabilities"]["setpoint_controls"] = {"cooling": True}

    caps = ControllerCapabilities.from_dict(data)

    assert caps.setpoint_controls.cooling is True
    assert caps.setpoint_controls.heating is False
    assert caps.setpoint_controls.hot_water is False


def test_capabilities_reject_non_object_setpoint_controls(controller):
    data = controller.capabilities()
    data["capabilities"]["setpoint_controls"] = "nope"

    with pytest.raises(MaconProtocolError):
        ControllerCapabilities.from_dict(data)


def test_capabilities_parse_network_identity(controller):
    caps = ControllerCapabilities.from_dict(controller.capabilities())

    assert caps.ip_address == "192.168.1.21"
    assert caps.local_hostname == "arctic-e540.local"


def test_capabilities_tolerate_missing_network(controller):
    data = controller.capabilities()
    del data["network"]

    caps = ControllerCapabilities.from_dict(data)

    assert caps.ip_address is None
    assert caps.local_hostname is None


def test_capabilities_tolerate_null_ip_address(controller):
    data = controller.capabilities()
    data["network"]["ip_address"] = None

    caps = ControllerCapabilities.from_dict(data)

    assert caps.ip_address is None
    assert caps.local_hostname == "arctic-e540.local"


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


@pytest.mark.asyncio
async def test_check_updates_reports_available_release(controller):
    controller.ota_release = {
        "update_available": True,
        "current_version": "2.11.6",
        "latest_version": "2.12.0",
        "published_at": "2026-08-19T00:00:00Z",
        "download_ready": True,
        "release_notes": "Adds crash-loop safe mode.",
    }
    client = make_client(controller)
    await client.async_setup()
    info = await client.async_check_updates()
    assert isinstance(info, OtaReleaseInfo)
    assert info.update_available is True
    assert info.download_ready is True
    assert info.current_version == "2.11.6"
    assert info.latest_version == "2.12.0"
    assert info.release_notes == "Adds crash-loop safe mode."
    await client.stop()


@pytest.mark.asyncio
async def test_check_updates_when_up_to_date(controller):
    client = make_client(controller)
    await client.async_setup()
    info = await client.async_check_updates()
    assert info.update_available is False
    assert info.download_ready is False
    await client.stop()


@pytest.mark.asyncio
async def test_ota_status_reports_progress(controller):
    controller.ota_status = {
        "state": "downloading",
        "progress": 42,
        "bytes_downloaded": 4200,
        "total_bytes": 10000,
        "current_version": "2.11.6",
        "new_version": "2.12.0",
        "pending_verify": False,
    }
    client = make_client(controller)
    await client.async_setup()
    status = await client.async_ota_status()
    assert isinstance(status, OtaStatus)
    assert status.state == "downloading"
    assert status.progress == 42
    assert status.in_progress is True
    assert status.failed is False
    assert status.new_version == "2.12.0"
    await client.stop()


@pytest.mark.asyncio
async def test_ota_status_defaults_when_idle_and_blank(controller):
    controller.ota_status = {"state": "", "progress": 150}
    client = make_client(controller)
    await client.async_setup()
    status = await client.async_ota_status()
    assert status.state == "idle"
    assert status.progress == 100  # clamped
    assert status.in_progress is False
    assert status.new_version is None
    await client.stop()


@pytest.mark.asyncio
async def test_start_update_triggers_download(controller):
    controller.ota_release = {
        "update_available": True,
        "current_version": "2.11.6",
        "latest_version": "2.12.0",
        "published_at": "",
        "download_ready": True,
    }
    client = make_client(controller)
    await client.async_setup()
    await client.async_start_update()
    assert controller.ota_started is True
    assert controller.ota_status["state"] == "downloading"
    await client.stop()


@pytest.mark.asyncio
async def test_start_update_without_available_raises_validation(controller):
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(MaconCommandValidationError):
        await client.async_start_update()
    assert controller.ota_started is False
    await client.stop()


@pytest.mark.asyncio
async def test_start_update_conflicts_when_already_running(controller):
    controller.ota_release = {
        "update_available": True,
        "current_version": "2.11.6",
        "latest_version": "2.12.0",
        "published_at": "",
        "download_ready": True,
    }
    controller.ota_status = {**controller.ota_status, "state": "verifying"}
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(MaconCommandConflictError):
        await client.async_start_update()
    await client.stop()


@pytest.mark.asyncio
async def test_ota_endpoints_require_authentication(controller):
    client = MaconClient(
        controller.host,
        "b" * 64,
        controller.fingerprint,
        device_id=controller.device_id,
        port=controller.port,
    )
    with pytest.raises(MaconAuthenticationError):
        await client.async_check_updates()
    await client.stop()


def _state_payload(**error_fields):
    controller = FakeController.__new__(FakeController)
    controller.device_id = "arctic-abcdef012345"
    controller.boot_id = "boot"
    controller.revision = 1
    controller.tank_temperature = 40
    payload = controller.snapshot()["state"]
    payload["error"] = error_fields
    return payload


def test_error_state_parses_enriched_fields():
    state = ControllerState.from_dict(
        _state_payload(
            active=True,
            code="P02",
            name="Water flow fault",
            description="Water flow switch open",
            severity="critical",
            help_url="https://arcticheatpumps.freshdesk.com/support/solutions/articles/60000832838",
        )
    )
    assert state.error.active is True
    assert state.error.code == "P02"
    assert state.error.name == "Water flow fault"
    assert state.error.description == "Water flow switch open"
    assert state.error.severity == "critical"
    assert state.error.help_url == (
        "https://arcticheatpumps.freshdesk.com/support/solutions/articles/60000832838"
    )


def test_error_state_back_compat_without_new_fields():
    state = ControllerState.from_dict(
        _state_payload(active=False, description=None)
    )
    assert state.error.active is False
    assert state.error.code is None
    assert state.error.name is None
    assert state.error.severity is None
    assert state.error.help_url is None


@pytest.mark.asyncio
async def test_capabilities_advertise_diagnostics_and_restart(controller):
    client = make_client(controller)
    await client.async_setup()
    assert client.capabilities.diagnostics is True
    assert client.capabilities.restart is True
    await client.stop()


@pytest.mark.asyncio
async def test_capabilities_default_diagnostics_off_for_old_firmware(
    controller,
):
    controller.supports_diagnostics = False
    client = make_client(controller)
    await client.async_setup()
    assert client.capabilities.diagnostics is False
    assert client.capabilities.restart is False
    await client.stop()


@pytest.mark.asyncio
async def test_fetch_diagnostics_parses_master_document(controller):
    client = make_client(controller)
    await client.async_setup()
    diagnostics = await client.async_fetch_diagnostics()
    assert isinstance(diagnostics, ControllerDiagnostics)
    assert diagnostics.boot_id == controller.boot_id
    assert diagnostics.uptime_ms == 123456
    assert diagnostics.last_reset_reason == "power_on"
    assert diagnostics.brownout_count == 2
    assert diagnostics.watchdog_count == 1
    assert diagnostics.safe_mode is False
    assert diagnostics.wifi_ssid == "home"
    assert diagnostics.wifi_rssi_dbm == -61
    assert diagnostics.wifi_disconnect_count == 3
    assert diagnostics.time_synced is True
    assert diagnostics.bus_role == "master"
    assert diagnostics.bus_polls_ok == 500
    assert diagnostics.bus_writes_failed == 1
    assert diagnostics.bus_frames_ok is None
    await client.stop()


@pytest.mark.asyncio
async def test_fetch_diagnostics_tolerates_missing_and_malformed_fields(
    controller,
):
    controller.diagnostics = {
        "uptime_ms": -5,
        "system": "nope",
        "wifi": {"connected": "yes", "ssid": None, "rssi_dbm": True},
        "rs485": {"role": "future_role", "frames_ok": 9, "resyncs": 1},
    }
    client = make_client(controller)
    await client.async_setup()
    diagnostics = await client.async_fetch_diagnostics()
    assert diagnostics.uptime_ms is None
    assert diagnostics.brownout_count is None
    assert diagnostics.wifi_connected is None
    assert diagnostics.wifi_ssid is None
    assert diagnostics.wifi_rssi_dbm is None
    assert diagnostics.bus_role == "future_role"
    assert diagnostics.bus_frames_ok == 9
    assert diagnostics.bus_polls_ok is None
    await client.stop()


@pytest.mark.asyncio
async def test_fetch_diagnostics_failure_does_not_mark_unavailable(
    controller,
):
    controller.supports_diagnostics = False
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(MaconConnectionError):
        await client.async_fetch_diagnostics()
    assert client.available is True
    await client.stop()


@pytest.mark.asyncio
async def test_fetch_diagnostics_rejects_other_device(controller):
    client = make_client(controller)
    await client.async_setup()
    controller.device_id = "arctic-aabbccddeeff"
    with pytest.raises(MaconProtocolError):
        await client.async_fetch_diagnostics()
    await client.stop()


@pytest.mark.asyncio
async def test_restart_names_current_boot(controller):
    client = make_client(controller)
    await client.async_setup()
    result = await client.async_restart(command_id="restart-1")
    assert result.accepted is True
    assert result.command_id == "restart-1"
    assert result.status == "restarting"
    assert controller.restart_requests == [
        {"command_id": "restart-1", "boot_id": controller.boot_id}
    ]
    await client.stop()


@pytest.mark.asyncio
async def test_restart_with_stale_boot_is_conflict(controller):
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(MaconCommandConflictError):
        await client.async_restart(boot_id="f" * 32)
    await client.stop()


@pytest.mark.asyncio
async def test_restart_during_ota_is_unavailable(controller):
    controller.restart_unavailable = True
    client = make_client(controller)
    await client.async_setup()
    with pytest.raises(MaconControlUnavailableError):
        await client.async_restart()
    await client.stop()
