import asyncio, json, logging, os, sys, traceback

# Ensure debugpy avoids sys.monitoring mode, which can stall kernel threads.
os.environ.setdefault("PYDEVD_USE_SYS_MONITORING", "0")

from typing import Callable

import zmq
from fastcore.basics import str2bool
from IPython.core.async_helpers import _asyncio_runner
from IPython.core.application import BaseIPythonApplication
from IPython.core.completer import provisionalcompleter as _provisionalcompleter
from IPython.core.completer import rectify_completions as _rectify_completions
from IPython.core.interactiveshell import InteractiveShell
from IPython.core.shellapp import InteractiveShellApp

from ipymini_debug import Debugger, debug_cell_filename
from ipymini_term import IPythonCapture, io_state

_debug = os.environ.get("IPYMINI_DEBUG", "").lower() in ("1", "true", "yes")
_real_stderr = sys.__stderr__  # Use original stderr, not wrapped version


def _dbg(*args):
    if _debug: print("[bridge]", *args, file=_real_stderr, flush=True)


experimental_completions_key = "_jupyter_types_experimental"
log = logging.getLogger("ipymini.startup")
startup_done = False


def _maybe_json(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except json.JSONDecodeError: return {}
    return value


def _env_flag(name: str) -> bool | None:
    "Parse env var `name` to bool; return None if unset/invalid."
    raw = os.environ.get(name)
    if raw is None: return None
    try: return str2bool(raw)
    except TypeError: return None


class _MiniShellApp(BaseIPythonApplication, InteractiveShellApp):
    "Minimal IPython app for loading config/extensions/startup."

    name = "ipython-kernel"

    def __init__(self, shell, **kwargs):
        super().__init__(**kwargs)
        self.shell = shell

    def init_shell(self):
        if self.shell: self.shell.configurables.append(self)


def _init_ipython_app(shell):
    global startup_done
    if startup_done: return
    app = _MiniShellApp(shell)
    app.init_profile_dir()
    app.init_config_files()
    app.load_config_file()
    app.init_path()
    app.init_shell()
    app.init_extensions()
    app.init_code()
    startup_done = True


class KernelBridge:
    def __init__(self, request_input: Callable[[str, bool], str], debug_event_callback: Callable[[dict], None] | None = None,
        zmq_context: zmq.Context | None = None, *, user_ns: dict | None = None, use_singleton: bool = True):
        "Initialize IPython shell, IO capture, and debugger hooks."
        from IPython.core import page

        os.environ.setdefault("MPLBACKEND", "module://matplotlib_inline.backend_inline")
        io_state.install()
        if use_singleton: self.shell = InteractiveShell.instance(user_ns=user_ns)
        else: self.shell = InteractiveShell(user_ns=user_ns)
        use_jedi = _env_flag("IPYMINI_USE_JEDI")
        if use_jedi is not None: self.shell.Completer.use_jedi = use_jedi

        def _code_name(raw_code: str, transformed_code: str, number: int) -> str: return debug_cell_filename(raw_code)

        self.shell.compile.get_code_name = _code_name
        self.request_input = request_input
        self.capture = IPythonCapture(self.shell, request_input=request_input)
        self.current_exec_task = None

        hook = page.as_hook(page.display_page) if self.shell.display_page else page.as_hook(self._payloadpage_page)
        self.shell.set_hook("show_in_pager", hook, 99)
        self.shell._last_traceback = None

        def _showtraceback(etype, evalue, stb): self.shell._last_traceback = stb

        def _enable_gui(gui=None): self.shell.active_eventloop = gui

        def _set_next_input(text: str, replace: bool = False):
            payload = dict(source="set_next_input", text=text, replace=bool(replace))
            self.shell.payload_manager.write_payload(payload)

        self.shell._showtraceback = _showtraceback
        self.shell.enable_gui = _enable_gui
        self.shell.set_next_input = _set_next_input
        _init_ipython_app(self.shell)
        kernel_modules = [module.__file__ for module in sys.modules.values() if getattr(module, "__file__", None)]
        self.debugger = Debugger(debug_event_callback, zmq_context=zmq_context, kernel_modules=kernel_modules,
            debug_just_my_code=False, filter_internal_frames=True)

    def _payloadpage_page(self, strg, start: int = 0, screen_lines: int = 0, pager_cmd=None):
        start = max(0, start)
        data = strg if isinstance(strg, dict) else {"text/plain": strg}
        payload = dict(source="page", data=data, start=start)
        self.shell.payload_manager.write_payload(payload)

    async def _run_cell(self, code: str, silent: bool, store_history: bool):
        shell = self.shell
        if not (hasattr(shell, "run_cell_async") and hasattr(shell, "should_run_async")):
            _dbg("_run_cell: using sync run_cell")
            return shell.run_cell(code, store_history=store_history, silent=silent)
        try:
            transformed = shell.transform_cell(code)
            exc_tuple = None
        except Exception:
            transformed = code
            exc_tuple = sys.exc_info()
        try: loop_running = asyncio.get_running_loop().is_running()
        except RuntimeError: loop_running = False
        should_run_async = shell.should_run_async(code, transformed_cell=transformed, preprocessing_exc_tuple=exc_tuple)
        _dbg(f"_run_cell: loop_running={loop_running}, should_run_async={should_run_async}")
        if loop_running and _asyncio_runner and shell.loop_runner is _asyncio_runner and should_run_async:
            res = None
            coro = shell.run_cell_async(code, store_history=store_history, silent=silent,
                transformed_cell=transformed, preprocessing_exc_tuple=exc_tuple)
            task = asyncio.create_task(coro)
            self.current_exec_task = task
            _dbg("_run_cell: awaiting async task")
            try:
                try: res = await task
                except asyncio.CancelledError as exc: raise KeyboardInterrupt() from exc
            finally:
                self.current_exec_task = None
                shell.events.trigger("post_execute")
                if not silent: shell.events.trigger("post_run_cell", res)
            _dbg("_run_cell: async task done")
            return res
        return shell.run_cell(code, store_history=store_history, silent=silent)

    def _exc_to_error(self, exc: BaseException) -> dict:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return dict(ename=type(exc).__name__, evalue=str(exc), traceback=tb)

    async def execute(self, code: str, silent: bool = False, store_history: bool = True, user_expressions=None,
        allow_stdin: bool = False) -> dict:
        "Execute `code` in IPython and return captured outputs/errors (never raises)."
        _dbg(f"execute start: {code[:30]!r}...")
        self.capture.reset()
        result = None
        raised = None
        try:
            with self.capture.capture(allow_stdin=bool(allow_stdin), silent=silent):
                self.debugger.trace_current_thread()
                _dbg("execute: calling _run_cell")
                result = await self._run_cell(code, silent=silent, store_history=store_history)
                _dbg("execute: _run_cell done")
        except BaseException as exc:
            _dbg(f"execute: exception {type(exc).__name__}: {exc}")
            raised = exc

        payload = self.capture.consume_payload()

        if raised is not None:
            snapshot = self.capture.snapshot(execution_count=self.shell.execution_count)
            return dict(**snapshot, result=None, result_metadata={}, error=self._exc_to_error(raised),
                user_expressions={}, payload=payload)

        error = None
        err = getattr(result, "error_in_exec", None) or getattr(result, "error_before_exec", None)
        if err is not None: error = dict(ename=type(err).__name__, evalue=str(err), traceback=self.shell._last_traceback or [])

        if user_expressions is None: user_expressions = {}
        user_expressions = _maybe_json(user_expressions) or {}
        user_expr = self.shell.user_expressions(user_expressions) if error is None else {}

        exec_count = getattr(result, "execution_count", self.shell.execution_count)
        result_meta = self.shell.displayhook.last_metadata or {}
        snapshot = self.capture.snapshot(result=self.shell.displayhook.last, result_metadata=result_meta,
            execution_count=exec_count)
        return dict(**snapshot, error=error, user_expressions=user_expr, payload=payload)

    def set_stream_sender(self, sender: Callable[[str, str], None] | None): self.capture.set_stream_sender(sender)

    def set_display_sender(self, sender: Callable[[dict], None] | None):
        "Set live display sender; None to buffer display events."
        self.capture.set_display_sender(sender)

    def cancel_exec_task(self, loop: asyncio.AbstractEventLoop | None) -> bool:
        "Cancel the currently running async execution task, if any."
        task = self.current_exec_task
        if task is None or task.done() or loop is None: return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def complete(self, code: str, cursor_pos: int | None = None) -> dict:
        "Return completion matches for `code` at `cursor_pos`."
        if cursor_pos is None: cursor_pos = len(code)
        with _provisionalcompleter():
            completions = list(_rectify_completions(code, self.shell.Completer.completions(code, cursor_pos)))
        if completions:
            cursor_start = completions[0].start
            cursor_end = completions[0].end
            matches = [c.text for c in completions]
        else:
            cursor_start = cursor_pos
            cursor_end = cursor_pos
            matches = []
        exp = [dict(start=c.start, end=c.end, text=c.text, type=c.type, signature=c.signature) for c in completions]
        return dict(matches=matches, cursor_start=cursor_start, cursor_end=cursor_end,
            metadata={experimental_completions_key: exp}, status="ok")

    def inspect(self, code: str, cursor_pos: int | None = None, detail_level: int = 0) -> dict:
        "Return inspection data for `code` at `cursor_pos`."
        if cursor_pos is None: cursor_pos = len(code)
        from IPython.utils.tokenutil import token_at_cursor

        name = token_at_cursor(code, cursor_pos)
        if not name: return dict(status="ok", found=False, data={}, metadata={})
        bundle = self.shell.object_inspect_mime(name, detail_level=detail_level)
        if not self.shell.enable_html_pager: bundle.pop("text/html", None)
        return dict(status="ok", found=True, data=bundle, metadata={})

    def is_complete(self, code: str) -> dict:
        "Report completeness status and indentation for `code`."
        tm = getattr(self.shell, "input_transformer_manager", None)
        if tm is None: tm = self.shell.input_splitter
        status, indent_spaces = tm.check_complete(code)
        reply = {"status": status}
        if status == "incomplete": reply["indent"] = " " * indent_spaces
        return reply

    def history(self, hist_access_type: str, output: bool, raw: bool, session: int = 0, start: int = 0, stop=None,
        n=None, pattern=None, unique: bool = False) -> dict:
        "Return history entries based on `hist_access_type` query."
        if hist_access_type == "tail": hist = self.shell.history_manager.get_tail(n, raw=raw, output=output, include_latest=True)
        elif hist_access_type == "range": hist = self.shell.history_manager.get_range(session, start, stop, raw=raw, output=output)
        elif hist_access_type == "search": hist = self.shell.history_manager.search(pattern, raw=raw, output=output, n=n, unique=unique)
        else: hist = []
        return {"status": "ok", "history": list(hist)}

    def debug_request(self, request_json: str) -> dict:
        "Handle a debug_request DAP message in JSON."
        return self.debugger.process_request_json(request_json)
