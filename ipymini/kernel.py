import asyncio, contextvars, faulthandler, json, logging, os, signal, sys, threading, traceback, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from fastcore.basics import nested_idx, store_attr
from microio import ActorCore, CloseScope, ScopeGroup, ServiceGroup, WorkTracker
import zmq
from jupyter_client.session import Session
from .shell import MiniShell
from .comms import get_comm_manager
from .concur import _release as _unlock_release, _subshell as _subshell_var, unlock as _unlock, subshell as _subshell_context
from .debug import DebugFlags, setup_debug, trace_msg
from .zmqthread import AsyncRouterThread, HeartbeatThread, IOPubThread, StdinRouterThread

log = logging.getLogger("ipymini.kernel")
_debug_flags = DebugFlags.from_env("IPYMINI")
_debug = _debug_flags.enabled
_dbg_lock = threading.Lock()
def dbg(*args, **kw):
    if _debug:
        with _dbg_lock: print("[ipymini]", *args, **kw, file=sys.__stderr__, flush=True)

def _install_thread_excepthook(kernel: "MiniKernel"):
    prev = threading.excepthook
    def hook(args):
        try: prev(args)
        except Exception: log.exception("previous threading.excepthook failed")
        name = getattr(args.thread, "name", "")
        critical = name in {"iopub-thread", "stdin-router", "heartbeat-thread"} or name.endswith("-router")
        if not critical: return
        log.error("Critical thread crashed: %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        kernel.request_stop(f"critical thread crashed: {name}", failed=True)
    threading.excepthook = hook
    return prev
subshell_abort_clear = object()


@dataclass
class ConnectionInfo:
    transport:str
    ip:str
    shell_port:int
    iopub_port:int
    stdin_port:int
    control_port:int
    hb_port:int
    key:str
    signature_scheme:str

    @classmethod
    def from_file(cls, path:str)->"ConnectionInfo":
        "Load connection info from JSON connection file at `path`."
        with open(path, encoding="utf-8") as f: data = json.load(f)
        return cls(transport=data["transport"], ip=data["ip"], shell_port=int(data["shell_port"]),
            iopub_port=int(data["iopub_port"]), stdin_port=int(data["stdin_port"]), control_port=int(data["control_port"]),
            hb_port=int(data["hb_port"]), key=data.get("key", ""), signature_scheme=data.get("signature_scheme", "hmac-sha256"))

    def addr(self, port:int)->str: return f"{self.transport}://{self.ip}:{port}"


class ExecState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    ABORTED = "aborted"


class KernelState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


def _join_or_log(thread: threading.Thread|None, timeout:float = 1.0)->bool:
    if thread is None: return True
    thread.join(timeout=timeout)
    if not thread.is_alive(): return True
    log.error("Thread did not stop: %s", thread.name)
    return False

shell_required = dict(execute_request=("code",), complete_request=("code", "cursor_pos"),
    inspect_request=("code", "cursor_pos"), history_request=("hist_access_type",), is_complete_request=("code",))
shell_specs = dict(complete_request=("complete_reply", dict(code=("code", ""), cursor_pos="cursor_pos"), "complete"),
    inspect_request=("inspect_reply", dict(code=("code", ""), cursor_pos="cursor_pos", detail_level=("detail_level", 0)), "inspect"),
    is_complete_request=("is_complete_reply", dict(code=("code", "")), "is_complete"))
missing_defaults = dict(complete_request=dict(matches=[], cursor_start=0, cursor_end=0, metadata={}),
    inspect_request=dict(found=False, data={}, metadata={}), history_request={"history": []}, is_complete_request={"indent": ""})


def _reply_type(msg_type:str)->str: return msg_type.replace("_request", "_reply")

def _error_content(ename:str, evalue:str = "", tb: list|None = None)->dict: return dict(status="error", ename=ename, evalue=evalue, traceback=tb or [])

def _exc_content(exc: BaseException)->dict: return _error_content(type(exc).__name__, str(exc), traceback.format_exception(type(exc), exc, exc.__traceback__))


def _raise_async_exception(thread_id:int, exc_type: type[BaseException])->bool:
    "Inject `exc_type` into a thread by id; returns success."
    try: import ctypes
    except ImportError: return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(exc_type))
    if res == 0: return False
    if res > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        return False
    return True


def _env_float(name:str, default:float)->float:
    "Return float env var `name`, or `default` on missing/invalid."
    raw = os.environ.get(name)
    if raw is None: return default
    try: return float(raw)
    except ValueError: return log.warning("invalid %s=%r; using %s", name, raw, default) or default

