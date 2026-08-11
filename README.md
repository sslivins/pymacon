# pymacon

Secure async REST and WebSocket client for the
**Macon Heat Pump Controller**. It is the transport library used by the
[Macon Home Assistant integration](https://github.com/sslivins/hass-macon).

Each `MaconClient` owns the credential, pinned certificate,
ordered state, reconnect loop, and reconciliation state for exactly one
controller. Multiple controllers therefore remain fully isolated.

## Install

```bash
pip install pymacon
```

## Usage

```python
from pymacon import MaconClient

client = MaconClient(
    "192.168.1.21",
    token,
    certificate_fingerprint,
    # arctic-* is retained because it is the firmware wire-format ID.
    device_id="arctic-001122334455",
)
client.subscribe(handle_snapshot)
client.subscribe_status(handle_status)
await client.start()
```

Call `await client.stop()` when unloading the owning config entry. Sessions
passed into the constructor remain owned by the caller; sessions created by
the client are closed automatically.

Control methods acknowledge that a command was accepted, but they never
optimistically mutate local state. Subscribe to snapshots to observe the
controller's reported state:

```python
await client.async_set_cooling_setpoint(12)
await client.async_set_power(False)
```

Only controls advertised by `client.capabilities` should be called. The
firmware rejects unavailable, unsafe, or unsupported operations.

## Development

```bash
python -m pip install -e ".[tests]"
pytest
ruff check src tests
mypy src
```
