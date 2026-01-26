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
from . import debug as _dbg_mod

log = logging.getLogger("ipymini.kernel")
_debug = _dbg_mod.enabled
_dbg_lock = threading.Lock()
def dbg(*args, **kw):
    if _debug:
        with _dbg_lock: print("[ipymini]", *args, **kw, file=sys.__stderr__, flush=True)

def _install_thread_excepthook(kernel: "MiniKernel"):
    prev = threading.excepthook
    def hook(args):
        try: prev(args)
        except Exception: pass
        name = getattr(args.thread, "name", "")
        critical = name in {"iopub-thread", "stdin-router", "heartbeat-thread"} or name.endswith("-router")
        if not critical: return
        log.error("Critical thread crashed: %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        kernel.shutdown_event.set()
        kernel.subshells.parent.stop()
    threading.excepthook = hook
    return prev
subshell_stop = object()
subshell_abort_clear = object()


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

    def drain_nowait(self)->list:
        if self.q is None: return []
        out = []
        while True:
            try: out.append(self.q.get_nowait())
            except asyncio.QueueEmpty: return out

    def suppress_late_puts(self): self.suppress_late = True

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

shell_required = dict(execute_request=("code",), complete_request=("code", "cursor_pos"),
    inspect_request=("code", "cursor_pos"), history_request=("hist_access_type",), is_complete_request=("code",))
bridge_specs = dict(complete_request=("complete_reply", dict(code=("code", ""), cursor_pos="cursor_pos"), "complete"),
    inspect_request=("inspect_reply", dict(code=("code", ""), cursor_pos="cursor_pos", detail_level=("detail_level", 0)), "inspect"),
    is_complete_request=("is_complete_reply", dict(code=("code", "")), "is_complete"))
missing_defaults = dict(complete_request=dict(matches=[], cursor_start=0, cursor_end=0, metadata={}),
    inspect_request=dict(found=False, data={}, metadata={}), history_request={"history": []}, is_complete_request={"indent": ""})


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
        super().__init__(daemon=True, name="heartbeat-thread")
        store_attr()
        self.stop_event = threading.Event()

    def run(self):
        "Echo heartbeat requests on REP socket until stopped."
        sock = None
        try:
            sock = self.context.socket(zmq.REP)
            sock.linger = 0
            sock.bind(self.addr)
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.stop_event.is_set():
                events = dict(poller.poll(100))
                if sock in events and events[sock] & zmq.POLLIN:
                    msg = sock.recv()
                    sock.send(msg)
        finally:
            if sock is not None: sock.close(0)

    def stop(self): self.stop_event.set()


class IOPubThread(threading.Thread):
    "IOPub sender thread using a sync PUB socket with a bounded queue."

    def __init__(self, context: zmq.Context, addr:str, session: Session):
        super().__init__(daemon=True, name="iopub-thread")
        store_attr()
        self.stop_event = threading.Event()
        self.q = queue.Queue(maxsize=int(os.environ.get("IPYMINI_IOPUB_QMAX", "10000")))
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def send(self, msg_type:str, content: dict, parent: dict|None, metadata: dict|None=None,
        ident: bytes | list[bytes]|None=None, buffers: list[bytes | memoryview]|None=None):
        "Queue an IOPub message for send; drop on full queue."
        self.enqueued += 1
        try: self.q.put_nowait((msg_type, content, parent, metadata, ident, buffers))
        except queue.Full:
            backlog = self.enqueued - self.sent
            if backlog in (100, 500, 1000): log.warning("IOPub queue full; dropping. enq=%d sent=%d", self.enqueued, self.sent)

    def run(self):
        dbg("IOPubThread starting...")
        sock = None
        try:
            sock = self.context.socket(zmq.PUB)
            sock.linger = 0
            if (hwm := os.environ.get("IPYMINI_IOPUB_SNDHWM")):
                try: sock.sndhwm = int(hwm)
                except ValueError: pass
            sock.bind(self.addr)
            dbg(f"IOPubThread bound to {self.addr}")
            while True:
                item = self.q.get()
                if item is None or self.stop_event.is_set(): break
                msg_type, content, parent, metadata, ident, buffers = item
                parent_id = (parent or {}).get("header", {}).get("msg_id", "?")[:8]
                if msg_type == "status": dbg(f"iopub SEND {msg_type} state={content.get('execution_state')} parent={parent_id}")
                else: dbg(f"iopub SEND {msg_type} parent={parent_id}")
                self.session.send(sock, msg_type, content, parent=parent, metadata=metadata, ident=ident, buffers=buffers)
                self.sent += 1
                dbg(f"iopub SENT {msg_type} parent={parent_id}")
        finally:
            dbg("IOPubThread exiting")
            try:
                if sock is not None: sock.close(0)
            except Exception: pass

    def stop(self):
        self.stop_event.set()
        try: self.q.put_nowait(None)
        except queue.Full:
            while True:
                try: self.q.get_nowait()
                except queue.Empty: break
            self.q.put_nowait(None)


input_interrupted = object()


class StdinRouterThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr:str, session: Session):
        "Initialize stdin router for input_request/reply."
        super().__init__(daemon=True, name="stdin-router")
        store_attr()
        self.stop_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.pending_lock = threading.Lock()
        self.requests = queue.Queue()
        self.pending = {}
        self.pending_by_ident = {}
        self.socket = None

    def request_input( self, prompt:str, password: bool, parent: dict|None,
        ident: list[bytes]|None, timeout:float|None=None,)->str:
        "Send input_request and wait for input_reply; honors `timeout`."
        response_queue = queue.Queue()
        self.requests.put((prompt, password, parent, ident, response_queue))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.interrupt_event.is_set():
                self.interrupt_event.clear()
                raise KeyboardInterrupt
            if self.stop_event.is_set(): raise RuntimeError("stdin router stopped")
            try:
                if deadline is None: value = response_queue.get(timeout=0.1)
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0.0: raise TimeoutError("timed out waiting for input reply")
                    value = response_queue.get(timeout=min(0.1, remaining))
                if value is input_interrupted:
                    self.interrupt_event.clear()
                    raise KeyboardInterrupt
                return value
            except queue.Empty: continue

    def run(self):
        "Route input_reply messages to waiting queues."
        sock = None
        try:
            sock = self.context.socket(zmq.ROUTER)
            sock.linger = 0
            if hasattr(zmq, "ROUTER_HANDOVER"): sock.router_handover = 1
            sock.bind(self.addr)
            self.socket = sock
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self.stop_event.is_set():
                self._drain_requests(sock)
                events = dict(poller.poll(50))
                if sock in events and events[sock] & zmq.POLLIN:
                    try: idents, msg = self.session.recv(sock, mode=0)
                    except ValueError as err:
                        if "Duplicate Signature" not in str(err): log.warning("Error decoding stdin message: %s", err)
                        continue
                    if msg is None: continue
                    if msg.get("msg_type") != "input_reply": continue
                    parent = msg.get("parent_header", {})
                    msg_id = parent.get("msg_id")
                    waiter = None
                    if msg_id:
                        with self.pending_lock:
                            pending = self.pending.pop(msg_id, None)
                            if pending is not None:
                                ident_key, waiter = pending
                                waiters = self.pending_by_ident.get(ident_key)
                                if waiters:
                                    try: waiters.remove(waiter)
                                    except ValueError: pass
                                    if not waiters: self.pending_by_ident.pop(ident_key, None)
                    if waiter is None:
                        key = tuple(idents or [])
                        with self.pending_lock:
                            waiters = self.pending_by_ident.get(key)
                            if waiters:
                                waiter = waiters.popleft()
                                if not waiters: self.pending_by_ident.pop(key, None)
                    if waiter is not None:
                        value = msg.get("content", {}).get("value", "")
                        waiter.put(value)
        finally:
            if sock is not None: sock.close(0)

    def _drain_requests(self, sock: zmq.Socket):
        while True:
            try: prompt, password, parent, ident, waiter = self.requests.get_nowait()
            except queue.Empty: return
            if self.interrupt_event.is_set():
                waiter.put(input_interrupted)
                continue
            msg = self.session.send(sock, "input_request", {"prompt": prompt, "password": password},
                parent=parent, ident=ident)
            msg_id = msg.get("header", {}).get("msg_id")
            key = tuple(ident or [])
            with self.pending_lock:
                if msg_id: self.pending[msg_id] = (key, waiter)
                self.pending_by_ident.setdefault(key, deque()).append(waiter)

    def stop(self): self.stop_event.set()

    def interrupt_pending(self):
        "Cancel pending input requests and wake any waiters."
        self.interrupt_event.set()
        waiters = []
        with self.pending_lock:
            waiters.extend(waiter for _, waiter in self.pending.values())
            self.pending.clear()
            self.pending_by_ident.clear()
        while True:
            try: _prompt, _password, _parent, _ident, waiter = self.requests.get_nowait()
            except queue.Empty: break
            waiters.append(waiter)
        for waiter in waiters: waiter.put(input_interrupted)


