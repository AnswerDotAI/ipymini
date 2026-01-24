import asyncio, json, logging, os, queue, signal, threading, traceback, time, uuid
from collections import deque
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from fastcore.basics import store_attr
import zmq
from jupyter_client.session import Session
from .bridge import KernelBridge
from .comms import comm_context, get_comm_manager

_LOG = logging.getLogger("ipymini.stdin")
_SHELL_SEND_STOP = object()
_SUBSHELL_STOP = object()

@dataclass
class ConnectionInfo:
    transport: str
    ip: str
    shell_port: int
    iopub_port: int
    stdin_port: int
    control_port: int
    hb_port: int
    key: str
    signature_scheme: str

    @classmethod
    def from_file(cls, path: str) -> "ConnectionInfo":
        "Load connection info from JSON connection file at `path`."
        with open(path, encoding="utf-8") as f: data = json.load(f)
        return cls(transport=data["transport"], ip=data["ip"], shell_port=int(data["shell_port"]),
            iopub_port=int(data["iopub_port"]), stdin_port=int(data["stdin_port"]), control_port=int(data["control_port"]),
            hb_port=int(data["hb_port"]), key=data.get("key", ""), signature_scheme=data.get("signature_scheme", "hmac-sha256"))

    def addr(self, port: int) -> str: return f"{self.transport}://{self.ip}:{port}"


def _raise_async_exception(thread_id: int, exc_type: type[BaseException]) -> bool:
    "Inject `exc_type` into a thread by id; returns success."
    try: import ctypes
    except Exception: return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(exc_type))
    if res == 0: return False
    if res > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        return False
    return True

class HeartbeatThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr: str) -> None:
        "Initialize heartbeat thread bound to `addr`."
        super().__init__(daemon=True)
        store_attr()
        self._stop_event = threading.Event()

    def run(self) -> None:
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

    def stop(self) -> None: self._stop_event.set()


class IOPubThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr: str, session: Session) -> None:
        "Initialize IOPub sender thread."
        super().__init__(daemon=True)
        store_attr()
        self.queue = queue.Queue()
        self._stop_event = threading.Event()
        self._socket = None

    def send(self, msg_type: str, content: dict, parent: dict | None, metadata: dict | None = None,
        ident: bytes | list[bytes] | None = None, buffers: list[bytes | memoryview] | None = None) -> None:
        "Queue an IOPub message for async send."
        self.queue.put((msg_type, content, parent, metadata, ident, buffers))

    def run(self) -> None:
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

    def _drain_queue(self) -> None:
        "Send queued IOPub messages to the socket."
        if self._socket is None: return
        while True:
            try: msg_type, content, parent, metadata, ident, buffers = self.queue.get_nowait()
            except queue.Empty: break
            self.session.send(self._socket, msg_type, content, parent=parent, metadata=metadata, ident=ident,
                buffers=buffers)

    def stop(self) -> None: self._stop_event.set()


_INPUT_INTERRUPTED = object()


class StdinRouterThread(threading.Thread):
    def __init__(self, context: zmq.Context, addr: str, session: Session) -> None:
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

    def request_input( self, prompt: str, password: bool, parent: dict | None,
        ident: list[bytes] | None, timeout: float | None = None,) -> str:
        "Send input_request and wait for input_reply; honors `timeout`."
        response_queue = queue.Queue()
        self._requests.put((prompt, password, parent, ident, response_queue))
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if self._interrupt_event.is_set():
                self._interrupt_event.clear()
                raise KeyboardInterrupt
            if self._stop_event.is_set(): raise RuntimeError("stdin router stopped")
            try:
                if deadline is None: value = response_queue.get(timeout=0.1)
                else:
                    remaining = max(0.0, deadline - time.time())
                    if remaining == 0.0: raise TimeoutError("timed out waiting for input reply")
                    value = response_queue.get(timeout=min(0.1, remaining))
                if value is _INPUT_INTERRUPTED:
                    self._interrupt_event.clear()
                    raise KeyboardInterrupt
                return value
            except queue.Empty: continue

    def run(self) -> None:
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

    def _drain_requests(self, sock: zmq.Socket) -> None:
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

    def stop(self) -> None: self._stop_event.set()

    def interrupt_pending(self) -> None:
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


