"""Keep credential work bounded without blocking the serving event loop."""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from threading import BoundedSemaphore


class CredentialBusyError(RuntimeError):
    pass


class _BoundedWorker:
    def __init__(self, workers, name):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.capacity = BoundedSemaphore(workers)

    async def run(self, function, *args, **kwargs):
        # Reject before submission: there is no unbounded executor queue and no
        # timed-out write that might take effect after a retry response.
        if not self.capacity.acquire(blocking=False):
            raise CredentialBusyError("Credential checks are busy")
        try:
            context = contextvars.copy_context()
            future = self.executor.submit(context.run, partial(function, *args, **kwargs))
        except BaseException:
            self.capacity.release()
            raise
        # A disconnected client must not release a worker still hashing or
        # writing. Only the actual concurrent future owns this capacity.
        future.add_done_callback(lambda completed: self.capacity.release())
        wrapped = asyncio.wrap_future(future)
        # Retrieve a late exception even if its client disconnected. This does
        # not change the result/exception delivered to a caller still awaiting.
        wrapped.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
        # Cancelling wait() detaches its waiter without cancelling the running
        # concurrent operation or creating a second shield future with an
        # unobserved late exception.
        await asyncio.wait((wrapped,))
        return wrapped.result()


_reads = _BoundedWorker(2, "sparkflow-auth-read")
_credentials = _BoundedWorker(4, "sparkflow-credential")


async def run_auth_read(function, *args, **kwargs):
    return await _reads.run(function, *args, **kwargs)


async def run_credential_work(function, *args, **kwargs):
    return await _credentials.run(function, *args, **kwargs)


@asynccontextmanager
async def credential_slot(app):
    gate = app.state.credential_gate
    if gate.locked():
        raise CredentialBusyError("Credential checks are busy")
    await gate.acquire()
    try:
        yield
    finally:
        gate.release()
