import asyncio, contextvars, json, logging, os, queue, signal, sys, threading, traceback, time, uuid
from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from fastcore.basics import store_attr
import zmq, zmq.asyncio
from jupyter_client.session import Session
from .bridge import KernelBridge
from .comms import comm_context, get_comm_manager

_LOG = logging.getLogger("ipymini.stdin")
_SUBSHELL_STOP = object()
_SUBSHELL_ABORT_CLEAR = object()

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
    except ValueError: return default

class HeartbeatThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr:str):
        "Initialize heartbeat thread bound to `addr`."
        super().__init__(daemon=True)
        store_attr()
        self._stop_event = threading.Event()

    def run(self):
        "Echo heartbeat requests on REP socket until stopped."
        sock = self.context.socket(zmq.REP)
        sock.linger = 0
        sock.bind(self.addr)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop_event.is_set():
                events = dict(poller.poll(100))
                if sock in events and events[sock] & zmq.POLLIN:
                    msg = sock.recv()
                    sock.send(msg)
        finally: sock.close(0)

    def stop(self): self._stop_event.set()


class IOPubThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr:str, session: Session):
        "Initialize IOPub sender thread."
        super().__init__(daemon=True)
        store_attr()
        self.queue = queue.Queue()
        self._stop_event = threading.Event()
        self._socket = None

    def send(self, msg_type:str, content: dict, parent: dict|None, metadata: dict|None=None,
        ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None):
        "Queue an IOPub message for async send."
        self.queue.put((msg_type, content, parent, metadata, ident, buffers))

    def run(self):
        "Send queued IOPub messages and handle subscription handshake."
        sock = self.context.socket(zmq.XPUB)
        sock.linger = 0
        sock.setsockopt(zmq.XPUB_VERBOSE, 1)
        sock.bind(self.addr)
        self._socket = sock
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop_event.is_set():
                self._drain_queue()
                events = dict(poller.poll(50))
                if sock in events and events[sock] & zmq.POLLIN:
                    msg = sock.recv()
                    if msg and msg[0] == 1: self.session.send(sock, "iopub_welcome", {}, parent=None)
        finally: sock.close(0)

    def _drain_queue(self):
        "Send queued IOPub messages to the socket."
        if self._socket is None: return
        while True:
            try: msg_type, content, parent, metadata, ident, buffers = self.queue.get_nowait()
            except queue.Empty: break
            self.session.send(self._socket, msg_type, content, parent=parent, metadata=metadata, ident=ident,
                buffers=buffers)

    def stop(self): self._stop_event.set()


_INPUT_INTERRUPTED = object()


class StdinRouterThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr:str, session: Session):
        "Initialize stdin router for input_request/reply."
        super().__init__(daemon=True)
        store_attr()
        self._stop_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._pending_lock = threading.Lock()
        self._requests = queue.Queue()
        self._pending = {}
        self._pending_by_ident = {}
        self._socket = None

    def request_input( self, prompt:str, password: bool, parent: dict|None,
        ident: list[bytes]|None, timeout:float|None=None,)->str:
        "Send input_request and wait for input_reply; honors `timeout`."
        response_queue = queue.Queue()
        self._requests.put((prompt, password, parent, ident, response_queue))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._interrupt_event.is_set():
                self._interrupt_event.clear()
                raise KeyboardInterrupt
            if self._stop_event.is_set(): raise RuntimeError("stdin router stopped")
            try:
                if deadline is None: value = response_queue.get(timeout=0.1)
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0.0: raise TimeoutError("timed out waiting for input reply")
                    value = response_queue.get(timeout=min(0.1, remaining))
                if value is _INPUT_INTERRUPTED:
                    self._interrupt_event.clear()
                    raise KeyboardInterrupt
                return value
            except queue.Empty: continue

    def run(self):
        "Route input_reply messages to waiting queues."
        sock = self.context.socket(zmq.ROUTER)
        sock.linger = 0
        if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
        sock.bind(self.addr)
        self._socket = sock
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop_event.is_set():
                self._drain_requests(sock)
                events = dict(poller.poll(50))
                if sock in events and events[sock] & zmq.POLLIN:
                    try: idents, msg = self.session.recv(sock, mode=0)
                    except ValueError as err:
                        if "Duplicate Signature" not in str(err): _LOG.warning("Error decoding stdin message: %s", err)
                        continue
                    if msg is None: continue
                    if msg.get("msg_type") != "input_reply": continue
                    parent = msg.get("parent_header", {})
                    msg_id = parent.get("msg_id")
                    waiter = None
                    if msg_id:
                        with self._pending_lock:
                            pending = self._pending.pop(msg_id, None)
                            if pending is not None:
                                ident_key, waiter = pending
                                waiters = self._pending_by_ident.get(ident_key)
                                if waiters:
                                    try: waiters.remove(waiter)
                                    except ValueError: pass
                                    if not waiters: self._pending_by_ident.pop(ident_key, None)
                    if waiter is None:
                        key = tuple(idents or [])
                        with self._pending_lock:
                            waiters = self._pending_by_ident.get(key)
                            if waiters:
                                waiter = waiters.popleft()
                                if not waiters: self._pending_by_ident.pop(key, None)
                    if waiter is not None:
                        value = msg.get("content", {}).get("value", "")
                        waiter.put(value)
        finally: sock.close(0)

    def _drain_requests(self, sock: zmq.Socket):
        "Send pending input_request messages."
        while True:
            try: prompt, password, parent, ident, waiter = self._requests.get_nowait()
            except queue.Empty: return
            if self._interrupt_event.is_set():
                waiter.put(_INPUT_INTERRUPTED)
                continue
            msg = self.session.send(sock, "input_request", {"prompt": prompt, "password": password},
                parent=parent, ident=ident)
            msg_id = msg.get("header", {}).get("msg_id")
            key = tuple(ident or [])
            with self._pending_lock:
                if msg_id: self._pending[msg_id] = (key, waiter)
                self._pending_by_ident.setdefault(key, deque()).append(waiter)

    def stop(self): self._stop_event.set()

    def interrupt_pending(self):
        "Cancel pending input requests and wake any waiters."
        self._interrupt_event.set()
        waiters = []
        with self._pending_lock:
            waiters.extend(waiter for _, waiter in self._pending.values())
            self._pending.clear()
            self._pending_by_ident.clear()
        while True:
            try: _prompt, _password, _parent, _ident, waiter = self._requests.get_nowait()
            except queue.Empty: break
            waiters.append(waiter)
        for waiter in waiters: waiter.put(_INPUT_INTERRUPTED)