class Subshell:
    def __init__(self, kernel: "MiniKernel", subshell_id: str | None, user_ns: dict,
        use_singleton: bool = False) -> None:
        "Create subshell worker thread and bridge for execution."
        store_attr("kernel,subshell_id")
        self._queue = queue.Queue()
        self._stop = threading.Event()
        name = "subshell-parent" if subshell_id is None else f"subshell-{subshell_id}"
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self.bridge = KernelBridge(request_input=self.request_input, debug_event_callback=self._send_debug_event,
            zmq_context=self.kernel.context, user_ns=user_ns, use_singleton=use_singleton)
        self.bridge.set_stream_sender(self._send_stream)
        self._parent_header = None
        self._parent_idents = None
        self._executing = threading.Event()
        self._shell_handlers = dict(kernel_info_request=self._handle_kernel_info, connect_request=self._handle_connect,
            complete_request=self._handle_complete, inspect_request=self._handle_inspect, history_request=self._handle_history,
            is_complete_request=self._handle_is_complete, comm_info_request=self._handle_comm_info, comm_open=self._handle_comm_open,
            comm_msg=self._handle_comm_msg, comm_close=self._handle_comm_close, shutdown_request=self._handle_shutdown)

    def start(self) -> None: self._thread.start()

    def stop(self) -> None:
        "Signal subshell thread to stop and wake it."
        self._stop.set()
        self._queue.put_nowait(_SUBSHELL_STOP)

    def join(self, timeout: float | None = None) -> None: self._thread.join(timeout=timeout)

    def submit(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None: self._queue.put((msg, idents, sock))

    def interrupt(self) -> bool:
        "Raise KeyboardInterrupt in subshell thread if executing."
        if not self._executing.is_set(): return False
        thread_id = self._thread.ident
        if thread_id is None: return False
        return _raise_async_exception(thread_id, KeyboardInterrupt)

    def request_input(self, prompt: str, password: bool) -> str:
        "Forward input_request through stdin router for this subshell."
        try:
            if os.sys.stdout is not None: os.sys.stdout.flush()
            if os.sys.stderr is not None: os.sys.stderr.flush()
        except Exception: pass
        return self.kernel.stdin_router.request_input(prompt, password, self._parent_header, self._parent_idents)

    def _send_stream(self, name: str, text: str) -> None:
        "Send stream output on IOPub for current parent message."
        if not self._parent_header: return
        self.kernel._iopub_send("stream", {"name": name, "text": text}, self._parent_header)

    def _send_debug_event(self, event: dict) -> None:
        "Send debug_event on IOPub for current parent message."
        parent = self._parent_header or {}
        self.kernel._iopub_send("debug_event", event, parent)

    def _send_status(self, state: str, parent: dict | None) -> None:
        self.kernel._iopub_send("status", {"execution_state": state}, parent)

    def _send_reply( self, socket: zmq.Socket, msg_type: str, content: dict,
        parent: dict, idents: list[bytes] | None,) -> None:
        self.kernel._queue_shell_reply(msg_type, content, parent, idents)

    def _run(self) -> None:
        "Main subshell loop: read queue and dispatch messages."
        while True:
            item = self._queue.get()
            if item is _SUBSHELL_STOP: break
            msg, idents, sock = item
            if not msg: continue
            self._handle_message(msg, idents, sock)

    def _handle_message(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Dispatch message based on msg_type, handling execute separately."
        msg_type = msg["header"]["msg_type"]
        if msg_type == "execute_request":
            self._handle_execute(msg, idents, sock)
            return
        self._dispatch_shell_non_execute(msg, idents, sock)

    def _dispatch_shell_non_execute(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Dispatch non-execute shell requests to handlers."
        msg_type = msg["header"]["msg_type"]
        handler = self._shell_handlers.get(msg_type)
        if handler is None:
            self._send_reply(sock, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, sock)

    def _abort_pending_executes(self) -> None:
        "Abort queued execute requests and reply aborted."
        drained = []
        while True:
            try: drained.append(self._queue.get_nowait())
            except queue.Empty: break
        for msg, idents, sock in drained:
            msg_type = msg.get("header", {}).get("msg_type")
            if msg_type == "execute_request":
                reply_content = dict(status="aborted", execution_count=self.bridge.shell.execution_count,
                    user_expressions={}, payload=[])
                self._send_reply(sock, "execute_reply", reply_content, msg, idents)
            elif msg_type: self._dispatch_shell_non_execute(msg, idents, sock)

    def _handle_kernel_info(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to kernel_info_request."
        self._send_status("busy", msg)
        content = self.kernel._kernel_info_content()
        self._send_reply(sock, "kernel_info_reply", content, msg, idents)
        self._send_status("idle", msg)

    def _handle_connect(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to connect_request with port numbers."
        content = dict(shell_port=self.kernel.connection.shell_port, iopub_port=self.kernel.connection.iopub_port,
            stdin_port=self.kernel.connection.stdin_port, control_port=self.kernel.connection.control_port, hb_port=self.kernel.connection.hb_port)
        self._send_reply(sock, "connect_reply", content, msg, idents)

    def _handle_execute(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle execute_request via KernelBridge and emit IOPub."
        content = msg.get("content", {})
        code = content.get("code", "")
        silent = bool(content.get("silent", False))
        store_history = bool(content.get("store_history", True))
        stop_on_error = bool(content.get("stop_on_error", True))
        user_expressions = content.get("user_expressions", {})
        allow_stdin = bool(content.get("allow_stdin", False))

        self._parent_header = msg
        self._parent_idents = idents
        self._executing.set()
        sent_reply = False
        sent_error = False
        exec_count = None

        try:
            self._send_status("busy", msg)
            try:
                with comm_context(self.kernel._iopub_send, msg):
                    result = self.bridge.execute(code, silent=silent, store_history=store_history,
                        user_expressions=user_expressions, allow_stdin=allow_stdin)
            except BaseException as exc:
                result = dict(streams=[], display=[], result=None, result_metadata={}, execution_count=self.bridge.shell.execution_count,
                    error=dict(ename=type(exc).__name__, evalue=str(exc), traceback=traceback.format_exception(type(exc), exc, exc.__traceback__)),
                    user_expressions={}, payload=[])

            exec_count = result.get("execution_count")
            if not silent: self.kernel._iopub_send("execute_input", {"code": code, "execution_count": exec_count}, msg)

            error = result.get("error")
            if not silent:
                for stream in result.get("streams", []):
                    self.kernel._iopub_send( "stream", {"name": stream["name"], "text": stream["text"]}, msg,)
                for event in result.get("display", []):
                    if event.get("type") == "clear_output":
                        self.kernel._iopub_send( "clear_output", {"wait": event.get("wait", False)}, msg,)
                        continue
                    if event.get("type") == "display":
                        msg_type = "update_display_data" if event.get("update") else "display_data"
                        self.kernel._iopub_send(msg_type, dict(data=event.get("data", {}), metadata=event.get("metadata", {}),
                            transient=event.get("transient", {})), msg, buffers=event.get("buffers"))

            if error:
                self.kernel._iopub_send("error", dict(ename=error["ename"], evalue=error["evalue"],
                    traceback=error.get("traceback", [])), msg)
                sent_error = True

            if not silent and not error and result.get("result") is not None:
                self.kernel._iopub_send("execute_result", dict(execution_count=exec_count, data=result.get("result"),
                    metadata=result.get("result_metadata", {})), msg)

            reply_content = dict(status="ok" if not error else "error", execution_count=exec_count,
                user_expressions=result.get("user_expressions", {}), payload=result.get("payload", []))
            if error: reply_content.update(error)

            self._send_reply(sock, "execute_reply", reply_content, msg, idents)
            sent_reply = True
            if error and stop_on_error: self._abort_pending_executes()
            self._send_status("idle", msg)
        except KeyboardInterrupt as exc:
            error = dict(ename=type(exc).__name__, evalue=str(exc),
                traceback=traceback.format_exception(type(exc), exc, exc.__traceback__))
            if not sent_error: self.kernel._iopub_send("error", error, msg)
            if not sent_reply:
                reply_content = dict(status="error", execution_count=exec_count or self.bridge.shell.execution_count,
                    user_expressions={}, payload=[])
                reply_content.update(error)
                self._send_reply(sock, "execute_reply", reply_content, msg, idents)
                if stop_on_error: self._abort_pending_executes()
            self._send_status("idle", msg)
        finally:
            self._parent_header = None
            self._parent_idents = None
            self._executing.clear()

    def _handle_complete(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to complete_request."
        content = msg.get("content", {})
        reply = self.bridge.complete(content.get("code", ""), content.get("cursor_pos"))
        self._send_reply(sock, "complete_reply", reply, msg, idents)

    def _handle_inspect(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to inspect_request."
        content = msg.get("content", {})
        reply = self.bridge.inspect(content.get("code", ""), content.get("cursor_pos"), content.get("detail_level", 0))
        self._send_reply(sock, "inspect_reply", reply, msg, idents)

    def _handle_history(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to history_request."
        content = msg.get("content", {})
        reply = self.bridge.history(content.get("hist_access_type", ""), bool(content.get("output", False)),
            bool(content.get("raw", False)), session=int(content.get("session", 0)), start=int(content.get("start", 0)),
            stop=content.get("stop"), n=content.get("n"), pattern=content.get("pattern"), unique=bool(content.get("unique", False)))
        self._send_reply(sock, "history_reply", reply, msg, idents)

    def _handle_is_complete(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to is_complete_request."
        content = msg.get("content", {})
        reply = self.bridge.is_complete(content.get("code", ""))
        self._send_reply(sock, "is_complete_reply", reply, msg, idents)

    def _handle_comm_info(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Reply to comm_info_request."
        content = msg.get("content", {})
        target_name = content.get("target_name")
        manager = get_comm_manager()
        comms = {comm_id: dict(target_name=comm.target_name) for comm_id, comm in manager.comms.items()
            if target_name is None or comm.target_name == target_name}
        reply = dict(status="ok", comms=comms)
        self._send_reply(sock, "comm_info_reply", reply, msg, idents)

    def _handle_comm_open(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle comm_open request and broadcast on IOPub."
        content = msg.get("content", {})
        manager = get_comm_manager()
        manager.comm_open(None, None, msg)
        self.kernel._iopub_send("comm_open", content, msg, metadata=msg.get("metadata"), buffers=msg.get("buffers"))

    def _handle_comm_msg(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle comm_msg request and broadcast on IOPub."
        content = msg.get("content", {})
        manager = get_comm_manager()
        manager.comm_msg(None, None, msg)
        self.kernel._iopub_send("comm_msg", content, msg, metadata=msg.get("metadata"), buffers=msg.get("buffers"))

    def _handle_comm_close(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle comm_close request and broadcast on IOPub."
        content = msg.get("content", {})
        manager = get_comm_manager()
        manager.comm_close(None, None, msg)
        self.kernel._iopub_send("comm_close", content, msg, metadata=msg.get("metadata"), buffers=msg.get("buffers"))

    def _handle_shutdown(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        self.kernel._handle_shutdown(msg, idents, sock)


class SubshellManager:
    def __init__(self, kernel: "MiniKernel") -> None:
        "Manage parent and child subshells sharing a user namespace."
        self.kernel = kernel
        self._user_ns = {}
        self.parent = Subshell(kernel, None, self._user_ns, use_singleton=True)
        self._subs = {}
        self._lock = threading.Lock()

    def start(self) -> None: self.parent.start()

    def get(self, subshell_id: str | None) -> Subshell | None:
        "Return subshell by id, or the parent when None."
        if subshell_id is None: return self.parent
        with self._lock: return self._subs.get(subshell_id)

    def create(self) -> str:
        "Create and start a new subshell; return its id."
        subshell_id = str(uuid.uuid4())
        subshell = Subshell(self.kernel, subshell_id, self._user_ns)
        with self._lock: self._subs[subshell_id] = subshell
        subshell.start()
        return subshell_id

    def list(self) -> list[str]:
        with self._lock: return list(self._subs.keys())

    def delete(self, subshell_id: str) -> None:
        "Stop and remove a subshell by id."
        with self._lock: subshell = self._subs.pop(subshell_id)
        subshell.stop()
        subshell.join(timeout=1)

    def stop_all(self) -> None:
        "Stop all subshells and the parent."
        with self._lock:
            subshells = list(self._subs.values())
            self._subs.clear()
        for subshell in subshells:
            subshell.stop()
            subshell.join(timeout=1)
        self.parent.stop()
        self.parent.join(timeout=1)

    def interrupt_all(self) -> None:
        "Send interrupts to all subshells."
        self.parent.interrupt()
        with self._lock: subshells = list(self._subs.values())
        for subshell in subshells: subshell.interrupt()


class MiniKernel:
    def __init__(self, connection_file: str) -> None:
        "Initialize kernel sockets, threads, and subshell manager."
        self.connection = ConnectionInfo.from_file(connection_file)
        key = self.connection.key.encode()
        self.session = Session(key=key, signature_scheme=self.connection.signature_scheme)
        self.context = zmq.Context.instance()
        self.shell_socket = None
        self.control_socket = None
        self.async_context = None
        self.iopub_thread = IOPubThread(self.context, self.connection.addr(self.connection.iopub_port), self.session)
        self.stdin_router = StdinRouterThread(self.context, self.connection.addr(self.connection.stdin_port), self.session)

        self.hb = HeartbeatThread(self.context, self.connection.addr(self.connection.hb_port))
        self.subshells = SubshellManager(self)
        self.bridge = self.subshells.parent.bridge
        self._parent_header = None
        self._parent_idents = None
        self._shell_send_queue_async = None
        self._loop = None
        self._async_shutdown = None
        self._control_handlers = dict(shutdown_request=self._handle_shutdown, debug_request=self._handle_debug,
            interrupt_request=self._handle_interrupt, create_subshell_request=self._handle_create_subshell,
            list_subshell_request=self._handle_list_subshell, delete_subshell_request=self._handle_delete_subshell)

    def start(self) -> None:
        "Start kernel threads and serve shell/control messages."
        asyncio.run(self._start_async())

    async def _start_async(self) -> None:
        "Start kernel with asyncio-based shell/control loops."
        import zmq.asyncio as _zmq_asyncio
        self._loop = asyncio.get_running_loop()
        self.async_context = _zmq_asyncio.Context()
        self.shell_socket = self.async_context.socket(zmq.ROUTER)
        self.control_socket = self.async_context.socket(zmq.ROUTER)
        if hasattr(zmq, "ROUTER_HANDOVER"):
            self.shell_socket.router_handover = 1
            self.control_socket.router_handover = 1
        for sock in (self.shell_socket, self.control_socket): sock.linger = 0
        self.shell_socket.bind(self.connection.addr(self.connection.shell_port))
        self.control_socket.bind(self.connection.addr(self.connection.control_port))
        self._shell_send_queue_async = asyncio.Queue()
        self.iopub_thread.start()
        self.stdin_router.start()
        self.subshells.start()
        self.hb.start()
        prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)
        self._async_shutdown = asyncio.Event()
        shell_task = asyncio.create_task(self._recv_loop(self.shell_socket, self._handle_shell_msg))
        control_task = asyncio.create_task(self._recv_loop(self.control_socket, self._handle_control_msg))
        send_task = asyncio.create_task(self._shell_send_loop())
        try: await self._async_shutdown.wait()
        finally:
            for task in (shell_task, control_task, send_task): task.cancel()
            await asyncio.gather(shell_task, control_task, send_task, return_exceptions=True)
            self.hb.stop()
            self.hb.join(timeout=1)
            self.subshells.stop_all()
            self.stdin_router.stop()
            self.stdin_router.join(timeout=1)
            self.iopub_thread.stop()
            self.iopub_thread.join(timeout=1)
            self._close_sockets()
            signal.signal(signal.SIGINT, prev_sigint)

    async def _recv_loop(self, sock: zmq.Socket, handler) -> None:
        "Async recv loop for a socket using Session.deserialize."
        while not self._async_shutdown.is_set():
            try: frames = await sock.recv_multipart()
            except asyncio.CancelledError: break
            except Exception:
                if self._async_shutdown.is_set(): break
                continue
            try: idents, msg_list = self.session.feed_identities(frames)
            except Exception: continue
            try: msg = self.session.deserialize(msg_list)
            except Exception: continue
            if msg is None: continue
            try: handler(msg, idents)
            except Exception: _LOG.warning("Error handling message: %s", msg.get("msg_type"), exc_info=True)

    async def _shell_send_loop(self) -> None:
        "Async loop to drain shell replies from the thread queue."
        if self._shell_send_queue_async is None: return
        while not self._async_shutdown.is_set():
            item = await self._shell_send_queue_async.get()
            if item is _SHELL_SEND_STOP: break
            msg_type, content, parent, idents = item
            if self.shell_socket is None: continue
            self.session.send(self.shell_socket, msg_type, content, parent=parent, ident=idents)

    def _close_sockets(self) -> None:
        for sock in (self.shell_socket, self.control_socket):
            if sock is not None: sock.close(0)
        if self.async_context is not None:
            try: self.async_context.term()
            except Exception: pass

    def _handle_sigint(self, signum, frame) -> None:
        "Handle SIGINT by interrupting subshells and input waits."
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()

    def _handle_control_msg(self, msg: dict, idents: list[bytes] | None) -> None:
        "Handle control channel request message."
        msg_type = msg["header"]["msg_type"]
        handler = self._control_handlers.get(msg_type)
        if handler is None:
            self._send_reply(self.control_socket, msg_type.replace("_request", "_reply"), {}, msg, idents)
            return
        handler(msg, idents, self.control_socket)

    def _handle_shell_msg(self, msg: dict, idents: list[bytes] | None) -> None:
        "Handle shell request message and dispatch to subshells."
        subshell_id = msg.get("header", {}).get("subshell_id")
        subshell = self.subshells.get(subshell_id)
        if subshell is None:
            self._send_subshell_error(msg, idents)
            return
        subshell.submit(msg, idents, self.shell_socket)

    def _send_reply(self, socket: zmq.Socket, msg_type: str, content: dict, parent: dict,
        idents: list[bytes] | None) -> None:
        self.session.send(socket, msg_type, content, parent=parent, ident=idents)

    def _queue_shell_reply(self, msg_type: str, content: dict, parent: dict, idents: list[bytes] | None) -> None:
        "Enqueue a shell reply for async send."
        payload = (msg_type, content, parent, idents)
        if self._loop is None or self._shell_send_queue_async is None: return
        try: self._loop.call_soon_threadsafe(self._shell_send_queue_async.put_nowait, payload)
        except RuntimeError: pass

    def _send_status(self, state: str, parent: dict | None) -> None: self._iopub_send("status", {"execution_state": state}, parent)

    def _send_debug_event(self, event: dict) -> None:
        "Send a debug_event on IOPub using current parent header."
        parent = self._parent_header or {}
        self._iopub_send("debug_event", event, parent)

    def _iopub_send(self, msg_type: str, content: dict, parent: dict | None, metadata: dict | None = None,
        ident: bytes | list[bytes] | None = None, buffers: list[bytes | memoryview] | None = None) -> None:
        "Queue an IOPub message with optional metadata and buffers."
        self.iopub_thread.send(msg_type, content, parent, metadata, ident, buffers)

    def _send_subshell_error(self, msg: dict, idents: list[bytes] | None) -> None:
        "Send SubshellNotFound error reply for unknown subshell."
        msg_type = msg.get("header", {}).get("msg_type", "")
        subshell_id = msg.get("header", {}).get("subshell_id")
        if not msg_type.endswith("_request"): return
        content = dict(status="error", ename="SubshellNotFound", evalue=f"Unknown subshell_id {subshell_id!r}", traceback=[])
        if msg_type == "execute_request": content.update(dict(execution_count=0, user_expressions={}, payload=[]))
        self._send_reply(self.shell_socket, msg_type.replace("_request", "_reply"), content, msg, idents)

    def _handle_create_subshell(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle create_subshell_request."
        try:
            subshell_id = self.subshells.create()
            content = {"status": "ok", "subshell_id": subshell_id}
        except Exception as exc: content = {"status": "error", "evalue": str(exc)}
        self._send_reply(sock, "create_subshell_reply", content, msg, idents)

    def _handle_list_subshell(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle list_subshell_request."
        try: content = {"status": "ok", "subshell_id": self.subshells.list()}
        except Exception as exc: content = {"status": "error", "evalue": str(exc)}
        self._send_reply(sock, "list_subshell_reply", content, msg, idents)

    def _handle_delete_subshell(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle delete_subshell_request."
        try:
            subshell_id = msg.get("content", {}).get("subshell_id")
            if not isinstance(subshell_id, str): raise ValueError("subshell_id required")
            self.subshells.delete(subshell_id)
            content = {"status": "ok"}
        except Exception as exc: content = {"status": "error", "evalue": str(exc)}
        self._send_reply(sock, "delete_subshell_reply", content, msg, idents)

    def _kernel_info_content(self) -> dict:
        "Build kernel_info_reply content."
        try: impl_version = version("ipymini")
        except PackageNotFoundError: impl_version = "0.0.0+local"
        supported_features = ["kernel subshells"]
        if self.bridge.debug_available(): supported_features.append("debugger")
        return dict(status="ok", protocol_version="5.3", implementation="ipymini", implementation_version=impl_version,
            language_info=dict(name="python", version=self._python_version(), mimetype="text/x-python",
                file_extension=".py", pygments_lexer="python", codemirror_mode={"name": "ipython", "version": 3},
                nbconvert_exporter="python"), banner="ipymini", help_links=[], supported_features=supported_features)

    def _python_version(self) -> str: return ".".join(str(x) for x in os.sys.version_info[:3])

    def _handle_debug(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle debug_request via KernelBridge and emit events."
        self._send_status("busy", msg)
        self._parent_header = msg
        try:
            content = msg.get("content", {})
            reply = self.bridge.debug_request(json.dumps(content))
            response = reply.get("response", {})
            events = reply.get("events", [])
            self._send_reply(sock, "debug_reply", response, msg, idents)
            for event in events: self._send_debug_event(event)
            self._send_status("idle", msg)
        finally: self._parent_header = None

    def _send_interrupt_signal(self) -> None:
        "Send SIGINT to the current process or process group."
        if os.name == "nt":
            _LOG.warning("Interrupt request not supported on Windows")
            return
        pid = os.getpid()
        try: pgid = os.getpgid(pid)
        except Exception: pgid = None
        try:
            # Only signal the process group if we're the leader; otherwise avoid killing unrelated processes.
            if pgid and pgid == pid and hasattr(os, "killpg"): os.killpg(pgid, signal.SIGINT)
            else: os.kill(pid, signal.SIGINT)
        except OSError as err: _LOG.warning("Interrupt signal failed: %s", err)

    def _handle_interrupt(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle interrupt_request by signaling subshells."
        self._send_interrupt_signal()
        self.subshells.interrupt_all()
        self.stdin_router.interrupt_pending()
        self._send_reply(sock, "interrupt_reply", {"status": "ok"}, msg, idents)

    def _handle_shutdown(self, msg: dict, idents: list[bytes] | None, sock: zmq.Socket) -> None:
        "Handle shutdown_request and stop main loop."
        content = msg.get("content", {})
        reply = {"status": "ok", "restart": bool(content.get("restart", False))}
        self._send_reply(sock, "shutdown_reply", reply, msg, idents)
        if self._async_shutdown is not None:
            self._async_shutdown.set()
            if self._loop is not None and self._shell_send_queue_async is not None:
                self._loop.call_soon_threadsafe(self._shell_send_queue_async.put_nowait, _SHELL_SEND_STOP)


def run_kernel(connection_file: str) -> None:
    "Run kernel given a connection file path."
    signal.signal(signal.SIGINT, signal.default_int_handler)
    kernel = MiniKernel(connection_file)
    kernel.start()
