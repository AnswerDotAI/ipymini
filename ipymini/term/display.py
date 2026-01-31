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
    def __init__(self, shell=None):
        "DisplayHook that captures last result metadata."
        super().__init__(shell=shell)
        self.last = None
        self.last_metadata = None
        self.last_execution_count = None

    def write_output_prompt(self): self.last_execution_count = self.prompt_count

    def write_format_data(self, format_dict, md_dict=None):
        "Capture formatted output from displayhook."
        self.last = format_dict
        self.last_metadata = md_dict or {}

    def finish_displayhook(self): self._is_active = False