class Subshell:
    def __init__(self, kernel: "MiniKernel", subshell_id:str|None, user_ns: dict,
        use_singleton: bool = False, run_in_thread: bool = True):
        "Create subshell worker thread and shell layer for execution."
        store_attr("kernel,subshell_id")
        self.user_ns = user_ns
        self.use_singleton = use_singleton
        self.scope = CloseScope()
        name = "subshell-parent" if subshell_id is None else f"subshell-{subshell_id}"
        self.thread = None if not run_in_thread else threading.Thread(target=self._run_loop, daemon=True, name=name)
        self.loop = None
        self.loop_ready = threading.Event()
        self.actor = ActorCore(self._handle_actor_item, concurrent=True)
        self.aborting = False
        self.abort_handle = None
        self._shell = None
        self.shell_ready = threading.Event()
        self.parent_header_var = contextvars.ContextVar("parent_header", default=None)
        self.parent_idents_var = contextvars.ContextVar("parent_idents", default=None)
        self.exec_tracker = WorkTracker()
        self.executing = self.exec_tracker.busy
        self.exec_scopes = ScopeGroup()
        self.sync_executing = threading.Event()
        self.last_exec_state = None
        self.state_lock = threading.Lock()
        self.shell_handlers = dict(kernel_info_request=self._handle_kernel_info, connect_request=self._handle_connect,
            complete_request=self._shell_handler, inspect_request=self._shell_handler, history_request=self._handle_history,
            is_complete_request=self._shell_handler, comm_info_request=self._handle_comm_info, comm_open=self._handle_comm,
            comm_msg=self._handle_comm, comm_close=self._handle_comm, shutdown_request=self.handle_shutdown)
        if not run_in_thread: self._init_shell()

    @property
    def shell(self)->MiniShell:
        if self._shell is None and self.thread is not None and threading.current_thread() is not self.thread: self.shell_ready.wait(timeout=5)
        if self._shell is None: raise RuntimeError("subshell shell is not initialized")
        return self._shell

    def _init_shell(self):
        if self._shell is not None: return
        self._shell = MiniShell(request_input=self.request_input, debug_event_callback=self.send_debug_event,
            zmq_context=self.kernel.context, user_ns=self.user_ns, use_singleton=self.use_singleton,
            exec_scopes=self.exec_scopes, sync_execution_context=self._sync_execution_context)
        self._shell.ipy.kernel = self.kernel
        self._shell.set_stream_sender(self._send_stream)
        self._shell.set_display_sender(self._send_display_event)
        self.shell_ready.set()

    def start(self, wait: bool = True):
        if self.thread is not None:
            self.thread.start()
            if wait and not self.loop_ready.wait(timeout=5): raise TimeoutError(f"{self.thread.name} did not become ready")

    def stop(self, interrupt: bool = False):
        "Signal subshell loop to stop and wake it."
        if interrupt: self.interrupt()
        if not self.scope.close("subshell stop"): return False
        self.actor.close()
        return True

    def join(self, timeout:float|None=None)->bool: return _join_or_log(self.thread, timeout=timeout)

    def submit(self, msg: dict, idents: list[bytes]|None)->bool:
        "Queue executes behind the cell baton; run other messages on the loop directly."
        if nested_idx(msg, "header", "msg_type") == "execute_request": return bool(self.actor.submit((msg, idents)))
        return self._submit_direct(msg, idents)

    def _submit_direct(self, msg: dict, idents: list[bytes]|None)->bool:
        "Handle a non-execute message promptly, without queueing behind busy cells."
        if self.scope.closed: return False
        loop = self.loop
        if loop is None: return bool(self.actor.submit((msg, idents)))  # loop not started yet; deliver via mailbox
        item = (msg, idents)
        def _run(): loop.create_task(self._handle_actor_item(item, lambda: None))
        try: loop.call_soon_threadsafe(_run)
        except RuntimeError: return False
        return True

    def _set_last_exec_state(self, state: ExecState):
        with self.state_lock: self.last_exec_state = state

    def interrupt(self)->bool:
        "Cancel running async executions, or raise KeyboardInterrupt in a sync one."
        if not self.executing.is_set(): return False
        already_cancelling = self.exec_scopes.cancelling
        if self.exec_scopes.cancel("interrupt", latch=True): return True
        if already_cancelling: return True  # still cancelling from a previous interrupt; don't double-inject
        if not self.sync_executing.is_set(): return True
        if self.thread is None: return False
        thread_id = self.thread.ident
        if thread_id is None: return False
        return _raise_async_exception(thread_id, KeyboardInterrupt)

    def cancel_async_execution(self, *, wake: bool = False)->bool:
        "Cancel all active async cell executions, if any."
        return self.exec_scopes.cancel("interrupt", wake=wake, latch=True)

    @contextmanager
    def _sync_execution_context(self):
        self.sync_executing.set()
        try:
            if self.exec_scopes.cancelling: raise KeyboardInterrupt
            yield
        finally: self.sync_executing.clear()

    def request_input(self, prompt:str, password: bool)->str:
        "Forward input_request through stdin router for this subshell."
        try:
            if sys.stdout is not None: sys.stdout.flush()
            if sys.stderr is not None: sys.stderr.flush()
        except (OSError, ValueError): pass
        parent = self.parent_header_var.get()
        idents = self.parent_idents_var.get()
        return self.kernel.stdin_router.request_input(prompt, password, parent, idents)

    def _send_stream(self, name:str, text:str):
        parent = self.parent_header_var.get()
        if not parent: return
        self.kernel.iopub.stream(parent, name=name, text=text)

    def _send_display_event(self, event: dict):
        parent = self.parent_header_var.get()
        if not parent: return
        if event.get("type") == "clear_output":
            self.kernel.iopub.clear_output(parent, wait=event.get("wait", False))
            return
        data = event.get("data", {})
        metadata = event.get("metadata", {})
        transient = event.get("transient", {})
        buffers = event.get("buffers")
        payload = dict(data=data, metadata=metadata, transient=transient)
        if event.get("update"): self.kernel.iopub.update_display_data(parent, payload, buffers=buffers)
        else: self.kernel.iopub.display_data(parent, payload, buffers=buffers)

    def send_debug_event(self, event: dict):
        "Send debug_event on IOPub for current parent message."
        parent = self.parent_header_var.get() or {}
        self.kernel.iopub.debug_event(parent, content=event)

    def send_status(self, state:str, parent: dict|None): self.kernel.iopub.status(parent, execution_state=state)

    def send_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        parent_id = (nested_idx(parent, "header", "msg_id") or "?")[:8]
        dbg(f"REPLY {msg_type} id={parent_id}")
        self.kernel.queue_shell_reply(msg_type, content, parent, idents)

    def _run_loop(self): self._run_loop_body()

    def run_main(self):
        "Run parent subshell loop in the main thread."
        self._run_loop_body()

    def _run_loop_body(self):
        try:
            with asyncio.Runner() as runner:
                self.loop = runner.get_loop()
                runner.run(self._main())
        finally:
            self.loop = None
            self.loop_ready.clear()
            asyncio.set_event_loop(None)

    async def _main(self):
        loop = asyncio.get_running_loop()
        self.loop = loop
        self.actor.bind(loop)
        self._init_shell()
        self.loop_ready.set()
        dbg(f"SUBSHELL started id={self.subshell_id}")
        await self.actor.run(bind=False)

    async def _handle_actor_item(self, item, release):
        msg, idents = item
        if msg is subshell_abort_clear:
            self._stop_aborting()
            return
        if not msg: return
        msg_type = nested_idx(msg, "header", "msg_type") or "?"
        msg_id = (nested_idx(msg, "header", "msg_id") or "?")[:8]
        if self.scope.closed and msg_type == "execute_request": return self._send_abort_reply(msg, idents)  # don't run queued cells while stopping
        dbg(f"EXEC {msg_type} id={msg_id}")
        try: await self._handle_message(msg, idents, release)
        except Exception as exc: self._handle_internal_error(msg, idents, exc)
        dbg(f"DONE {msg_type} id={msg_id}")

    def _handle_internal_error(self, msg: dict, idents: list[bytes]|None, exc: Exception):
        msg_type = nested_idx(msg, "header", "msg_type") or ""
        log.warning("Internal error in %s handler", msg_type, exc_info=exc)
        self._send_error_reply(msg_type, _exc_content(exc), msg, idents)

    def _execute_reply_defaults(self, execution_count: int | None = None)->dict:
        return dict(execution_count=execution_count if execution_count is not None else self.shell.ipy.execution_count, user_expressions={}, payload=[])

    def _send_error_reply(self, msg_type:str, error:dict, msg: dict, idents: list[bytes]|None):
        if not msg_type.endswith("_request"): return
        reply = dict(error)
        if msg_type == "execute_request":
            reply |= self._execute_reply_defaults()
            self.kernel.send_status("busy", msg)
            self.kernel.iopub.error(msg, ename=error["ename"], evalue=error["evalue"], traceback=error.get("traceback", []))
        else: reply |= missing_defaults.get(msg_type, {})
        self.send_reply(_reply_type(msg_type), reply, msg, idents)
        if msg_type == "execute_request": self.kernel.send_status("idle", msg)

    async def _handle_message(self, msg: dict, idents: list[bytes]|None, release):
        msg_type = msg["header"]["msg_type"]
        msg_id = msg["header"].get("msg_id", "?")[:8]
        dbg(f"HANDLE_MSG {msg_type} id={msg_id}")
        token_header = self.parent_header_var.set(msg)
        token_idents = self.parent_idents_var.set(idents)
        try:
            missing = self._missing_fields(msg_type, msg.get("content", {}))
            if missing:
                self._send_missing_fields_reply(msg_type, missing, msg, idents)
                return
            if msg_type == "execute_request" and self.aborting:
                dbg(f"ABORTING id={msg_id}")
                self._send_abort_reply(msg, idents)
                return
            if msg_type == "execute_request":
                dbg(f"DISPATCH_EXEC id={msg_id}")
                await self._handle_execute(msg, idents, release)
                return
            self._dispatch_shell_non_execute(msg, idents)
        finally:
            self.parent_header_var.reset(token_header)
            self.parent_idents_var.reset(token_idents)

    def _missing_fields(self, msg_type:str, content: dict)->list[str]:
        required = shell_required.get(msg_type)
        return [key for key in required if key not in content] if required else []

    def _send_missing_fields_reply(self, msg_type:str, missing: list[str], msg: dict, idents: list[bytes]|None):
        evalue = f"missing required fields: {', '.join(missing)}"
        self._send_error_reply(msg_type, _error_content("MissingField", evalue), msg, idents)

    def _send_abort_reply(self, msg: dict, idents: list[bytes]|None):
        self.kernel.send_status("busy", msg)
        self._set_last_exec_state(ExecState.ABORTED)
        reply_content = dict(status="aborted") | self._execute_reply_defaults()
        self.send_reply("execute_reply", reply_content, msg, idents)
        self.kernel.send_status("idle", msg)

    def _start_aborting(self):
        self.aborting = True
        if self.abort_handle is not None:
            self.abort_handle.cancel()
            self.abort_handle = None
        timeout = self.kernel.stop_on_error_timeout
        if timeout > 0: self.abort_handle = self.loop.call_later(timeout, self._stop_aborting)

    def _stop_aborting(self):
        self.aborting = False
        if self.abort_handle is not None:
            self.abort_handle.cancel()
            self.abort_handle = None

    def _dispatch_shell_non_execute(self, msg: dict, idents: list[bytes]|None):
        msg_type = msg["header"]["msg_type"]
        handler = self.shell_handlers.get(msg_type)
        if handler is None:
            self.send_reply(_reply_type(msg_type), {}, msg, idents)
            return
        handler(msg, idents)

    def _abort_pending_executes(self, append_abort_clear: bool = False):
        drained = self.actor.drain_nowait()
        if append_abort_clear: self.actor.submit((subshell_abort_clear, None))
        for msg, idents in drained:
            if msg is subshell_abort_clear:
                self.actor.submit((msg, idents))
                continue
            msg_type = nested_idx(msg, "header", "msg_type") or None
            if msg_type == "execute_request": self._send_abort_reply(msg, idents)
            elif msg_type: self._dispatch_shell_non_execute(msg, idents)

    def _handle_kernel_info(self, msg: dict, idents: list[bytes]|None):
        with self.kernel.busy_idle(msg):
            content = self.kernel.kernel_info_content()
            self.send_reply("kernel_info_reply", content, msg, idents)

    def _handle_connect(self, msg: dict, idents: list[bytes]|None):
        content = dict(shell_port=self.kernel.connection.shell_port, iopub_port=self.kernel.connection.iopub_port,
            stdin_port=self.kernel.connection.stdin_port, control_port=self.kernel.connection.control_port, hb_port=self.kernel.connection.hb_port)
        self.send_reply("connect_reply", content, msg, idents)

    def _safe_release(self, release):
        "Wrap an actor release callback so it is safe to call from any thread."
        loop = asyncio.get_running_loop()
        def _do():
            try: running = asyncio.get_running_loop()
            except RuntimeError: running = None
            if running is loop: release()
            else: loop.call_soon_threadsafe(release)
        return _do

    async def _handle_execute(self, msg: dict, idents: list[bytes]|None, release):
        msg_id = (nested_idx(msg, "header", "msg_id") or "?")[:8]
        content = msg.get("content", {})
        code = content.get("code", "")
        silent = bool(content.get("silent", False))
        store_history = bool(content.get("store_history", True))
        stop_on_error = bool(content.get("stop_on_error", True))
        user_expressions = content.get("user_expressions", {})
        allow_stdin = bool(content.get("allow_stdin", False))

        dbg(f"HANDLE_EXEC id={msg_id} code={code[:30]!r}...")
        _unlock_release.set(self._safe_release(release))
        _subshell_var.set(self)
        self.exec_scopes.clear()  # a new execute ends any previous cancelling window
        self.exec_tracker.add()
        terminal_state = ExecState.COMPLETED
        iopub = self.kernel.iopub
        sent_reply = sent_error = False
        exec_count = None

        self.kernel.send_status("busy", msg)
        try:
            exec_count_pre = self.shell.ipy.execution_count
            if not silent: iopub.execute_input(msg, code=code, execution_count=exec_count_pre)

            dbg(f"BRIDGE_EXEC id={msg_id}")
            timeout_handle = None
            if _debug:
                loop = asyncio.get_running_loop()
                timeout_handle = loop.call_later(2.0, lambda: log.warning("execute still running msg_id=%s", msg_id))
            try:
                with self.shell.execution_context(allow_stdin=allow_stdin, silent=silent, comm_sender=iopub.send, parent=msg):
                    result = await self.shell.execute(code, silent=silent, store_history=store_history,
                        user_expressions=user_expressions, allow_stdin=allow_stdin)
            finally:
                if timeout_handle: timeout_handle.cancel()
            dbg(f"BRIDGE_DONE id={msg_id}")

            error = result.get("error")
            if error and error.get("ename") == "CancelledError" and self.exec_scopes.cancelling:
                error = dict(error) | dict(ename="KeyboardInterrupt", evalue="")
            exec_count = result.get("execution_count")

            if error:
                if error.get("ename") == "KeyboardInterrupt": terminal_state = ExecState.ABORTED
                iopub.error(msg, ename=error["ename"], evalue=error["evalue"], traceback=error.get("traceback", []))
                sent_error = True
            elif not silent and result.get("result") is not None:
                iopub.execute_result(msg, dict(execution_count=exec_count, data=result["result"], metadata=result.get("result_metadata", {})))

            reply = dict(status="error" if error else "ok", execution_count=exec_count,
                user_expressions=result.get("user_expressions", {}), payload=result.get("payload", []))
            if error: reply.update(error)
            self.send_reply("execute_reply", reply, msg, idents)
            sent_reply = True

            if error and stop_on_error:
                self._abort_pending_executes(append_abort_clear=self.kernel.stop_on_error_timeout <= 0)
                self._start_aborting()
        except KeyboardInterrupt as exc:
            terminal_state = ExecState.ABORTED
            error = _exc_content(exc)
            if not sent_error: iopub.error(msg, ename=error["ename"], evalue=error["evalue"], traceback=error.get("traceback", []))
            if not sent_reply:
                reply = dict(error) | self._execute_reply_defaults(exec_count)
                self.send_reply("execute_reply", reply, msg, idents)
                if stop_on_error:
                    self._abort_pending_executes(append_abort_clear=self.kernel.stop_on_error_timeout <= 0)
                    self._start_aborting()
        finally:
            self.kernel.send_status("idle", msg)
            self._set_last_exec_state(terminal_state)
            self.exec_tracker.done()

    def _shell_handler(self, msg: dict, idents: list[bytes]|None):
        msg_type = nested_idx(msg, "header", "msg_type") or None
        spec = shell_specs.get(msg_type)
        if spec is None: return
        reply_type, fields, method = spec
        self._shell_reply(msg, idents, reply_type, method, **fields)

    def _handle_history(self, msg: dict, idents: list[bytes]|None):
        content = msg.get("content", {})
        reply = self.shell.history(content.get("hist_access_type", ""), bool(content.get("output", False)),
            bool(content.get("raw", False)), session=int(content.get("session", 0)), start=int(content.get("start", 0)),
            stop=content.get("stop"), n=content.get("n"), pattern=content.get("pattern"), unique=bool(content.get("unique", False)))
        self.send_reply("history_reply", reply, msg, idents)

    def _shell_reply(self, msg: dict, idents: list[bytes]|None, reply_type:str, method:str, **fields):
        content = msg.get("content", {})
        args = {}
        for name, spec in fields.items():
            if isinstance(spec, tuple): key, default = spec
            else: key, default = spec, None
            args[name] = content.get(key, default)
        reply = getattr(self.shell, method)(**args)
        self.send_reply(reply_type, reply, msg, idents)

    def _handle_comm_info(self, msg: dict, idents: list[bytes]|None):
        content = msg.get("content", {})
        target_name = content.get("target_name")
        manager = get_comm_manager()
        comms = {comm_id: dict(target_name=comm.target_name) for comm_id, comm in manager.comms.items()
            if target_name is None or comm.target_name == target_name}
        reply = dict(status="ok", comms=comms)
        self.send_reply("comm_info_reply", reply, msg, idents)

    def _handle_comm(self, msg: dict, idents: list[bytes]|None):
        msg_type = msg["header"]["msg_type"]
        manager = get_comm_manager()
        getattr(manager, msg_type)(None, None, msg)
        self.kernel.iopub.send(msg_type, msg, msg.get("content", {}), metadata=msg.get("metadata"), buffers=msg.get("buffers"))

    def handle_shutdown(self, msg: dict, idents: list[bytes]|None): self.kernel.handle_shutdown(msg, idents, channel="shell")