class AsyncRouterThread(threading.Thread):
    def __init__(self, kernel: "MiniKernel", port:int, socket_attr:str, handler, log_label:str):
        "Async ROUTER loop for shell/control sockets."
        super().__init__(daemon=True, name=f"{log_label}-router")
        store_attr("kernel,port,socket_attr,handler,log_label")
        self.loop = None
        self.sock = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.outbox = ThreadBoundAsyncQueue()
        self.enqueued = 0
        self.sent = 0
        self.send_errors = 0

    def enqueue(self, item):
        self.enqueued += 1
        backlog = self.enqueued - self.sent
        if backlog in (1000, 2000, 5000):
            log.warning("%s backlog growing: enq=%d sent=%d", self.log_label, self.enqueued, self.sent)
        self.outbox.put(item)

    def stop(self):
        self.stop_event.set()
        self.outbox.suppress_late_puts()
        self.outbox.put(None)
        if self.loop is not None and self.sock is not None:
            try: self.loop.call_soon_threadsafe(self.sock.close, 0)
            except RuntimeError: pass

    def run(self):
        try: asyncio.run(self._run())
        finally:
            self.loop = None
            self.sock = None
            self.ready.clear()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        if sys.platform.startswith("win") and not isinstance(self.loop, asyncio.SelectorEventLoop):
            log.warning("Windows event loop may not support zmq.asyncio; consider SelectorEventLoop policy.")
        self.outbox.bind(self.loop)
        sock = self.kernel.bind_router_async(self.port)
        self.sock = sock
        setattr(self.kernel, self.socket_attr, sock)
        self.ready.set()
        try: await asyncio.gather(self._send_loop(sock), self._recv_loop(sock))
        finally:
            try: sock.close(0)
            except Exception: pass
            if getattr(self.kernel, self.socket_attr) is sock: setattr(self.kernel, self.socket_attr, None)

    async def _send_loop(self, sock: zmq.asyncio.Socket):
        while not self.stop_event.is_set():
            item = await self.outbox.get()
            if item is None: return
            msg_type, content, parent, idents = item
            parent_id = parent.get("header", {}).get("msg_id", "?")[:8]
            dbg(f"{self.log_label} SEND {msg_type} parent={parent_id}")
            msg = self.kernel.session.msg(msg_type, content, parent=parent)
            frames = self.kernel.session.serialize(msg, ident=idents)
            try:
                fut = sock.send_multipart(frames)
                if asyncio.isfuture(fut): await fut
                self.sent += 1
            except Exception as exc:
                self.send_errors += 1
                log.error("%s send error: %s", self.log_label, exc, exc_info=exc)
            dbg(f"{self.log_label} SENT {msg_type} parent={parent_id}")

    async def _recv_loop(self, sock: zmq.asyncio.Socket):
        try:
            while not self.stop_event.is_set() and not self.kernel.shutdown_event.is_set():
                dbg(f"{self.log_label} RECV waiting")
                try: idents, msg = await self.kernel.recv_msg_async(sock)
                except (zmq.ZMQError, asyncio.CancelledError) as e:
                    dbg(f"{self.log_label} RECV error: {type(e).__name__}: {e}")
                    self.outbox.put(None)  # wake sender to exit
                    return
                if msg is None:
                    dbg(f"{self.log_label} RECV None (signature?)")
                    continue
                msg_type = msg.get("header", {}).get("msg_type", "?")
                msg_id = msg.get("header", {}).get("msg_id", "?")[:8]
                dbg(f"{self.log_label} RECV {msg_type} id={msg_id}")
                _dbg_mod.tlog(log, f"{self.log_label} recv", msg)
                self.handler(msg, idents)
        finally: dbg(f"{self.log_label} RECV EXITING")

