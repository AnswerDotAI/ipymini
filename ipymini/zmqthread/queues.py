import asyncio, threading, logging
from collections import deque

log = logging.getLogger("ipymini.zmqthread")


class ThreadBoundAsyncQueue:
    "Thread-safe put + asyncio get once bound to an event loop."

    def __init__(self):
        self.loop, self.q, self.pending, self.lock = None, None, deque(), threading.Lock()
        self.suppress_late = False
        self.bound_once = False

    def bind(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.q = asyncio.Queue()
        self.bound_once = True
        with self.lock:
            for item in self.pending: self.q.put_nowait(item)
            self.pending.clear()

    def put(self, item):
        if self.loop is None or self.q is None:
            if self.bound_once:
                if self.suppress_late: return
                log.error("Queue put after loop lost; dropping")
                return
            with self.lock: self.pending.append(item)
            return
        try: self.loop.call_soon_threadsafe(self.q.put_nowait, item)
        except RuntimeError:
            if self.bound_once:
                if self.suppress_late: return
                log.error("Queue put after loop lost; dropping")
                return
            with self.lock: self.pending.append(item)

    async def get(self):
        if self.q is None: raise RuntimeError("queue not bound")
        return await self.q.get()

    def drain_nowait(self) -> list:
        if self.q is None: return []
        out = []
        while True:
            try: out.append(self.q.get_nowait())
            except asyncio.QueueEmpty: return out

    def suppress_late_puts(self): self.suppress_late = True
