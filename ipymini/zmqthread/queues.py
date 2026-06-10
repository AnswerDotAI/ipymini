import logging
from microio import BrokenResourceError, ClosedResourceError, create_channel

log = logging.getLogger("ipymini.zmqthread")


class ThreadBoundAsyncQueue:
    "Thread-safe put + asyncio get once bound to an event loop."

    def __init__(self):
        self.send, self.receive = create_channel()
        self.suppress_late = False

    def bind(self, loop): self.receive.bind(loop)

    def put(self, item):
        try: self.send.send_nowait(item)
        except (BrokenResourceError, ClosedResourceError):
            if not self.suppress_late: log.error("Queue put after loop lost; dropping")

    async def get(self): return await self.receive.receive()

    def drain_nowait(self) -> list: return self.receive.drain_nowait()

    def close(self):
        self.suppress_late_puts()
        return self.send.close()

    def fail(self, exc: BaseException):
        self.suppress_late_puts()
        return self.send.fail(exc)

    def suppress_late_puts(self): self.suppress_late = True