class SubshellManager:
    def __init__(self, kernel: "MiniKernel"):
        "Manage parent and child subshells sharing a user namespace."
        self.kernel = kernel
        self.user_ns = {}
        self.parent = Subshell(kernel, None, self.user_ns, use_singleton=True, run_in_thread=False)
        self.subs = {}
        self.lock = threading.Lock()
        self.route_override = None  # (subshell_id, session) routing untagged executes from that session

    def start(self): return

    def get(self, subshell_id:str|None)->Subshell|None:
        "Return subshell by id, or the parent when None."
        if subshell_id is None: return self.parent
        with self.lock: return self.subs.get(subshell_id)

    def create(self, wait: bool = True)->str:
        "Create and start a new subshell; return its id."
        subshell_id = str(uuid.uuid4())
        subshell = Subshell(self.kernel, subshell_id, self.user_ns)
        subshell.start(wait=wait)
        with self.lock: self.subs[subshell_id] = subshell
        return subshell_id

    def list(self)->list[str]:
        with self.lock: return list(self.subs.keys())

    def set_route_override(self, subshell_id:str, session:str):
        "Route untagged execute_requests from `session` to `subshell_id` until cleared."
        with self.lock:
            if self.route_override is not None: raise RuntimeError("subshell route override already active")
            self.route_override = (subshell_id, session)

    def clear_route_override(self):
        with self.lock: self.route_override = None

    def route_for(self, msg: dict)->str|None:
        "Subshell id an untagged message should be redirected to, or None (called from the router thread)."
        override = self.route_override
        if override is None: return None
        subshell_id, session = override
        if not session or nested_idx(msg, "header", "session") != session: return None
        if nested_idx(msg, "header", "msg_type") != "execute_request": return None
        return subshell_id

    def delete(self, subshell_id:str):
        "Stop and remove a subshell by id."
        with self.lock: subshell = self.subs.pop(subshell_id)
        subshell.stop(interrupt=True)
        if not subshell.join(timeout=2): raise RuntimeError(f"subshell {subshell_id} did not stop")

    def stop_all(self):
        "Stop all subshells and the parent."
        with self.lock:
            subshells = list(self.subs.items())
            self.subs.clear()
        for subshell_id, subshell in subshells:
            subshell.stop(interrupt=True)
            if not _join_or_log(subshell.thread, timeout=1): log.error("Subshell %s still alive during shutdown", subshell_id)
        self.parent.stop()
        self.parent.join(timeout=1)

    def interrupt_all(self):
        "Send interrupts to all subshells."
        self.parent.interrupt()
        with self.lock: subshells = list(self.subs.values())
        for subshell in subshells: subshell.interrupt()