class Subshell:
    def __init__(self, kernel: "MiniKernel", subshell_id:str|None, user_ns: dict,
        use_singleton: bool = False, run_in_thread: bool = True):
        "Create subshell worker thread and bridge for execution."
        store_attr("kernel,subshell_id")
        self.stop_event = threading.Event()  # not `_stop` since that shadows Thread._stop
        name = "subshell-parent" if subshell_id is None else f"subshell-{subshell_id}"
        self.thread = None if not run_in_thread else threading.Thread(target=self._run_loop, daemon=True, name=name)
        self.loop = None
        self.loop_ready = threading.Event()
        self.inbox = ThreadBoundAsyncQueue()
        self.aborting = False
        self.abort_handle = None
        self.async_lock = None
        self.bridge = KernelBridge(request_input=self.request_input, debug_event_callback=self.send_debug_event,
            zmq_context=self.kernel.context, user_ns=user_ns, use_singleton=use_singleton)
        self.bridge.set_stream_sender(self._send_stream)
        self.bridge.set_display_sender(self._send_display_event)
        self.parent_header_var = contextvars.ContextVar("parent_header", default=None)
        self.parent_idents_var = contextvars.ContextVar("parent_idents", default=None)
        self.executing = threading.Event()
        self.exec_state = ExecState.IDLE
        self.last_exec_state = None
        self.state_lock = threading.Lock()
        self.shell_handlers = dict(kernel_info_request=self._handle_kernel_info, connect_request=self._handle_connect,
            complete_request=self._bridge_handler, inspect_request=self._bridge_handler, history_request=self._handle_history,
            is_complete_request=self._bridge_handler, comm_info_request=self._handle_comm_info, comm_open=self._handle_comm,
            comm_msg=self._handle_comm, comm_close=self._handle_comm, shutdown_request=self.handle_shutdown)

    def start(self):
        if self.thread is not None:
            self.thread.start()
            self.loop_ready.wait()

    def stop(self):
        "Signal subshell loop to stop and wake it."
        self.stop_event.set()
        self.inbox.suppress_late_puts()
        self.inbox.put((subshell_stop, None, None))
        if self.loop is not None and self.loop.is_running(): self.loop.call_soon_threadsafe(self.loop.stop)

    def join(self, timeout:float|None=None):
        if self.thread is not None: self.thread.join(timeout=timeout)

    def submit(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket): self.inbox.put((msg, idents, sock))

    def _set_exec_state(self, state: ExecState):
        with self.state_lock: self.exec_state = state

    def _set_last_exec_state(self, state: ExecState):
        with self.state_lock: self.last_exec_state = state

    def _get_exec_state(self)->ExecState:
        with self.state_lock: return self.exec_state

    def interrupt(self)->bool:
        "Raise KeyboardInterrupt in subshell thread if executing."
        if self.thread is None: return False
        if self._get_exec_state() != ExecState.RUNNING: return False
        self._set_exec_state(ExecState.CANCELLING)
        if self.bridge.cancel_exec_task(self.loop): return True
        thread_id = self.thread.ident
        if thread_id is None: return False
        return _raise_async_exception(thread_id, KeyboardInterrupt)

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

    def send_reply(self, socket: zmq.Socket, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        parent_id = parent.get("header", {}).get("msg_id", "?")[:8]
        dbg(f"REPLY {msg_type} id={parent_id}")
        if socket is self.kernel.control_socket: self.kernel.queue_control_reply(msg_type, content, parent, idents)
        else: self.kernel.queue_shell_reply(msg_type, content, parent, idents)

    def _run_loop(self):
        self._setup_loop()
        try: self.loop.run_forever()
        finally: self._shutdown_loop()

    def run_main(self):
        "Run parent subshell loop in the main thread."
        self._setup_loop()
        try: self.loop.run_forever()
        finally: self._shutdown_loop()

    def _setup_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.inbox.bind(loop)
        self.async_lock = asyncio.Lock()
        self.loop_ready.set()
        loop.create_task(self._consume_queue())

    def _shutdown_loop(self):
        if self.loop is None: return
        loop = self.loop
        asyncio.set_event_loop(loop)
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending: task.cancel()
        if pending: loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        if hasattr(loop, "shutdown_asyncgens"): loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
        self.loop = None
        self.async_lock = None

    async def _consume_queue(self):
        dbg(f"SUBSHELL started id={self.subshell_id}")
        while True:
            msg, idents, sock = await self.inbox.get()
            if msg is subshell_stop:
                dbg("SUBSHELL stopping")
                return
            if msg is subshell_abort_clear:
                self._stop_aborting()
                continue
            if not msg: continue
            msg_type = msg.get("header", {}).get("msg_type", "?")
            msg_id = msg.get("header", {}).get("msg_id", "?")[:8]
            dbg(f"EXEC {msg_type} id={msg_id}")
            dbg(f"LOCK_WAIT id={msg_id}")
            async with self.async_lock:
                dbg(f"LOCK_ACQ id={msg_id}")
                try: await self._handle_message(msg, idents, sock)
                except Exception as exc: self._handle_internal_error(msg, idents, sock, exc)
            dbg(f"DONE {msg_type} id={msg_id}")

    def _handle_internal_error(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket, exc: Exception):
        msg_type = msg.get("header", {}).get("msg_type", "")
        log.warning("Internal error in %s handler", msg_type, exc_info=exc)
        if not msg_type.endswith("_request"): return
        reply_type = msg_type.replace("_request", "_reply")
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        reply = dict(status="error", ename=type(exc).__name__, evalue=str(exc), traceback=tb)
        if msg_type == "execute_request":
            reply |= dict(execution_count=self.bridge.shell.execution_count, user_expressions={}, payload=[])
            self.kernel.send_status("busy", msg)
            self.kernel.iopub.error(msg, ename=type(exc).__name__, evalue=str(exc), traceback=tb)
        else: reply |= missing_defaults.get(msg_type, {})
        self.send_reply(sock, reply_type, reply, msg, idents)
        if msg_type == "execute_request": self.kernel.send_status("idle", msg)

    async def _handle_message(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        msg_type = msg["header"]["msg_type"]
        msg_id = msg["header"].get("msg_id", "?")[:8]
        dbg(f"HANDLE_MSG {msg_type} id={msg_id}")
        token_header = self.parent_header_var.set(msg)
        token_idents = self.parent_idents_var.set(idents)
        try:
            missing = self._missing_fields(msg_type, msg.get("content", {}))
            if missing:
                self._send_missing_fields_reply(msg_type, missing, msg, idents, sock)
                return
            if msg_type == "execute_request" and self.aborting:
                dbg(f"ABORTING id={msg_id}")
                self._send_abort_reply(sock, msg, idents)
                return
            if msg_type == "execute_request":
                dbg(f"DISPATCH_EXEC id={msg_id}")
                await self._handle_execute(msg, idents, sock)
                return
            self._dispatch_shell_non_execute(msg, idents, sock)
        finally:
            self.parent_header_var.reset(token_header)
            self.parent_idents_var.reset(token_idents)

    def _missing_fields(self, msg_type:str, content: dict)->list[str]:
        required = shell_required.get(msg_type)
        return [key for key in required if key not in content] if required else []

    def _send_missing_fields_reply(self, msg_type:str, missing: list[str], msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        reply_type = msg_type.replace("_request", "_reply")
        evalue = f"missing required fields: {', '.join(missing)}"
        reply = dict(status="error", ename="MissingField", evalue=evalue, traceback=[])
        if msg_type == "execute_request":
            reply |= dict(execution_count=self.bridge.shell.execution_count, user_expressions={}, payload=[])
            self.kernel.send_status("busy", msg)
            self.kernel.iopub.error(msg, ename="MissingField", evalue=evalue, traceback=[])
        else: reply |= missing_defaults.get(msg_type, {})
        self.send_reply(sock, reply_type, reply, msg, idents)
        if msg_type == "execute_request": self.kernel.send_status("idle", msg)

    def _send_abort_reply(self, sock: zmq.Socket, msg: dict, idents: list[bytes]|None):
        self.kernel.send_status("busy", msg)
        self._set_last_exec_state(ExecState.ABORTED)
        reply_content = dict(status="aborted", execution_count=self.bridge.shell.execution_count, user_expressions={}, payload=[])
        self.send_reply(sock, "execute_reply", reply_content, msg, idents)
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

    def _dispatch_shell_non_execute(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        msg_type = msg["header"]["msg_type"]
        handler = self.shell_handlers.get(msg_type)
        if handler is None:
            self.send_reply(sock, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, sock)

    def _abort_pending_executes(self, append_abort_clear: bool = False):
        drained = self.inbox.drain_nowait()
        if append_abort_clear: self.inbox.put((subshell_abort_clear, None, None))
        for msg, idents, sock in drained:
            if msg is subshell_stop:
                self.inbox.put((msg, idents, sock))
                break
            if msg is subshell_abort_clear:
                self.inbox.put((msg, idents, sock))
                continue
            msg_type = msg.get("header", {}).get("msg_type")
            if msg_type == "execute_request":
                self.kernel.send_status("busy", msg)
                self._set_last_exec_state(ExecState.ABORTED)
                reply_content = dict(status="aborted", execution_count=self.bridge.shell.execution_count,
                    user_expressions={}, payload=[])
                self.send_reply(sock, "execute_reply", reply_content, msg, idents)
                self.kernel.send_status("idle", msg)
            elif msg_type: self._dispatch_shell_non_execute(msg, idents, sock)

    def _handle_kernel_info(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        with self.kernel.busy_idle(msg):
            content = self.kernel.kernel_info_content()
            self.send_reply(sock, "kernel_info_reply", content, msg, idents)

    def _handle_connect(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        content = dict(shell_port=self.kernel.connection.shell_port, iopub_port=self.kernel.connection.iopub_port,
            stdin_port=self.kernel.connection.stdin_port, control_port=self.kernel.connection.control_port, hb_port=self.kernel.connection.hb_port)
        self.send_reply(sock, "connect_reply", content, msg, idents)

    async def _handle_execute(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        msg_id = msg.get("header", {}).get("msg_id", "?")[:8]
        content = msg.get("content", {})
        code = content.get("code", "")
        silent = bool(content.get("silent", False))
        store_history = bool(content.get("store_history", True))
        stop_on_error = bool(content.get("stop_on_error", True))
        user_expressions = content.get("user_expressions", {})
        allow_stdin = bool(content.get("allow_stdin", False))

        dbg(f"HANDLE_EXEC id={msg_id} code={code[:30]!r}...")
        self._set_exec_state(ExecState.RUNNING)
        self.executing.set()
        terminal_state = ExecState.COMPLETED
        iopub = self.kernel.iopub
        sent_reply = sent_error = False
        exec_count = None

        self.kernel.send_status("busy", msg)
        try:
            exec_count_pre = self.bridge.shell.execution_count
            if not silent: iopub.execute_input(msg, code=code, execution_count=exec_count_pre)

            dbg(f"BRIDGE_EXEC id={msg_id}")
            timeout_handle = None
            if _debug:
                loop = asyncio.get_running_loop()
                timeout_handle = loop.call_later(2.0, lambda: log.warning("execute still running msg_id=%s", msg_id))
            try:
                with comm_context(iopub.send, msg):
                    result = await self.bridge.execute(code, silent=silent, store_history=store_history,
                        user_expressions=user_expressions, allow_stdin=allow_stdin)
            finally:
                if timeout_handle: timeout_handle.cancel()
            dbg(f"BRIDGE_DONE id={msg_id}")

            error = result.get("error")
            if error and error.get("ename") == "CancelledError" and self._get_exec_state() == ExecState.CANCELLING:
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
            self.send_reply(sock, "execute_reply", reply, msg, idents)
            sent_reply = True

            if error and stop_on_error:
                self._abort_pending_executes(append_abort_clear=self.kernel.stop_on_error_timeout <= 0)
                self._start_aborting()
        except KeyboardInterrupt as exc:
            terminal_state = ExecState.ABORTED
            error = dict(ename=type(exc).__name__, evalue=str(exc), traceback=traceback.format_exception(type(exc), exc, exc.__traceback__))
            if not sent_error: iopub.error(msg, content=error)
            if not sent_reply:
                reply = dict(status="error", execution_count=exec_count or self.bridge.shell.execution_count, user_expressions={}, payload=[])
                reply.update(error)
                self.send_reply(sock, "execute_reply", reply, msg, idents)
                if stop_on_error:
                    self._abort_pending_executes(append_abort_clear=self.kernel.stop_on_error_timeout <= 0)
                    self._start_aborting()
        finally:
            self.kernel.send_status("idle", msg)
            self._set_last_exec_state(terminal_state)
            self.executing.clear()
            self._set_exec_state(ExecState.IDLE)

    def _bridge_handler(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        msg_type = msg.get("header", {}).get("msg_type")
        spec = bridge_specs.get(msg_type)
        if spec is None: return
        reply_type, fields, method = spec
        self._bridge_reply(msg, idents, sock, reply_type, method, **fields)

    def _handle_history(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        content = msg.get("content", {})
        reply = self.bridge.history(content.get("hist_access_type", ""), bool(content.get("output", False)),
            bool(content.get("raw", False)), session=int(content.get("session", 0)), start=int(content.get("start", 0)),
            stop=content.get("stop"), n=content.get("n"), pattern=content.get("pattern"), unique=bool(content.get("unique", False)))
        self.send_reply(sock, "history_reply", reply, msg, idents)

    def _bridge_reply(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket, reply_type:str, method:str, **fields):
        content = msg.get("content", {})
        args = {}
        for name, spec in fields.items():
            if isinstance(spec, tuple): key, default = spec
            else: key, default = spec, None
            args[name] = content.get(key, default)
        reply = getattr(self.bridge, method)(**args)
        self.send_reply(sock, reply_type, reply, msg, idents)

    def _handle_comm_info(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        content = msg.get("content", {})
        target_name = content.get("target_name")
        manager = get_comm_manager()
        comms = {comm_id: dict(target_name=comm.target_name) for comm_id, comm in manager.comms.items()
            if target_name is None or comm.target_name == target_name}
        reply = dict(status="ok", comms=comms)
        self.send_reply(sock, "comm_info_reply", reply, msg, idents)

    def _handle_comm(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
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
        self.user_ns = {}
        self.parent = Subshell(kernel, None, self.user_ns, use_singleton=True, run_in_thread=False)
        self.subs = {}
        self.lock = threading.Lock()

    def start(self): return

    def get(self, subshell_id:str|None)->Subshell|None:
        "Return subshell by id, or the parent when None."
        if subshell_id is None: return self.parent
        with self.lock: return self.subs.get(subshell_id)

    def create(self)->str:
        "Create and start a new subshell; return its id."
        subshell_id = str(uuid.uuid4())
        subshell = Subshell(self.kernel, subshell_id, self.user_ns)
        with self.lock: self.subs[subshell_id] = subshell
        subshell.start()
        return subshell_id

    def list(self)->list[str]:
        with self.lock: return list(self.subs.keys())

    def delete(self, subshell_id:str):
        "Stop and remove a subshell by id."
        with self.lock: subshell = self.subs.pop(subshell_id)
        subshell.stop()
        subshell.join(timeout=1)

    def stop_all(self):
        "Stop all subshells and the parent."
        with self.lock:
            subshells = list(self.subs.values())
            self.subs.clear()
        for subshell in subshells:
            subshell.stop()
            subshell.join(timeout=1)
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
        self.parent_header = None
        self.parent_idents = None
        self.shell_router = None
        self.control_router = None
        self.shutdown_event = threading.Event()
        self.stop_on_error_timeout = _env_float("IPYMINI_STOP_ON_ERROR_TIMEOUT", 0.0)
        self.iopub_cmd = None
        self.control_handlers = dict(shutdown_request=self.handle_shutdown, debug_request=self.handle_debug,
            interrupt_request=self.handle_interrupt)
        self.control_specs = dict(kernel_info_request=("kernel_info_reply", lambda msg: self.kernel_info_content()),
            create_subshell_request=("create_subshell_reply", lambda msg: dict(subshell_id=self.subshells.create())),
            list_subshell_request=("list_subshell_reply", lambda msg: dict(subshell_id=self.subshells.list())),
            delete_subshell_request=("delete_subshell_reply", self._delete_subshell))

    def start(self):
        "Start kernel threads and serve shell/control messages."
        _dbg_mod.setup()
        dbg("kernel starting...")
        prev_hook = _install_thread_excepthook(self)
        self.iopub_thread.start()
        self.stdin_router.start()
        self.subshells.start()
        self.hb.start()
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self.handle_sigint)
        self.shutdown_event.clear()
        self.shell_router = AsyncRouterThread(self, self.connection.shell_port, "shell_socket", self.handle_shell_msg, "shell")
        self.control_router = AsyncRouterThread(self, self.connection.control_port, "control_socket", self.handle_control_msg, "control")
        self.shell_router.start()
        self.control_router.start()
        dbg("waiting for routers to be ready...")
        self.shell_router.ready.wait()
        self.control_router.ready.wait()
        dbg("kernel ready")
        try: self.subshells.parent.run_main()
        finally:
            threading.excepthook = prev_hook
            self.shutdown_event.set()
            if self.shell_router is not None: self.shell_router.stop()
            if self.control_router is not None: self.control_router.stop()
            if self.shell_router is not None: self.shell_router.join(timeout=1)
            if self.control_router is not None: self.control_router.join(timeout=1)
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
            if "Duplicate Signature" not in str(err): log.warning("Bad message signature", exc_info=True)
        except zmq.ZMQError: pass
        return None, None

    async def recv_msg_async(self, sock: zmq.asyncio.Socket):
        "Receive message from async `sock`."
        try: msg_list = await sock.recv_multipart(copy=False)
        except zmq.ZMQError as e:
            dbg(f"recv_msg_async ZMQError: {e}")
            return None, None
        idents, msg_list = self.session.feed_identities(msg_list, copy=False)
        try: msg = self.session.deserialize(msg_list, content=True, copy=False)
        except ValueError as err:
            dbg(f"recv_msg_async deserialize error: {err}")
            if "Duplicate Signature" not in str(err): log.warning("Bad message signature", exc_info=True)
            return None, None
        return idents, msg

    def handle_sigint(self, signum, frame):
        "Handle SIGINT by interrupting subshells and input waits."
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()
        if self.subshells.parent.executing.is_set():
            self.subshells.parent._set_exec_state(ExecState.CANCELLING)
            raise KeyboardInterrupt

    def handle_control_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle control channel request message."
        msg_type = msg["header"]["msg_type"]
        spec = self.control_specs.get(msg_type)
        if spec is not None:
            reply_type, fn = spec
            try:
                extra = fn(msg) or {}
                content = dict(status="ok") | extra
            except Exception as exc: content = dict(status="error", evalue=str(exc))
            self.send_reply(self.control_socket, reply_type, content, msg, idents)
            return
        handler = self.control_handlers.get(msg_type)
        if handler is None:
            self.send_reply(self.control_socket, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, self.control_socket)

    def handle_shell_msg(self, msg: dict, idents: list[bytes]|None):
        "Handle shell request message and dispatch to subshells."
        msg_type = msg.get("header", {}).get("msg_type", "?")
        msg_id = msg.get("header", {}).get("msg_id", "?")[:8]
        subshell_id = msg.get("header", {}).get("subshell_id") or None  # treat "" as None
        subshell = self.subshells.get(subshell_id)
        if subshell is None:
            dbg(f"DISPATCH {msg_type} id={msg_id} -> NO SUBSHELL")
            self.send_subshell_error(msg, idents)
            return
        dbg(f"DISPATCH {msg_type} id={msg_id}")
        subshell.submit(msg, idents, self.shell_socket)

    def send_reply(self, socket: zmq.Socket, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        self.session.send(socket, msg_type, content, parent=parent, ident=idents)

    def queue_shell_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        "Enqueue a shell reply for async send."
        if self.shell_router is None:
            dbg("QUEUE shell reply - NO ROUTER!")
            return
        _dbg_mod.tlog(log, "shell reply", parent)
        self.shell_router.enqueue((msg_type, content, parent, idents))

    def queue_control_reply(self, msg_type:str, content: dict, parent: dict, idents: list[bytes]|None):
        "Enqueue a control reply for async send."
        if self.control_router is None: return
        self.control_router.enqueue((msg_type, content, parent, idents))

    def send_status(self, state:str, parent: dict|None):
        if _dbg_mod.trace_msgs and parent: _dbg_mod.tlog(log, f"iopub status={state}", parent)
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

    def send_subshell_error(self, msg: dict, idents: list[bytes]|None):
        "Send SubshellNotFound error reply for unknown subshell. Always sends busy/idle for execute_request."
        msg_type = msg.get("header", {}).get("msg_type", "")
        subshell_id = msg.get("header", {}).get("subshell_id")
        if not msg_type.endswith("_request"): return
        content = dict(status="error", ename="SubshellNotFound", evalue=f"Unknown subshell_id {subshell_id!r}", traceback=[])
        if msg_type == "execute_request":
            content.update(dict(execution_count=0, user_expressions={}, payload=[]))
            self.send_status("busy", msg)
            self.iopub.error(msg, ename="SubshellNotFound", evalue=content["evalue"], traceback=[])
        self.send_reply(self.shell_socket, msg_type.replace("_request", "_reply"), content, msg, idents)
        if msg_type == "execute_request": self.send_status("idle", msg)

    def _delete_subshell(self, msg: dict)->dict:
        subshell_id = msg.get("content", {}).get("subshell_id")
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
                nbconvert_exporter="python"), banner="ipymini", help_links=[], supported_features=supported_features)

    def python_version(self)->str: return ".".join(str(x) for x in os.sys.version_info[:3])

    def handle_debug(self, msg: dict, idents: list[bytes]|None, sock: zmq.Socket):
        "Handle debug_request via KernelBridge and emit events."
        self.send_status("busy", msg)
        self.parent_header = msg
        try:
            content = msg.get("content", {})
            reply = self.bridge.debug_request(json.dumps(content))
            response = reply.get("response", {})
            events = reply.get("events", [])
            self.send_reply(sock, "debug_reply", response, msg, idents)
            for event in events: self.send_debug_event(event)
            self.send_status("idle", msg)
        finally: self.parent_header = None

    def send_interrupt_signal(self):
        "Send SIGINT to the current process or process group."
        if os.name == "nt":
            log.warning("Interrupt request not supported on Windows")
            return
        pid = os.getpid()
        try: pgid = os.getpgid(pid)
        except OSError: pgid = None
        try:
            # Only signal the process group if we're the leader; otherwise avoid killing unrelated processes.
            if pgid and pgid == pid and hasattr(os, "killpg"): os.killpg(pgid, signal.SIGINT)
            else: os.kill(pid, signal.SIGINT)
        except OSError as err: log.warning("Interrupt signal failed: %s", err)

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
        self.shutdown_event.set()
        self.subshells.parent.stop()


def run_kernel(connection_file:str):
    "Run kernel given a connection file path."
    signal.signal(signal.SIGINT, signal.default_int_handler)
    kernel = MiniKernel(connection_file)
    kernel.start()
