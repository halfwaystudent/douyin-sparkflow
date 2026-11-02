"""Keep credential work bounded without blocking the serving event loop."""

import asyncio
from contextlib import asynccontextmanager


class CredentialBusyError(RuntimeError):
    pass


@asynccontextmanager
async def credential_slot(app):
    gate = app.state.credential_gate
    try:
        await asyncio.wait_for(gate.acquire(), timeout=app.state.credential_timeout)
    except asyncio.TimeoutError as exc:
        raise CredentialBusyError("Credential checks are busy") from exc
    try:
        yield
    finally:
        gate.release()