class IOPubCommand:
    def __init__(self, kernel: "MiniKernel"):
        "Proxy iopub_send by attribute name."
        self.kernel = kernel

    def send(self, msg_type:str, parent: dict|None, content: dict|None=None, metadata: dict|None=None,
        ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None, **kwargs):
        "Send an IOPub message with an explicit msg_type."
        if msg_type.startswith("_"): raise AttributeError(msg_type)
        self.kernel.iopub_send(msg_type, parent, content, metadata, ident, buffers, **kwargs)

    def __getattr__(self, name:str):
        "Return a callable that sends the named IOPub message type."
        if name.startswith('_'): raise AttributeError(name)
        def _send(parent: dict|None, content: dict|None=None, metadata: dict|None=None,
            ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None, **kwargs):
            self.kernel.iopub_send(name, parent, content, metadata, ident, buffers, **kwargs)

        _send.__name__ = name
        return _send


def _isolate_process_group()->bool:
    "Make this POSIX kernel process its own process-group leader."
    if os.name == "nt": return False
    try:
        if os.getpgrp() != os.getpid(): os.setpgrp()
        return os.getpgrp() == os.getpid()
    except OSError: return log.warning("could not isolate kernel process group", exc_info=True) or False


class MiniKernel:
    def __init__(self, connection_file:str, *, terminate_process_group: bool = False):
        "Initialize kernel sockets, threads, and subshell manager."
        self.terminate_process_group = terminate_process_group
        self.connection = ConnectionInfo.from_file(connection_file)
        key = self.connection.key.encode()
        self.session = Session(key=key, signature_scheme=self.connection.signature_scheme)
        self.context = zmq.Context.instance()
        iopub_qmax = int(os.environ.get("IPYMINI_IOPUB_QMAX", "10000"))
        iopub_sndhwm = os.environ.get("IPYMINI_IOPUB_SNDHWM")
        try: iopub_sndhwm = int(iopub_sndhwm) if iopub_sndhwm else None
        except ValueError: iopub_sndhwm = None
        self.iopub_thread = IOPubThread(self.context, self.connection.addr(self.connection.iopub_port), self.session,
            qmax=iopub_qmax, sndhwm=iopub_sndhwm)
        self.stdin_router = StdinRouterThread(self.context, self.connection.addr(self.connection.stdin_port), self.session)

        self.hb = HeartbeatThread(self.context, self.connection.addr(self.connection.hb_port))
        self.subshells = SubshellManager(self)
        self.shell = self.subshells.parent.shell
        self.parent_header = None
        self.parent_idents = None
        self.shell_router = None
        self.control_router = None
        self.state = KernelState.STARTING
        self.state_lock = threading.Lock()
        self.stop_scope = CloseScope()
        self.shutdown_restart = None
        self.stop_on_error_timeout = _env_float("IPYMINI_STOP_ON_ERROR_TIMEOUT", 0.0)
        self.iopub_cmd = None
        self.control_handlers = dict(shutdown_request=self.handle_shutdown, debug_request=self.handle_debug,
            interrupt_request=self.handle_interrupt)
        self.control_specs = dict(kernel_info_request=("kernel_info_reply", lambda msg: self.kernel_info_content()),
            create_subshell_request=("create_subshell_reply", lambda msg: dict(subshell_id=self.subshells.create())),
            list_subshell_request=("list_subshell_reply", lambda msg: dict(subshell_id=self.subshells.list())),
            delete_subshell_request=("delete_subshell_reply", self._delete_subshell))

    def _set_state(self, state: KernelState):
        with self.state_lock: self.state = state

    def unlock(self)->bool:
        "Let queued shell messages run while the current cell awaits."
        return _unlock()

    def subshell(self):
        "Run same-session execute_requests in a temporary subshell while the current cell awaits."
        return _subshell_context()

    def request_stop(self, reason: str, *, interrupt: bool = True, failed: bool = False)->bool:
        "Transition toward shutdown once and wake blocked work."
        with self.state_lock:
            if self.stop_scope.closed or self.state in {KernelState.STOPPED, KernelState.FAILED}: return False
            if failed: self.stop_scope.fail(RuntimeError(reason), reason=reason)
            else: self.stop_scope.close(reason)
            self.state = KernelState.FAILED if failed else KernelState.STOPPING
        log.info("kernel stopping: %s", reason)
        self.stdin_router.interrupt_pending()
        if interrupt:
            self.subshells.interrupt_all()
            self.send_interrupt_signal()
        self.subshells.parent.stop()
        return True

    def start(self):
        "Start kernel threads and serve shell/control messages."
        setup_debug(_debug_flags)
        dbg("kernel starting...")
        self._set_state(KernelState.STARTING)
        prev_hook = _install_thread_excepthook(self)
        prev_sigint = signal.getsignal(signal.SIGINT)
        sigusr1_registered = False
        try:
            self.subshells.start()
            ServiceGroup(self.iopub_thread, self.stdin_router, self.hb).start().wait_started()
            signal.signal(signal.SIGINT, self.handle_sigint)
            if hasattr(signal, "SIGUSR1"):
                try:
                    faulthandler.register(signal.SIGUSR1, file=sys.__stderr__, all_threads=True)
                    sigusr1_registered = True
                except (OSError, RuntimeError, ValueError): log.warning("could not register SIGUSR1 dump", exc_info=True)
            self.shell_router = AsyncRouterThread(context=self.context, session=self.session,
                bind_addr=self.connection.addr(self.connection.shell_port), handler=self.handle_shell_msg, log_label="shell")
            self.control_router = AsyncRouterThread(context=self.context, session=self.session,
                bind_addr=self.connection.addr(self.connection.control_port), handler=self.handle_control_msg, log_label="control")
            dbg("waiting for routers to be ready...")
            ServiceGroup(self.shell_router, self.control_router).start().wait_started()
            dbg("kernel ready")
            self._set_state(KernelState.RUNNING)
            self.subshells.parent.run_main()
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                self.request_stop("kernel finalizer", interrupt=False, failed=failed)
                threading.excepthook = prev_hook
                ServiceGroup(self.shell_router, self.control_router).stop_join(timeout=1)
                ServiceGroup(self.hb).stop_join(timeout=1)
                self.subshells.stop_all()
                ServiceGroup(self.stdin_router, self.iopub_thread).stop_join(timeout=1)
                signal.signal(signal.SIGINT, prev_sigint)
                if sigusr1_registered: faulthandler.unregister(signal.SIGUSR1)
                with self.state_lock:
                    if self.state != KernelState.FAILED: self.state = KernelState.STOPPED
                    failed = self.state == KernelState.FAILED
            finally:
                if os.name == "nt": os._exit(1 if failed else 0)
                self._terminate_process_group_last()

    def _terminate_process_group_last(self):
        "Terminate the kernel-owned process group as the last shutdown action."
        if not self.terminate_process_group or os.name == "nt": return
        try:
            pid, pgid = os.getpid(), os.getpgrp()
            if pgid != pid: return log.warning("kernel is not process-group leader; skipping process-group termination")
        except OSError: return log.warning("could not inspect process group during shutdown", exc_info=True)
        prev_term = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.2)
        except OSError: log.warning("process-group SIGTERM failed", exc_info=True)
        finally: signal.signal(signal.SIGTERM, prev_term)
        os.killpg(pgid, signal.SIGKILL)

    def handle_sigint(self, signum, frame):
        "Handle SIGINT by interrupting subshells and input waits."
        with self.subshells.lock: children = list(self.subshells.subs.values())
        for subshell in children: subshell.interrupt()
        parent = self.subshells.parent
        if not parent.executing.is_set(): return
        if parent.cancel_async_execution(wake=True): return
        if not parent.sync_executing.is_set(): return
        raise KeyboardInterrupt

    def handle_control_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle control channel request message."
        msg_type = msg["header"]["msg_type"]
        trace_msg(log, "control recv", msg, enabled=_debug_flags.trace_msgs)
        if msg_type != "shutdown_request" and (content := self._stopping_control_error(msg_type)):
            return self._queue_control_result(msg_type, content, msg, idents)
        spec = self.control_specs.get(msg_type)
        if spec is not None: return self._dispatch_control_spec(msg_type, spec, msg, idents)
        handler = self.control_handlers.get(msg_type)
        if handler is None: return self._queue_control_result(msg_type, {}, msg, idents)
        handler(msg, idents)

    def _queue_control_result(self, msg_type:str, content: dict, msg: dict, idents: list[bytes]|None):
        self.queue_control_reply(_reply_type(msg_type), content, msg, idents)

    def _dispatch_control_spec(self, msg_type:str, spec, msg: dict, idents: list[bytes]|None):
        reply_type, fn = spec
        try: content = dict(status="ok") | (fn(msg) or {})
        except Exception as exc:
            log.warning("control handler failed: %s", msg_type, exc_info=True)
            content = _exc_content(exc)
        self.queue_control_reply(reply_type, content, msg, idents)

    def handle_shell_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle shell request message and dispatch to subshells."
        msg_type = nested_idx(msg, "header", "msg_type") or "?"
        msg_id = (nested_idx(msg, "header", "msg_id") or "?")[:8]
        trace_msg(log, "shell recv", msg, enabled=_debug_flags.trace_msgs)
        subshell_id = nested_idx(msg, "header", "subshell_id") or None  # treat "" as None
        if subshell_id is None: subshell_id = self.subshells.route_for(msg)
        subshell = self.subshells.get(subshell_id)
        if subshell is None:
            dbg(f"DISPATCH {msg_type} id={msg_id} -> NO SUBSHELL")
            self.send_subshell_error(msg, idents)
            return
        dbg(f"DISPATCH {msg_type} id={msg_id}")
        if not subshell.submit(msg, idents): self.send_subshell_error(msg, idents, f"Subshell {subshell_id!r} is not accepting requests")

    def queue_shell_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None)->int|None:
        "Enqueue a shell reply for async send."
        if self.shell_router is None:
            dbg("QUEUE shell reply - NO ROUTER!")
            return
        trace_msg(log, "shell reply", parent, enabled=_debug_flags.trace_msgs)
        return self.shell_router.enqueue((msg_type, content, parent, idents))

    def queue_control_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None)->int|None:
        "Enqueue a control reply for async send."
        if self.control_router is None: return
        return self.control_router.enqueue((msg_type, content, parent, idents))

    def send_status(self, state:str, parent: dict|None):
        if _debug_flags.trace_msgs and parent: trace_msg(log, f"iopub status={state}", parent, enabled=True)
        self.iopub.status(parent, execution_state=state)

    @contextmanager
    def busy_idle(self, parent: dict|None):
        "Send busy before work and idle after."
        self.send_status("busy", parent)
        try: yield
        finally: self.send_status("idle", parent)

    def send_debug_event(self, event: dict):
        "Send a debug_event on IOPub using current parent header."
        parent = self.parent_header or {}
        self.iopub.debug_event(parent, content=event)

    @property
    def iopub(self)->IOPubCommand:
        "Return cached IOPubCommand wrapper."
        if (proxy := self.iopub_cmd) is None: self.iopub_cmd = proxy = IOPubCommand(self)
        return proxy

    def iopub_send(self, msg_type:str, parent: dict|None, content: dict|None=None, metadata: dict|None=None,
        ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None, **kwargs):
        "Queue an IOPub message with optional metadata and buffers."
        if kwargs: content = dict(content or {}) | kwargs
        if content is None: content = {}
        self.iopub_thread.send(msg_type, content, parent, metadata, ident, buffers)

    def send_subshell_error(self, msg: dict, idents: list[bytes]|None, evalue: str | None = None):
        "Send SubshellNotFound error reply for unknown subshell. Always sends busy/idle for execute_request."
        msg_type = nested_idx(msg, "header", "msg_type") or ""
        subshell_id = nested_idx(msg, "header", "subshell_id")
        if not msg_type.endswith("_request"): return
        content = _error_content("SubshellNotFound", evalue or f"Unknown subshell_id {subshell_id!r}")
        if msg_type == "execute_request":
            content.update(dict(execution_count=0, user_expressions={}, payload=[]))
            self.send_status("busy", msg)
            self.iopub.error(msg, ename="SubshellNotFound", evalue=content["evalue"], traceback=[])
        self.queue_shell_reply(_reply_type(msg_type), content, msg, idents)
        if msg_type == "execute_request": self.send_status("idle", msg)

    def _delete_subshell(self, msg: dict)->dict:
        subshell_id = nested_idx(msg, "content", "subshell_id")
        if not isinstance(subshell_id, str): raise ValueError("subshell_id required")
        self.subshells.delete(subshell_id)
        return {}

    def kernel_info_content(self)->dict:
        "Build kernel_info_reply content."
        try: impl_version = version("ipymini")
        except PackageNotFoundError: impl_version = "0.0.0+local"
        supported_features = ["kernel subshells", "debugger"]
        return dict(status="ok", protocol_version="5.3", implementation="ipymini", implementation_version=impl_version,
            language_info=dict(name="python", version=self.python_version(), mimetype="text/x-python",
                file_extension=".py", pygments_lexer="python", codemirror_mode={"name": "ipython", "version": 3},
                nbconvert_exporter="python"), banner="ipymini", help_links=[], supported_features=supported_features, debugger=True)

    def python_version(self)->str: return ".".join(str(x) for x in os.sys.version_info[:3])

    def handle_debug(self, msg: dict, idents: list[bytes]|None):
        "Handle debug_request via MiniShell and emit events."
        self.send_status("busy", msg)
        self.parent_header = msg
        try:
            content = msg.get("content", {})
            reply = self.shell.debug_request(json.dumps(content))
            response = reply.get("response", {})
            events = reply.get("events", [])
            self.queue_control_reply("debug_reply", response, msg, idents)
            for event in events: self.send_debug_event(event)
            self.send_status("idle", msg)
        finally: self.parent_header = None

    def send_interrupt_signal(self):
        "Send SIGINT to the current process or process group."
        if os.name == "nt": return log.warning("Interrupt request not supported on Windows")
        pid = os.getpid()
        try: pgid = os.getpgid(pid)
        except OSError: pgid = None
        try:
            # Only signal the process group if we're the leader; otherwise avoid killing unrelated processes.
            if pgid and pgid == pid and hasattr(os, "killpg"): os.killpg(pgid, signal.SIGINT)
            else: os.kill(pid, signal.SIGINT)
        except OSError as err: log.warning("Interrupt signal failed: %s", err)

    def handle_interrupt(self, msg: dict, idents: list[bytes]|None):
        "Handle interrupt_request by signaling subshells."
        if content := self._stopping_control_error("interrupt_request"):
            self.queue_control_reply("interrupt_reply", content, msg, idents)
            return
        self.send_interrupt_signal()
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()
        self.queue_control_reply("interrupt_reply", {"status": "ok"}, msg, idents)

    def _stopping_control_error(self, msg_type:str)->dict|None:
        with self.state_lock: state = self.state
        if state not in {KernelState.STOPPING, KernelState.STOPPED, KernelState.FAILED}: return None
        return dict(status="error", ename="KernelStopping", evalue=f"kernel is {state.value}; cannot handle {msg_type}", traceback=[])

    def _commit_shutdown(self, requested_restart: bool)->tuple[dict, bool]:
        "Record one shutdown/restart decision and say whether this request should start stopping."
        with self.state_lock:
            first = self.shutdown_restart is None
            if first: self.shutdown_restart = requested_restart
            reply = {"status": "ok", "restart": self.shutdown_restart}
            start_stop = first and not self.stop_scope.closed and self.state not in {KernelState.STOPPED, KernelState.FAILED}
            if start_stop: self.state = KernelState.STOPPING
        return reply, start_stop

    def handle_shutdown(self, msg: dict, idents: list[bytes]|None, channel: str = "control"):
        "Handle shutdown_request and stop main loop."
        content = msg.get("content", {})
        reply, start_stop = self._commit_shutdown(bool(content.get("restart", False)))
        if channel == "shell":
            target = self.queue_shell_reply("shutdown_reply", reply, msg, idents)
            router = self.shell_router
        else:
            target = self.queue_control_reply("shutdown_reply", reply, msg, idents)
            router = self.control_router

        def _stop_after_reply():
            if target is not None and router is not None: router.wait_for_sent(target, timeout=1.0)
            self.request_stop("shutdown_request", interrupt=True)

        if start_stop: threading.Thread(target=_stop_after_reply, daemon=True, name="shutdown-waiter").start()


def run_kernel(connection_file:str):
    "Run kernel given a connection file path."
    signal.signal(signal.SIGINT, signal.default_int_handler)
    kernel = MiniKernel(connection_file, terminate_process_group=_isolate_process_group())
    kernel.start()
