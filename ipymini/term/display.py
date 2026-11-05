import contextvars, sys
from pathlib import PurePath

from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher
from IPython.core.formatters import DisplayFormatter
from IPython.display import DisplayObject, TextDisplayObject
from fastcore.basics import patch
from fastcore.xtras import truncstr

prose_types = {'pandas.DataFrame', 'pandas.io.formats.style.Styler', 'polars.dataframe.frame.DataFrame', 'polars.series.series.Series'}


def _is_prose(o): return any(f'{c.__module__}.{c.__qualname__}' in prose_types for c in type(o).__mro__)


def full_metadata(obj, metadata): return {**metadata, "full": True} if getattr(obj, "full", False) else metadata


@patch
def format(self:DisplayFormatter, obj, include=None, exclude=None):
    data,meta = self._orig_format(obj, include=include, exclude=exclude)
    if 'text/html' in data and _is_prose(obj): meta.setdefault('text/html', {})['prose'] = True
    return data,full_metadata(obj, meta)

@patch
def __repr__(self:DisplayObject):
    s = self.data or self.url or self.filename
    if s is None: return object.__repr__(self)
    return f"{type(self).__name__}({s if isinstance(s, str) else truncstr(str(s), 30)})"

@patch
def __init__(self:TextDisplayObject, data=None, url=None, filename=None, metadata=None):
    if isinstance(data, PurePath): data = str(data)
    self.url,self.filename,self.data = url,filename,data
    if metadata is not None: self.metadata = metadata
    elif self.metadata is None: self.metadata = {}
    self.reload()
    self._check_data()


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
    "Publish each result when bound to a kernel; retain the last result for standalone capture."

    sender = None

    # ContextVars so concurrent callbacks cannot read or clobber an execution's result
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

    def write_output_prompt(self):
        self.last_execution_count = self.exec_result.execution_count if self.exec_result is not None else self.prompt_count

    def write_format_data(self, format_dict, md_dict=None):
        "Capture formatted output from displayhook."
        self.last = format_dict
        self.last_metadata = md_dict or {}
        if self.sender is not None:
            sys.stdout.flush()
            sys.stderr.flush()
            self.sender("execute_result", content=dict(data=format_dict, metadata=self.last_metadata, execution_count=self.last_execution_count))

    def finish_displayhook(self): self._is_active = False