class AsyncRouterThread(threading.Thread):
    def __init__(self, kernel: "MiniKernel", port:int, socket_attr:str, handler, log_label:str):
        "Async ROUTER loop for shell/control sockets."
        super().__init__(daemon=True, name=f"{log_label}-router")
        store_attr("kernel,port,socket_attr,handler,log_label")
        self._loop = None
        self._queue = None
        self._ready = threading.Event()
        self._socket_ready = threading.Event()
        self._stop = threading.Event()
        self._pending = queue.Queue()

    def enqueue(self, item):
        if self._queue is None or self._loop is None or not self._ready.is_set():
            self._pending.put(item)
            return
        try: self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError: _LOG.debug("Router loop closed; dropping %s item", self.log_label)

    def stop(self):
        self._stop.set()
        if self._loop is None or self._queue is None: return
        try: self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        except RuntimeError: pass

    def run(self):
        loop = asyncio.new_event_loop()
        if sys.platform.startswith("win") and not isinstance(loop, asyncio.SelectorEventLoop):
            _LOG.warning("Windows event loop may not support zmq.asyncio; consider SelectorEventLoop policy.")
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue()
        self._ready.set()
        while True:
            try: item = self._pending.get_nowait()
            except queue.Empty: break
            self._queue.put_nowait(item)
        try: loop.run_until_complete(self._router_loop())
        finally:
            if hasattr(loop, "shutdown_asyncgens"): loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)
            self._loop = None
            self._queue = None

    async def _router_loop(self):
        sock = self.kernel.bind_router_async(self.port)
        setattr(self.kernel, self.socket_attr, sock)
        self._socket_ready.set()
        poller = zmq.asyncio.Poller()
        poller.register(sock, zmq.POLLIN)
        def _drain_send_queue():
            while True:
                try: item = self._queue.get_nowait()
                except asyncio.QueueEmpty: break
                if item is not None: self.kernel._send_router_item(sock, item)
        try:
            while not self.kernel._shutdown_event.is_set() and not self._stop.is_set():
                poll_task = asyncio.ensure_future(poller.poll())
                send_task = asyncio.create_task(self._queue.get())
                done, pending = await asyncio.wait({poll_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
                if send_task in done:
                    item = send_task.result()
                    if item is not None: self.kernel._send_router_item(sock, item)
                    _drain_send_queue()
                if poll_task in done:
                    events = dict(poll_task.result())
                    if sock in events and events[sock] & zmq.POLLIN:
                        idents, msg = await self.kernel.recv_msg_async(sock)
                        if msg is not None:
                            try: self.handler(msg, idents)
                            except Exception: _LOG.warning("Error in %s: %s", self.log_label, msg.get("msg_type"), exc_info=True)
                        _drain_send_queue()
                for task in pending: task.cancel()
                if pending: await asyncio.gather(*pending, return_exceptions=True)
        finally:
            poller.unregister(sock)
            sock.close(0)
            if getattr(self.kernel, self.socket_attr) is sock: setattr(self.kernel, self.socket_attr, None)

class Subshell:
    def __init__(self, kernel: "MiniKernel", subshell_id:str|None, user_ns: dict,
        use_singleton: bool = False, run_in_thread: bool = True):
        "Create subshell worker thread and bridge for execution."
        store_attr("kernel,subshell_id")
        self._stop = threading.Event()
        name = "subshell-parent" if subshell_id is None else f"subshell-{subshell_id}"
        self._thread = None if not run_in_thread else threading.Thread(target=self._run_loop, daemon=True, name=name)
        self._loop = None
        self._loop_ready = threading.Event()
        self._pending = deque()
        self._pending_lock = threading.Lock()
        self._pending_event = None
        self._aborting = False
        self._abort_handle = None
        self._async_lock = None
        self.bridge = KernelBridge(request_input=self.request_input, debug_event_callback=self.send_debug_event,
            zmq_context=self.kernel.context, user_ns=user_ns, use_singleton=use_singleton)
        self.bridge.set_stream_sender(self._send_stream)
        self.bridge.set_display_sender(self._send_display_event)
        self._parent_header_var = contextvars.ContextVar("parent_header", default=None)
        self._parent_idents_var = contextvars.ContextVar("parent_idents", default=None)
        self._executing = threading.Event()
        self._exec_state = ExecState.IDLE
        self._last_exec_state = None
        self._state_lock = threading.Lock()
        self._shell_required = dict(execute_request=("code",), complete_request=("code", "cursor_pos"),
            inspect_request=("code", "cursor_pos"), history_request=("hist_access_type",), is_complete_request=("code",))
        self._shell_handlers = dict(kernel_info_request=self._handle_kernel_info, connect_request=self._handle_connect,
            complete_request=self._bridge_handler, inspect_request=self._bridge_handler, history_request=self._handle_history,
            is_complete_request=self._bridge_handler, comm_info_request=self._handle_comm_info, comm_open=self._handle_comm,
            comm_msg=self._handle_comm, comm_close=self._handle_comm, shutdown_request=self.handle_shutdown)
        complete_req = ("complete_reply", dict(code=("code", ""), cursor_pos="cursor_pos"), "complete")
        inspect_req = ("inspect_reply", dict(code=("code", ""), cursor_pos="cursor_pos", detail_level=("detail_level", 0)), "inspect")
        is_complete_req = ("is_complete_reply", dict(code=("code", "")), "is_complete")
        self._bridge_specs = dict(complete_request=complete_req, inspect_request=inspect_req, is_complete_request=is_complete_req)

    def start(self):
        if self._thread is not None:
            self._thread.start()
            self._loop_ready.wait()

    def stop(self):
        "Signal subshell loop to stop and wake it."
        self._stop.set()
        if self._loop is None: return
        def _stop_loop():
            with self._pending_lock: self._pending.append((_SUBSHELL_STOP, None, None))
            if self._pending_event is not None: self._pending_event.set()
            self._loop.stop()
        if self._loop.is_running(): self._loop.call_soon_threadsafe(_stop_loop)
        else: _stop_loop()

    def join(self, timeout:float|None=None):
        if self._thread is not None: self._thread.join(timeout=timeout)

    def submit(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        with self._pending_lock: self._pending.append((msg, idents, sock))
        if self._pending_event is None: return
        if self._loop is not None and self._loop.is_running(): self._loop.call_soon_threadsafe(self._pending_event.set)
        else: self._pending_event.set()

    def _set_exec_state(self, state: ExecState):
        with self._state_lock: self._exec_state = state

    def _set_last_exec_state(self, state: ExecState):
        with self._state_lock: self._last_exec_state = state

    def _get_exec_state(self)->ExecState:
        with self._state_lock: return self._exec_state

    def interrupt(self)->bool:
        "Raise KeyboardInterrupt in subshell thread if executing."
        if self._thread is None: return False
        if self._get_exec_state() != ExecState.RUNNING: return False
        self._set_exec_state(ExecState.CANCELLING)
        if self.bridge.cancel_exec_task(self._loop): return True
        thread_id = self._thread.ident
        if thread_id is None: return False
        return _raise_async_exception(thread_id, KeyboardInterrupt)

    def request_input(self, prompt:str, password: bool)->str:
        "Forward input_request through stdin router for this subshell."
        try:
            if sys.stdout is not None: sys.stdout.flush()
            if sys.stderr is not None: sys.stderr.flush()
        except (OSError, ValueError): pass
        parent = self._parent_header_var.get()
        idents = self._parent_idents_var.get()
        return self.kernel.stdin_router.request_input(prompt, password, parent, idents)

    def _send_stream(self, name:str, text:str):
        "Send stream output on IOPub for current parent message."
        parent = self._parent_header_var.get()
        if not parent: return
        self.kernel.iopub.stream(parent, name=name, text=text)

    def _send_display_event(self, event: dict):
        "Send display/clear events on IOPub for current parent message."
        parent = self._parent_header_var.get()
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
        parent = self._parent_header_var.get() or {}
        self.kernel.iopub.debug_event(parent, content=event)

    def send_status(self, state:str, parent: dict|None): self.kernel.iopub.status(parent, execution_state=state)

    def send_reply(self, socket: zmq.Socket, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        if socket is self.kernel.control_socket: self.kernel.queue_control_reply(msg_type, content, parent, idents)
        else: self.kernel.queue_shell_reply(msg_type, content, parent, idents)

    def _run_loop(self):
        "Run subshell asyncio loop in a background thread."
        self._setup_loop()
        try: self._loop.run_forever()
        finally: self._shutdown_loop()

    def run_main(self):
        "Run parent subshell loop in the main thread."
        self._setup_loop()
        try: self._loop.run_forever()
        finally: self._shutdown_loop()

    def _setup_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._pending_event = asyncio.Event()
        self._async_lock = asyncio.Lock()
        self._loop_ready.set()
        if self._pending: self._pending_event.set()
        loop.create_task(self._consume_queue())

    def _shutdown_loop(self):
        if self._loop is None: return
        loop = self._loop
        asyncio.set_event_loop(loop)
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending: task.cancel()
        if pending: loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        if hasattr(loop, "shutdown_asyncgens"): loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
        self._loop = None
        self._pending_event = None
        self._async_lock = None

    async def _consume_queue(self):
        "Process queued shell messages on the subshell event loop."
        while True:
            await self._pending_event.wait()
            while True:
                with self._pending_lock:
                    if not self._pending:
                        self._pending_event.clear()
                        break
                    msg, idents, sock = self._pending.popleft()
                if msg is _SUBSHELL_STOP: return
                if msg is _SUBSHELL_ABORT_CLEAR:
                    self._stop_aborting()
                    continue
                if not msg: continue
                async with self._async_lock:
                    try: await self._handle_message(msg, idents, sock)
                    except Exception: _LOG.warning("Error in subshell handler", exc_info=True)

    async def _handle_message(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Dispatch message based on msg_type, handling execute separately."
        msg_type = msg["header"]["msg_type"]
        token_header = self._parent_header_var.set(msg)
        token_idents = self._parent_idents_var.set(idents)
        try:
            missing = self._missing_fields(msg_type, msg.get("content", {}))
            if missing:
                self._send_missing_fields_reply(msg_type, missing, msg, idents, sock)
                return
            if msg_type == "execute_request" and self._aborting:
                self._send_abort_reply(sock, msg, idents)
                return
            if msg_type == "execute_request":
                await self._handle_execute(msg, idents, sock)
                return
            self._dispatch_shell_non_execute(msg, idents, sock)
        finally:
            self._parent_header_var.reset(token_header)
            self._parent_idents_var.reset(token_idents)

    def _missing_fields(self, msg_type:str, content: dict)->list[str]:
        required = self._shell_required.get(msg_type)
        if not required: return []
        return [key for key in required if key not in content]

    def _missing_reply_defaults(self, msg_type:str)->dict:
        cr = dict(matches=[], cursor_start=0, cursor_end=0, metadata={})
        ir = dict(found=False, data={}, metadata={})
        return dict(complete_request=cr, inspect_request=ir, history_request={"history": []},
            is_complete_request={"indent": ""}).get(msg_type, {})

    def _send_missing_fields_reply(self, msg_type:str, missing: list[str], msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        reply_type = msg_type.replace("_request", "_reply")
        evalue = f"missing required fields: {', '.join(missing)}"
        reply = dict(status="error", ename="MissingField", evalue=evalue, traceback=[])
        if msg_type == "execute_request":
            reply |= dict(execution_count=self.bridge.shell.execution_count, user_expressions={}, payload=[])
        else: reply |= self._missing_reply_defaults(msg_type)
        self.send_reply(sock, reply_type, reply, msg, idents)

    def _send_abort_reply(self, sock: zmq.Socket, msg: dict, idents: list[bytes]|None):
        "Send an abort reply for `msg`."
        self.kernel.send_status("busy", msg)
        self._set_last_exec_state(ExecState.ABORTED)
        reply_content = dict(status="aborted", execution_count=self.bridge.shell.execution_count, user_expressions={}, payload=[])
        self.send_reply(sock, "execute_reply", reply_content, msg, idents)
        self.kernel.send_status("idle", msg)

    def _start_aborting(self):
        "Mark subshell aborting and schedule clearing."
        self._aborting = True
        if self._abort_handle is not None:
            self._abort_handle.cancel()
            self._abort_handle = None
        timeout = self.kernel.stop_on_error_timeout
        if timeout > 0: self._abort_handle = self._loop.call_later(timeout, self._stop_aborting)

    def _stop_aborting(self):
        "Clear aborting state after stop_on_error window."
        self._aborting = False
        if self._abort_handle is not None:
            self._abort_handle.cancel()
            self._abort_handle = None

    def _dispatch_shell_non_execute(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Dispatch non-execute shell requests to handlers."
        msg_type = msg["header"]["msg_type"]
        handler = self._shell_handlers.get(msg_type)
        if handler is None:
            self.send_reply(sock, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, sock)

    def _abort_pending_executes(self, append_abort_clear: bool = False):
        "Abort queued execute requests and reply aborted."
        drained = []
        with self._pending_lock:
            while self._pending: drained.append(self._pending.popleft())
            if append_abort_clear: self._pending.append((_SUBSHELL_ABORT_CLEAR, None, None))
        for msg, idents, sock in drained:
            if msg is _SUBSHELL_STOP:
                with self._pending_lock: self._pending.append((msg, idents, sock))
                break
            if msg is _SUBSHELL_ABORT_CLEAR:
                with self._pending_lock: self._pending.append((msg, idents, sock))
                continue
            msg_type = msg.get("header", {}).get("msg_type")
            if msg_type == "execute_request":
                self._set_last_exec_state(ExecState.ABORTED)
                reply_content = dict(status="aborted", execution_count=self.bridge.shell.execution_count,
                    user_expressions={}, payload=[])
                self.send_reply(sock, "execute_reply", reply_content, msg, idents)
            elif msg_type: self._dispatch_shell_non_execute(msg, idents, sock)

    def _handle_kernel_info(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Reply to kernel_info_request."
        with self.kernel.busy_idle(msg):
            content = self.kernel.kernel_info_content()
            self.send_reply(sock, "kernel_info_reply", content, msg, idents)

    def _handle_connect(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Reply to connect_request with port numbers."
        content = dict(shell_port=self.kernel.connection.shell_port, iopub_port=self.kernel.connection.iopub_port,
            stdin_port=self.kernel.connection.stdin_port, control_port=self.kernel.connection.control_port, hb_port=self.kernel.connection.hb_port)
        self.send_reply(sock, "connect_reply", content, msg, idents)

    async def _handle_execute(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle execute_request via KernelBridge and emit IOPub."
        content = msg.get("content", {})
        code = content.get("code", "")
        silent = bool(content.get("silent", False))
        store_history = bool(content.get("store_history", True))
        stop_on_error = bool(content.get("stop_on_error", True))
        user_expressions = content.get("user_expressions", {})
        allow_stdin = bool(content.get("allow_stdin", False))

        self._set_exec_state(ExecState.RUNNING)
        self._executing.set()
        sent_reply = False
        sent_error = False
        exec_count = None
        terminal_state = ExecState.COMPLETED

        self.kernel.send_status("busy", msg)
        iopub = self.kernel.iopub
        try:
            exec_count_pre = self.bridge.shell.execution_count
            if not silent: exec_count_pre += 1 if store_history else 0
            # pre-send execute_input before any live output to match ipykernel ordering
            if not silent: iopub.execute_input(msg, code=code, execution_count=exec_count_pre)
            try:
                with comm_context(iopub.send, msg):
                    result = await self.bridge.execute(code, silent=silent, store_history=store_history,
                        user_expressions=user_expressions, allow_stdin=allow_stdin)
            except BaseException as exc:
                tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
                result = dict(streams=[], display=[], result=None, result_metadata={}, execution_count=self.bridge.shell.execution_count,
                    error=dict(ename=type(exc).__name__, evalue=str(exc), traceback=tb), user_expressions={}, payload=[])

            exec_count = result.get("execution_count")

            error = result.get("error")
            if error and error.get("ename") == "CancelledError" and self._get_exec_state() == ExecState.CANCELLING:
                error = dict(error) | dict(ename="KeyboardInterrupt", evalue="")
            if not silent:
                for stream in result.get("streams", []): iopub.stream(msg, name=stream["name"], text=stream["text"])
                for event in result.get("display", []):
                    if event.get("type") == "clear_output":
                        iopub.clear_output(msg, wait=event.get("wait", False))
                        continue
                    if event.get("type") == "display":
                        data = event.get("data", {})
                        metadata = event.get("metadata", {})
                        transient = event.get("transient", {})
                        buffers = event.get("buffers")
                        payload = dict(data=data, metadata=metadata, transient=transient)
                        if event.get("update"): iopub.update_display_data(msg, payload, buffers=buffers)
                        else: iopub.display_data(msg, payload, buffers=buffers)

            if error:
                if error.get("ename") == "KeyboardInterrupt": terminal_state = ExecState.ABORTED
                tb = error.get("traceback", [])
                iopub.error(msg, ename=error["ename"], evalue=error["evalue"], traceback=tb)
                sent_error = True

            if not silent and not error and result.get("result") is not None:
                result_content = dict(execution_count=exec_count, data=result.get("result"), metadata=result.get("result_metadata", {}))
                iopub.execute_result(msg, result_content)

            reply_content = dict(status="ok" if not error else "error", execution_count=exec_count,
                user_expressions=result.get("user_expressions", {}), payload=result.get("payload", []))
            if error: reply_content.update(error)

            self.send_reply(sock, "execute_reply", reply_content, msg, idents)
            sent_reply = True
            if error and stop_on_error:
                append_abort_clear = self.kernel.stop_on_error_timeout <= 0
                self._abort_pending_executes(append_abort_clear=append_abort_clear)
                self._start_aborting()
        except KeyboardInterrupt as exc:
            terminal_state = ExecState.ABORTED
            error = dict(ename=type(exc).__name__, evalue=str(exc),
                traceback=traceback.format_exception(type(exc), exc, exc.__traceback__))
            if not sent_error: iopub.error(msg, content=error)
            if not sent_reply:
                reply_content = dict(status="error", execution_count=exec_count or self.bridge.shell.execution_count,
                    user_expressions={}, payload=[])
                reply_content.update(error)
                self.send_reply(sock, "execute_reply", reply_content, msg, idents)
                if stop_on_error:
                    append_abort_clear = self.kernel.stop_on_error_timeout <= 0
                    self._abort_pending_executes(append_abort_clear=append_abort_clear)
                    self._start_aborting()
        finally:
            self.kernel.send_status("idle", msg)
            self._set_last_exec_state(terminal_state)
            self._executing.clear()
            self._set_exec_state(ExecState.IDLE)

    def _bridge_handler(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle simple bridge-request messages."
        msg_type = msg.get("header", {}).get("msg_type")
        spec = self._bridge_specs.get(msg_type)
        if spec is None: return
        reply_type, fields, method = spec
        self._bridge_reply(msg, idents, sock, reply_type, method, **fields)

    def _handle_history(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Reply to history_request."
        content = msg.get("content", {})
        reply = self.bridge.history(content.get("hist_access_type", ""), bool(content.get("output", False)),
            bool(content.get("raw", False)), session=int(content.get("session", 0)), start=int(content.get("start", 0)),
            stop=content.get("stop"), n=content.get("n"), pattern=content.get("pattern"), unique=bool(content.get("unique", False)))
        self.send_reply(sock, "history_reply", reply, msg, idents)

    def _bridge_reply(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket, reply_type:str, method:str, **fields):
        "Call a bridge method and send reply."
        content = msg.get("content", {})
        args = {}
        for name, spec in fields.items():
            if isinstance(spec, tuple): key, default = spec
            else: key, default = spec, None
            args[name] = content.get(key, default)
        reply = getattr(self.bridge, method)(**args)
        self.send_reply(sock, reply_type, reply, msg, idents)

    def _handle_comm_info(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Reply to comm_info_request."
        content = msg.get("content", {})
        target_name = content.get("target_name")
        manager = get_comm_manager()
        comms = {comm_id: dict(target_name=comm.target_name) for comm_id, comm in manager.comms.items()
            if target_name is None or comm.target_name == target_name}
        reply = dict(status="ok", comms=comms)
        self.send_reply(sock, "comm_info_reply", reply, msg, idents)

    def _handle_comm(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle comm requests and broadcast on IOPub."
        msg_type = msg["header"]["msg_type"]
        manager = get_comm_manager()
        getattr(manager, msg_type)(None, None, msg)
        self.kernel.iopub.send(msg_type, msg, msg.get("content", {}), metadata=msg.get("metadata"), buffers=msg.get("buffers"))

    def handle_shutdown(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        self.kernel.handle_shutdown(msg, idents, sock)


class SubshellManager:
    def __init__(self, kernel: "MiniKernel"):
        "Manage parent and child subshells sharing a user namespace."
        self.kernel = kernel
        self._user_ns = {}
        self.parent = Subshell(kernel, None, self._user_ns, use_singleton=True, run_in_thread=False)
        self._subs = {}
        self._lock = threading.Lock()

    def start(self): return

    def get(self, subshell_id:str|None)->Subshell|None:
        "Return subshell by id, or the parent when None."
        if subshell_id is None: return self.parent
        with self._lock: return self._subs.get(subshell_id)

    def create(self)->str:
        "Create and start a new subshell; return its id."
        subshell_id = str(uuid.uuid4())
        subshell = Subshell(self.kernel, subshell_id, self._user_ns)
        with self._lock: self._subs[subshell_id] = subshell
        subshell.start()
        return subshell_id

    def list(self)->list[str]:
        with self._lock: return list(self._subs.keys())

    def delete(self, subshell_id:str):
        "Stop and remove a subshell by id."
        with self._lock: subshell = self._subs.pop(subshell_id)
        subshell.stop()
        subshell.join(timeout=1)

    def stop_all(self):
        "Stop all subshells and the parent."
        with self._lock:
            subshells = list(self._subs.values())
            self._subs.clear()
        for subshell in subshells:
            subshell.stop()
            subshell.join(timeout=1)
        self.parent.stop()
        self.parent.join(timeout=1)

    def interrupt_all(self):
        "Send interrupts to all subshells."
        self.parent.interrupt()
        with self._lock: subshells = list(self._subs.values())
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


class MiniKernel:
    def __init__(self, connection_file:str):
        "Initialize kernel sockets, threads, and subshell manager."
        self.connection = ConnectionInfo.from_file(connection_file)
        key = self.connection.key.encode()
        self.session = Session(key=key, signature_scheme=self.connection.signature_scheme)
        self.context = zmq.Context.instance()
        self.async_context = zmq.asyncio.Context.shadow(self.context) if hasattr(zmq.asyncio.Context, "shadow") else zmq.asyncio.Context.instance()
        self.shell_socket = None
        self.control_socket = None
        self.iopub_thread = IOPubThread(self.context, self.connection.addr(self.connection.iopub_port), self.session)
        self.stdin_router = StdinRouterThread(self.context, self.connection.addr(self.connection.stdin_port), self.session)

        self.hb = HeartbeatThread(self.context, self.connection.addr(self.connection.hb_port))
        self.subshells = SubshellManager(self)
        self.bridge = self.subshells.parent.bridge
        self._parent_header = None
        self._parent_idents = None
        self._shell_router = None
        self._control_router = None
        self._shutdown_event = threading.Event()
        self.stop_on_error_timeout = _env_float("IPYMINI_STOP_ON_ERROR_TIMEOUT", 0.0)
        self._iopub_cmd = None
        self._control_handlers = dict(shutdown_request=self.handle_shutdown, debug_request=self.handle_debug,
            interrupt_request=self.handle_interrupt)
        self._control_handlers |= dict(create_subshell_request=self.handle_create_subshell, list_subshell_request=self.handle_list_subshell)
        self._control_handlers |= dict(delete_subshell_request=self.handle_delete_subshell, kernel_info_request=self.handle_control_kernel_info)

    def start(self):
        "Start kernel threads and serve shell/control messages."
        self.iopub_thread.start()
        self.stdin_router.start()
        self.subshells.start()
        self.hb.start()
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self.handle_sigint)
        self._shutdown_event.clear()
        self._shell_router = AsyncRouterThread(self, self.connection.shell_port, "shell_socket", self.handle_shell_msg, "shell")
        self._control_router = AsyncRouterThread(self, self.connection.control_port, "control_socket", self.handle_control_msg, "control")
        self._shell_router.start()
        self._control_router.start()
        self._shell_router._ready.wait()
        self._control_router._ready.wait()
        try: self.subshells.parent.run_main()
        finally:
            self._shutdown_event.set()
            if self._shell_router is not None: self._shell_router.stop()
            if self._control_router is not None: self._control_router.stop()
            if self._shell_router is not None: self._shell_router.join(timeout=1)
            if self._control_router is not None: self._control_router.join(timeout=1)
            self.hb.stop()
            self.hb.join(timeout=1)
            self.subshells.stop_all()
            self.stdin_router.stop()
            self.stdin_router.join(timeout=1)
            self.iopub_thread.stop()
            self.iopub_thread.join(timeout=1)
            signal.signal(signal.SIGINT, prev_sigint)

    def bind_router(self, port:int)->zmq.Socket:
        "Bind ROUTER socket to `port`."
        sock = self.context.socket(zmq.ROUTER)
        if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
        sock.linger = 0
        sock.bind(self.connection.addr(port))
        return sock

    def bind_router_async(self, port:int)->zmq.asyncio.Socket:
        "Bind async ROUTER socket to `port`."
        sock = self.async_context.socket(zmq.ROUTER)
        if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
        sock.linger = 0
        sock.bind(self.connection.addr(port))
        return sock

    def recv_msg(self, sock: zmq.Socket):
        "Receive message from `sock`."
        try: return self.session.recv(sock, mode=0)
        except ValueError as err:
            if "Duplicate Signature" not in str(err): _LOG.warning("Bad message signature", exc_info=True)
        except zmq.ZMQError: pass
        return None, None

    async def recv_msg_async(self, sock: zmq.asyncio.Socket):
        "Receive message from async `sock`."
        try: msg_list = await sock.recv_multipart(copy=False)
        except zmq.ZMQError: return None, None
        idents, msg_list = self.session.feed_identities(msg_list, copy=False)
        try: msg = self.session.deserialize(msg_list, content=True, copy=False)
        except ValueError as err:
            if "Duplicate Signature" not in str(err): _LOG.warning("Bad message signature", exc_info=True)
            return None, None
        return idents, msg

    def _send_router_item(self, sock: zmq.asyncio.Socket, item):
        msg_type, content, parent, idents = item
        self.session.send(sock, msg_type, content, parent=parent, ident=idents)

    def handle_sigint(self, signum, frame):
        "Handle SIGINT by interrupting subshells and input waits."
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()
        if self.subshells.parent._executing.is_set():
            self.subshells.parent._set_exec_state(ExecState.CANCELLING)
            raise KeyboardInterrupt

    def handle_control_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle control channel request message."
        msg_type = msg["header"]["msg_type"]
        handler = self._control_handlers.get(msg_type)
        if handler is None:
            self.send_reply(self.control_socket, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, self.control_socket)

    def handle_shell_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle shell request message and dispatch to subshells."
        subshell_id = msg.get("header", {}).get("subshell_id")
        subshell = self.subshells.get(subshell_id)
        if subshell is None:
            self.send_subshell_error(msg, idents)
            return
        subshell.submit(msg, idents, self.shell_socket)

    def send_reply(self, socket: zmq.Socket, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        self.session.send(socket, msg_type, content, parent=parent, ident=idents)

    def queue_shell_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        "Enqueue a shell reply for async send."
        if self._shell_router is None: return
        self._shell_router.enqueue((msg_type, content, parent, idents))

    def queue_control_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        "Enqueue a control reply for async send."
        if self._control_router is None: return
        self._control_router.enqueue((msg_type, content, parent, idents))

    def send_status(self, state:str, parent: dict|None): self.iopub.status(parent, execution_state=state)

    @contextmanager
    def busy_idle(self, parent: dict|None):
        "Send busy before work and idle after."
        self.send_status("busy", parent)
        try: yield
        finally: self.send_status("idle", parent)

    def send_debug_event(self, event: dict):
        "Send a debug_event on IOPub using current parent header."
        parent = self._parent_header or {}
        self.iopub.debug_event(parent, content=event)

    @property
    def iopub(self)->IOPubCommand:
        "Return cached IOPubCommand wrapper."
        if (proxy := self._iopub_cmd) is None: self._iopub_cmd = proxy = IOPubCommand(self)
        return proxy

    def iopub_send(self, msg_type:str, parent: dict|None, content: dict|None=None, metadata: dict|None=None,
        ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None, **kwargs):
        "Queue an IOPub message with optional metadata and buffers."
        if kwargs: content = dict(content or {}) | kwargs
        if content is None: content = {}
        self.iopub_thread.send(msg_type, content, parent, metadata, ident, buffers)

    def send_subshell_error(self, msg: dict, idents: list[bytes]|None):
        "Send SubshellNotFound error reply for unknown subshell."
        msg_type = msg.get("header", {}).get("msg_type", "")
        subshell_id = msg.get("header", {}).get("subshell_id")
        if not msg_type.endswith("_request"): return
        content = dict(status="error", ename="SubshellNotFound", evalue=f"Unknown subshell_id {subshell_id!r}", traceback=[])
        if msg_type == "execute_request": content.update(dict(execution_count=0, user_expressions={}, payload=[]))
        self.send_reply(self.shell_socket, msg_type.replace("_request", "_reply"), content, msg, idents)

    def control_ok(self, sock: zmq.Socket, msg: dict, idents: list[bytes]|None, reply_type:str, fn):
        "Send ok/error reply for control handlers."
        try:
            content = fn() or {}
            content = dict(status="ok") | content
        except Exception as exc: content = dict(status="error", evalue=str(exc))
        self.send_reply(sock, reply_type, content, msg, idents)

    def handle_create_subshell(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle create_subshell_request."
        def _do(): return dict(subshell_id=self.subshells.create())
        self.control_ok(sock, msg, idents, "create_subshell_reply", _do)

    def handle_list_subshell(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle list_subshell_request."
        def _do(): return dict(subshell_id=self.subshells.list())
        self.control_ok(sock, msg, idents, "list_subshell_reply", _do)

    def handle_delete_subshell(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle delete_subshell_request."
        def _do():
            subshell_id = msg.get("content", {}).get("subshell_id")
            if not isinstance(subshell_id, str): raise ValueError("subshell_id required")
            self.subshells.delete(subshell_id)
            return {}
        self.control_ok(sock, msg, idents, "delete_subshell_reply", _do)

    def handle_control_kernel_info(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Reply to kernel_info_request on control channel."
        content = self.kernel_info_content()
        self.send_reply(sock, "kernel_info_reply", content, msg, idents)

    def kernel_info_content(self)->dict:
        "Build kernel_info_reply content."
        try: impl_version = version("ipymini")
        except PackageNotFoundError: impl_version = "0.0.0+local"
        supported_features = ["kernel subshells", "debugger"]
        return dict(status="ok", protocol_version="5.3", implementation="ipymini", implementation_version=impl_version,
            language_info=dict(name="python", version=self.python_version(), mimetype="text/x-python",
                file_extension=".py", pygments_lexer="python", codemirror_mode={"name": "ipython", "version": 3},
                nbconvert_exporter="python"), banner="ipymini", help_links=[], supported_features=supported_features)

    def python_version(self)->str: return ".".join(str(x) for x in os.sys.version_info[:3])

    def handle_debug(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle debug_request via KernelBridge and emit events."
        self.send_status("busy", msg)
        self._parent_header = msg
        try:
            content = msg.get("content", {})
            reply = self.bridge.debug_request(json.dumps(content))
            response = reply.get("response", {})
            events = reply.get("events", [])
            self.send_reply(sock, "debug_reply", response, msg, idents)
            for event in events: self.send_debug_event(event)
            self.send_status("idle", msg)
        finally: self._parent_header = None

    def send_interrupt_signal(self):
        "Send SIGINT to the current process or process group."
        if os.name == "nt":
            _LOG.warning("Interrupt request not supported on Windows")
            return
        pid = os.getpid()
        try: pgid = os.getpgid(pid)
        except OSError: pgid = None
        try:
            # Only signal the process group if we're the leader; otherwise avoid killing unrelated processes.
            if pgid and pgid == pid and hasattr(os, "killpg"): os.killpg(pgid, signal.SIGINT)
            else: os.kill(pid, signal.SIGINT)
        except OSError as err: _LOG.warning("Interrupt signal failed: %s", err)

    def handle_interrupt(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle interrupt_request by signaling subshells."
        self.send_interrupt_signal()
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()
        self.send_reply(sock, "interrupt_reply", {"status": "ok"}, msg, idents)

    def handle_shutdown(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle shutdown_request and stop main loop."
        content = msg.get("content", {})
        reply = {"status": "ok", "restart": bool(content.get("restart", False))}
        self.send_reply(sock, "shutdown_reply", reply, msg, idents)
        self._shutdown_event.set()
        self.subshells.parent.stop()


def run_kernel(connection_file:str):
    "Run kernel given a connection file path."
    signal.signal(signal.SIGINT, signal.default_int_handler)
    kernel = MiniKernel(connection_file)
    kernel.start()
