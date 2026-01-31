import contextvars
from contextlib import contextmanager
from typing import Callable

from .display import MiniDisplayHook, MiniDisplayPublisher
from .io import thread_local_io
from .streams import MiniStream


class IPythonCapture:
    def __init__(self, shell, request_input: Callable[[str, bool], str]):
        self.shell = shell
        self.request_input = request_input
        self.display_sender = None
        self.display_live = contextvars.ContextVar("ipymini.display_live", default=False)
        self.shell.display_pub = MiniDisplayPublisher(self.display_sender, self.display_live)
        self.shell.displayhook = MiniDisplayHook(shell=self.shell)
        self.shell.display_trap.hook = self.shell.displayhook

        self.stream_events = []
        self.stream_sender = None
        self.stream_live = contextvars.ContextVar("ipymini.stream_live", default=False)
        self.stdout = MiniStream("stdout", self.stream_events, sink=self._emit_stream)
        self.stderr = MiniStream("stderr", self.stream_events, sink=self._emit_stream)

    def set_stream_sender(self, sender: Callable[[str, str], None] | None):
        self.stream_sender = sender
        ev = None if sender is not None else self.stream_events
        self.stdout.events, self.stderr.events = ev, ev

    def set_display_sender(self, sender: Callable[[dict], None] | None):
        self.display_sender = sender
        if hasattr(self.shell.display_pub, "set_sender"): self.shell.display_pub.set_sender(sender)

    def reset(self):
        self.shell.display_pub.events.clear()
        self.shell.displayhook.last = None
        self.shell.displayhook.last_metadata = None
        self.shell.displayhook.last_execution_count = None
        self.shell._last_traceback = None
        self.stream_events.clear()

    @contextmanager
    def capture(self, *, allow_stdin: bool, silent: bool):
        display_token = self.display_live.set(not silent and self.display_sender is not None)
        stream_token = self.stream_live.set(not silent and self.stream_sender is not None)
        try:
            with thread_local_io(self.shell, self.stdout, self.stderr, self.request_input, allow_stdin): yield
        finally:
            try:
                self.stdout.flush()
                self.stderr.flush()
            except Exception: pass
            self.stream_live.reset(stream_token)
            self.display_live.reset(display_token)

    def consume_payload(self) -> list[dict]:
        payload = self.shell.payload_manager.read_payload()
        self.shell.payload_manager.clear_payload()
        return self._dedupe_set_next_input(payload)

    def snapshot(self, *, result=None, result_metadata=None, execution_count=None) -> dict:
        streams = [] if self.stream_sender is not None else list(self.stream_events)
        display_events = [] if self.display_sender is not None else list(self.shell.display_pub.events)
        if result is None: result = self.shell.displayhook.last
        if result_metadata is None: result_metadata = self.shell.displayhook.last_metadata or {}
        if execution_count is None: execution_count = self.shell.displayhook.last_execution_count
        return dict(streams=streams, display=display_events, result=result, result_metadata=result_metadata, execution_count=execution_count)

    def _emit_stream(self, name: str, text: str):
        if self.stream_live.get() and self.stream_sender is not None and text: self.stream_sender(name, text)

    def _dedupe_set_next_input(self, payload: list[dict]) -> list[dict]:
        "Deduplicate set_next_input payloads, keeping the newest."
        if not payload: return payload
        seen = False
        deduped = []
        for item in reversed(payload):
            if isinstance(item, dict) and item.get("source") == "set_next_input":
                if seen: continue
                seen = True
            deduped.append(item)
        return list(reversed(deduped))
