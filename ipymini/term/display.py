import contextvars

from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher


class MiniDisplayPublisher(DisplayPublisher):
    def __init__(self, sender=None, live_var=None):
        "Collect display_pub events for IOPub."
        super().__init__()
        self.events = []
        self.sender = sender
        self.live_var = live_var

    def set_sender(self, sender):
        "Set live display sender."
        self.sender = sender

    def publish(self, data, metadata=None, transient=None, update=False, **kwargs):
        "Record display data/update for later emission."
        buffers = kwargs.get("buffers")
        event = dict(type="display", data=data, metadata=metadata or {}, transient=transient or {}, update=bool(update), buffers=buffers)
        live = self.live_var.get() if self.live_var is not None else False
        if self.sender is not None and live: self.sender(event)
        else: self.events.append(event)

    def clear_output(self, wait: bool = False):
        event = {"type": "clear_output", "wait": bool(wait)}
        live = self.live_var.get() if self.live_var is not None else False
        if self.sender is not None and live: self.sender(event)
        else: self.events.append(event)


class MiniDisplayHook(DisplayHook):
    "DisplayHook that captures last result metadata, isolated per execution context."

    # ContextVars so concurrent (unlocked) executions cannot read or clobber each other's result
    _last = contextvars.ContextVar("ipymini.dh_last", default=None)
    _last_metadata = contextvars.ContextVar("ipymini.dh_last_metadata", default=None)
    _last_execution_count = contextvars.ContextVar("ipymini.dh_last_execution_count", default=None)

    @property
    def last(self): return self._last.get()
    @last.setter
    def last(self, v): self._last.set(v)

    @property
    def last_metadata(self): return self._last_metadata.get()
    @last_metadata.setter
    def last_metadata(self, v): self._last_metadata.set(v)

    @property
    def last_execution_count(self): return self._last_execution_count.get()
    @last_execution_count.setter
    def last_execution_count(self, v): self._last_execution_count.set(v)

    def write_output_prompt(self): self.last_execution_count = self.prompt_count

    def write_format_data(self, format_dict, md_dict=None):
        "Capture formatted output from displayhook."
        self.last = format_dict
        self.last_metadata = md_dict or {}

    def finish_displayhook(self): self._is_active = False
