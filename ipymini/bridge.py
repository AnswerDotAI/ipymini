import asyncio, builtins, contextvars, getpass, json, logging, os, queue, socket, sys, tempfile, threading
from contextlib import contextmanager
from typing import Callable
import zmq
from fastcore.basics import str2bool
from .murmur2 import DEBUG_HASH_SEED, murmur2_x86

# Ensure debugpy avoids sys.monitoring mode, which can stall kernel threads.
os.environ.setdefault("PYDEVD_USE_SYS_MONITORING", "0")
from IPython.core import getipython as _getipython_mod
from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher
from IPython.core.getipython import get_ipython
from IPython.core.interactiveshell import InteractiveShell
from IPython.core.async_helpers import _asyncio_runner
from IPython.core.shellapp import InteractiveShellApp
from IPython.core.application import BaseIPythonApplication
import debugpy
from IPython.core.completer import provisionalcompleter as _provisionalcompleter
from IPython.core.completer import rectify_completions as _rectify_completions

_EXPERIMENTAL_COMPLETIONS_KEY = "_jupyter_types_experimental"
_LOG = logging.getLogger("ipymini.startup")
_STARTUP_DONE = False


class _ThreadLocalStream:
    def __init__(self, name:str, default):
        "Create a thread-local stream proxy for `name` with `default` fallback."
        self._name = name
        self._default = default

    def _target(self):
        "Return the current thread-local stream or the default."
        target = _IO_STATE.get(self._name)
        return self._default if target is None else target

    def write(self, value)->int:
        "Write `value` to the current stream and return bytes written."
        target = self._target()
        if target is None: return 0
        return target.write(value)

    def writelines(self, lines)->int:
        "Write a sequence of lines to the current stream."
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        "Flush the current stream if it supports `flush()`."
        target = self._target()
        if target is None: return None
        if hasattr(target, "flush"): target.flush()
        return None

    def isatty(self)->bool:
        "Report whether the current stream is a TTY."
        target = self._target()
        if target is None: return False
        return bool(target.isatty()) if hasattr(target, "isatty") else False

_chans = ("shell", "stdout", "stderr", "request_input", "allow_stdin")

class _ThreadLocalIO:
    def __init__(self):
        "Capture original IO hooks and prepare thread-local state."
        self._installed = False
        self._vars = {name: contextvars.ContextVar(f"ipymini.{name}", default=None) for name in _chans}
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        self._orig_input = builtins.input
        self._orig_getpass = getpass.getpass
        self._orig_get_ipython = _getipython_mod.get_ipython

    def install(self):
        "Install thread-local stdout/stderr/input/getpass/get_ipython hooks."
        if self._installed: return
        sys.stdout = _ThreadLocalStream("stdout", self._orig_stdout)
        sys.stderr = _ThreadLocalStream("stderr", self._orig_stderr)
        builtins.input = _thread_local_input
        getpass.getpass = _thread_local_getpass
        _getipython_mod.get_ipython = _thread_local_get_ipython
        self._installed = True

    def get(self, name: str): return self._vars[name].get()

    def push(self, shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool)->dict:
        "Set per-thread IO bindings; returns the previous bindings."
        args = locals()
        prev = {name: self._vars[name].set(args[name]) for name in _chans}
        return prev

    def pop(self, prev: dict):
        "Restore IO bindings from `prev`."
        for name in _chans: self._vars[name].reset(prev[name])


_IO_STATE = _ThreadLocalIO()


def _thread_local_get_ipython():
    "Return thread-local shell or fall back to original get_ipython."
    shell = _IO_STATE.get("shell")
    return shell if shell is not None else _IO_STATE._orig_get_ipython()


def _thread_local_input(prompt:str = "")->str:
    "Route input() through kernel stdin handler using `prompt`."
    handler = _IO_STATE.get("request_input")
    allow = bool(_IO_STATE.get("allow_stdin"))
    if handler is None or not allow:
        msg = "raw_input was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), False)


def _thread_local_getpass(prompt:str = "Password: ", stream=None)->str:
    "Route getpass() through stdin handler using `prompt`."
    handler = _IO_STATE.get("request_input")
    allow = bool(_IO_STATE.get("allow_stdin"))
    if handler is None or not allow:
        msg = "getpass was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), True)


@contextmanager
def _thread_local_io( shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool):
    "Context manager that installs thread-local IO for a request."
    prev = _IO_STATE.push(shell, stdout, stderr, request_input, allow_stdin)
    try: yield
    finally: _IO_STATE.pop(prev)


class DebugpyMessageQueue:
    HEADER = "Content-Length: "
    HEADER_LENGTH = 16
    SEPARATOR = "\r\n\r\n"
    SEPARATOR_LENGTH = 4

    def __init__(self, event_callback, response_callback):
        "Initialize a parser for debugpy TCP frames."
        self.tcp_buffer = ""
        self._reset_tcp_pos()
        self.event_callback = event_callback
        self.response_callback = response_callback

    def _reset_tcp_pos(self):
        "Reset header/size offsets for the TCP buffer."
        self.header_pos = -1
        self.separator_pos = -1
        self.message_size = 0
        self.message_pos = -1

    def _put_message(self, raw_msg:str):
        "Decode a JSON message and dispatch to event/response callback."
        msg = json.loads(raw_msg)
        if msg.get("type") == "event": self.event_callback(msg)
        else: self.response_callback(msg)

    def put_tcp_frame(self, frame:str):
        "Append TCP frame data and emit complete debugpy messages."
        self.tcp_buffer += frame
        while True:
            if self.header_pos == -1: self.header_pos = self.tcp_buffer.find(DebugpyMessageQueue.HEADER)
            if self.header_pos == -1: return

            if self.separator_pos == -1:
                hint = self.header_pos + DebugpyMessageQueue.HEADER_LENGTH
                self.separator_pos = self.tcp_buffer.find(DebugpyMessageQueue.SEPARATOR, hint)
            if self.separator_pos == -1: return

            if self.message_pos == -1:
                size_pos = self.header_pos + DebugpyMessageQueue.HEADER_LENGTH
                self.message_pos = self.separator_pos + DebugpyMessageQueue.SEPARATOR_LENGTH
                self.message_size = int(self.tcp_buffer[size_pos : self.separator_pos])

            if len(self.tcp_buffer) - self.message_pos < self.message_size: return

            self._put_message(self.tcp_buffer[self.message_pos : self.message_pos + self.message_size])

            if len(self.tcp_buffer) - self.message_pos == self.message_size:
                self.tcp_buffer = ""
                self._reset_tcp_pos()
                return

            self.tcp_buffer = self.tcp_buffer[self.message_pos + self.message_size :]
            self._reset_tcp_pos()


class MiniDebugpyClient:
    def __init__(self, context: zmq.Context, event_callback: Callable[[dict], None]|None):
        "Initialize debugpy client state for a ZMQ connection."
        self.context = context
        self.next_seq = 1
        self._event_callback = event_callback
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader_thread = None
        self._initialized = threading.Event()
        self._outgoing = queue.Queue()
        self._routing_id = None
        self._endpoint = None
        self._message_queue = DebugpyMessageQueue(self._handle_event, self._handle_response)

    def connect(self, host:str, port:int):
        "Connect to debugpy adapter at `host:port` and start reader."
        self._endpoint = f"tcp://{host}:{port}"
        self._start_reader()

    def _start_reader(self):
        "Start reader thread if not already running."
        if self._reader_thread and self._reader_thread.is_alive(): return
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def close(self):
        "Stop reader thread and close debugpy socket."
        self._stop.set()
        self._initialized.clear()
        if self._reader_thread: self._reader_thread.join(timeout=1)
        self._reader_thread = None

    def _handle_event(self, msg: dict):
        "Handle debugpy event messages and set init state."
        if msg.get("event") == "initialized": self._initialized.set()
        if self._event_callback: self._event_callback(msg)

    def _handle_response(self, msg: dict):
        "Resolve a pending request from a debugpy response."
        req_seq = msg.get("request_seq")
        if isinstance(req_seq, int):
            with self._pending_lock: waiter = self._pending.get(req_seq)
            if waiter is not None: waiter.put(msg)

    def _reader_loop(self):
        "Read debugpy frames from ZMQ and feed the parser."
        if self._endpoint is None: return
        debugpy.trace_this_thread(False)
        sock = self.context.socket(zmq.STREAM)
        sock.linger = 0
        sock.connect(self._endpoint)
        self._routing_id = sock.getsockopt(zmq.ROUTING_ID)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                self._drain_outgoing(sock)
                events = dict(poller.poll(50))
                if sock in events and events[sock] & zmq.POLLIN:
                    frames = sock.recv_multipart()
                    if len(frames) < 2: continue
                    data = frames[1]
                    if not data: continue
                    text = data.decode("utf-8", errors="replace")
                    self._message_queue.put_tcp_frame(text)
        finally: sock.close(0)

    def _drain_outgoing(self, sock: zmq.Socket):
        "Send queued debugpy requests to the socket."
        if self._routing_id is None: return
        while True:
            try: msg = self._outgoing.get_nowait()
            except queue.Empty: break
            payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            sock.send_multipart([self._routing_id, header + payload])

    def send_request(self, msg: dict, timeout:float = 10.0)->dict:
        "Send a debugpy request and wait for a response."
        req_seq = msg.get("seq")
        if not isinstance(req_seq, int) or req_seq <= 0:
            req_seq = self.next_internal_seq()
            msg["seq"] = req_seq
        req_seq, waiter = self.send_request_async(msg)
        return self.wait_for_response(req_seq, waiter, timeout=timeout)

    def send_request_async(self, msg: dict)->tuple[int, queue.Queue]:
        "Send a request and return `(seq, waiter)` without waiting."
        req_seq = msg.get("seq")
        if not isinstance(req_seq, int) or req_seq <= 0:
            req_seq = self.next_internal_seq()
            msg["seq"] = req_seq
        waiter = queue.Queue()
        with self._pending_lock: self._pending[req_seq] = waiter
        self._outgoing.put(msg)
        return req_seq, waiter

    def wait_for_response(self, req_seq:int, waiter: queue.Queue, timeout:float = 10.0)->dict:
        "Wait for a response on `waiter` until `timeout`."
        try: reply = waiter.get(timeout=timeout)
        except queue.Empty as exc: raise TimeoutError("timed out waiting for debugpy response") from exc
        finally:
            with self._pending_lock: self._pending.pop(req_seq, None)
        return reply

    def wait_initialized(self, timeout:float = 5.0)->bool: return self._initialized.wait(timeout=timeout)

    def next_internal_seq(self)->int:
        "Return the next internal sequence number."
        seq = self.next_seq
        self.next_seq += 1
        return seq


class MiniDebugger:
    def __init__(self, event_callback: Callable[[dict], None]|None=None, *, zmq_context: zmq.Context|None=None,
        kernel_modules: list[str]|None=None, debug_just_my_code:bool=False, filter_internal_frames:bool=True):
        "Initialize DAP handler and debugpy client state."
        self.events = []
        self._event_callback = event_callback
        context = zmq_context or zmq.Context.instance()
        self.client = MiniDebugpyClient(context, self._handle_event)
        self.started = False
        self.host = "127.0.0.1"
        self.port = None
        self.breakpoint_list = {}
        self.stopped_threads = set()
        self._traced_threads = set()
        self._removed_cleanup = {}
        self.kernel_modules = kernel_modules or []
        self.just_my_code = debug_just_my_code
        self.filter_internal_frames = filter_internal_frames

    def _get_free_port(self)->int:
        "Select a free localhost TCP port."
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _ensure_started(self):
        "Start debugpy adapter and connect client once."
        if self.started: return
        if self.port is not None:
            self.client.connect(self.host, self.port)
            self._remove_cleanup_transforms()
            self.started = True
            return
        port = self._get_free_port()
        debugpy.listen((self.host, port))
        self.client.connect(self.host, port)
        self.port = port
        self._remove_cleanup_transforms()
        self.started = True

    def _handle_event(self, msg: dict):
        "Track stopped/continued threads and collect events."
        if msg.get("event") == "stopped":
            thread_id = msg.get("body", {}).get("threadId")
            if isinstance(thread_id, int): self.stopped_threads.add(thread_id)
        elif msg.get("event") == "continued":
            thread_id = msg.get("body", {}).get("threadId")
            if isinstance(thread_id, int): self.stopped_threads.discard(thread_id)
        if self._event_callback: self._event_callback(msg)
        else: self.events.append(msg)

    def process_request(self, request: dict)->tuple[dict, list]:
        "Handle a DAP request and return response plus queued events."
        self.events = []
        command = request.get("command")
        if command == "terminate":
            if self.started: self._reset_session()
            return self._response(request, True, body={}), self.events
        self._ensure_started()
        if "seq" in request: self.client.next_seq = max(self.client.next_seq, int(request["seq"]) + 1)

        simple = dict(configurationDone=lambda r: self._response(r, True, body={}), debugInfo=self._debug_info,
            inspectVariables=self._inspect_variables)
        simple |= dict(richInspectVariables=self._rich_inspect_variables, copyToGlobals=self._copy_to_globals,
            modules=self._modules)
        simple |= dict(source=self._source, dumpCell=self._dump_cell)
        if command in simple: return simple[command](request), self.events

        if command == "attach":
            arguments = request.get("arguments") or {}
            arguments["connect"] = {"host": self.host, "port": self.port}
            arguments["logToFile"] = True
            if not self.just_my_code: arguments["debugOptions"] = ["DebugStdLib"]
            if self.filter_internal_frames and self.kernel_modules:
                arguments["rules"] = [{"path": path, "include": False} for path in self.kernel_modules]
            request["arguments"] = arguments
            req_seq, waiter = self.client.send_request_async(request)
            if self.client.wait_initialized(timeout=10.0):
                config = self._request_payload("configurationDone")
                try: self.client.send_request(config, timeout=10.0)
                except TimeoutError: pass
            response = self.client.wait_for_response(req_seq, waiter, timeout=10.0)
            return response or {}, self.events

        if command == "setBreakpoints":
            response = self.client.send_request(request)
            if response.get("success"):
                src = request.get("arguments", {}).get("source", {}).get("path")
                if src:
                    self.breakpoint_list[src] = [{"line": bp["line"]} for bp in response.get("body", {}).get("breakpoints", [])
                        if isinstance(bp, dict) and "line" in bp]
            return response or {}, self.events

        response = self.client.send_request(request)
        if command == "disconnect" and self.started: self._reset_session()
        return response or {}, self.events

    def _reset_session(self):
        "Reset debugpy client session state."
        self.client.close()
        self.started = False
        self.breakpoint_list = {}
        self.stopped_threads = set()
        self._traced_threads.clear()
        self._restore_cleanup_transforms()

    def trace_current_thread(self):
        "Enable debugpy tracing on the current thread if needed."
        if not self.started: return
        thread_id = threading.get_ident()
        if thread_id in self._traced_threads: return
        debugpy.trace_this_thread(True)
        self._traced_threads.add(thread_id)

    def _remove_cleanup_transforms(self):
        "Temporarily remove IPython cleanup transforms."
        ip = get_ipython()
        if ip is None: return
        from IPython.core.inputtransformer2 import leading_empty_lines
        cleanup_transforms = ip.input_transformer_manager.cleanup_transforms
        if leading_empty_lines in cleanup_transforms:
            index = cleanup_transforms.index(leading_empty_lines)
            self._removed_cleanup[index] = cleanup_transforms.pop(index)

    def _restore_cleanup_transforms(self):
        "Restore IPython cleanup transforms removed earlier."
        if not self._removed_cleanup: return
        ip = get_ipython()
        if ip is None: return
        cleanup_transforms = ip.input_transformer_manager.cleanup_transforms
        for index in sorted(self._removed_cleanup):
            func = self._removed_cleanup.pop(index)
            cleanup_transforms.insert(index, func)

    def _request_payload(self, command:str, arguments: dict|None=None, seq:int|None=None)->dict:
        "Build a DAP request payload for `command`."
        if seq is None: seq = self.client.next_internal_seq()
        if arguments is None: arguments = {}
        return dict(type="request", command=command, seq=seq, arguments=arguments)

    def _response(self, request: dict, success: bool, body: dict|None=None, message:str|None=None)->dict:
        "Build a DAP response dict for `request`."
        reply = dict(type="response", request_seq=request.get("seq"), success=bool(success), command=request.get("command"))
        if message: reply["message"] = message
        if body is not None: reply["body"] = body
        return reply

    def _dump_cell(self, request: dict)->dict:
        "Write debug cell to disk and return its path."
        code = request.get("arguments", {}).get("code", "")
        file_name = _debug_file_name(code)
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, "w", encoding="utf-8") as f: f.write(code)
        return self._response(request, True, body=dict(sourcePath=file_name))

    def _debug_info(self, request: dict)->dict:
        "Return debugInfo response."
        breakpoints = [{"source": key, "breakpoints": value} for key, value in self.breakpoint_list.items()]
        body = dict(isStarted=self.started, hashMethod="Murmur2", hashSeed=DEBUG_HASH_SEED,
            tmpFilePrefix=_debug_tmp_directory() + os.sep, tmpFileSuffix=".py", breakpoints=breakpoints,
            stoppedThreads=list(self.stopped_threads), richRendering=True, exceptionPaths=["Python Exceptions"], copyToGlobals=True)
        return self._response(request, True, body=body)

    def _source(self, request: dict)->dict:
        "Return source response."
        source_path = request.get("arguments", {}).get("source", {}).get("path", "")
        if source_path and os.path.isfile(source_path):
            with open(source_path, encoding="utf-8") as f: content = f.read()
            return self._response(request, True, body=dict(content=content))
        return self._response(request, False, body={}, message="source unavailable")

    def _inspect_variables(self, request: dict)->dict:
        "Return a variables response from the user namespace."
        ip = get_ipython()
        if ip is None: return self._response(request, False, body={"variables": []}, message="no ipython")
        variables = []
        for name, value in ip.user_ns.items():
            if name.startswith("__") and name.endswith("__"): continue
            variables.append(dict(name=name, value=repr(value), type=type(value).__name__, evaluateName=name, variablesReference=0))
        return self._response(request, True, body={"variables": variables})

    def _rich_inspect_variables(self, request: dict)->dict:
        "Return rich variable data, including frame-based rendering."
        args = request.get("arguments", {}) if isinstance(request.get("arguments"), dict) else {}
        var_name = args.get("variableName")
        if not isinstance(var_name, str):
            return self._response(request, False, body={"data": {}, "metadata": {}}, message="invalid variable name")

        if not var_name.isidentifier():
            body = {"data": {}, "metadata": {}}
            if var_name in {"special variables", "function variables"}: return self._response(request, True, body=body)
            return self._response(request, False, body=body, message="invalid variable name")

        ip = get_ipython()
        if ip is None: return self._response(request, False, body={"data": {}, "metadata": {}}, message="no ipython")

        if self.stopped_threads and args.get("frameId") is not None:
            frame_id = args.get("frameId")
            if not isinstance(frame_id, int):
                return self._response(request, False, body={"data": {}, "metadata": {}}, message="invalid frame")
            code = f"get_ipython().display_formatter.format({var_name})"
            try:
                payload = self._request_payload("evaluate", dict(expression=code, frameId=frame_id, context="clipboard"))
                reply = self.client.send_request(payload)
            except TimeoutError: return self._response(request, False, body={"data": {}, "metadata": {}}, message="timeout")
            if reply.get("success"):
                try: repr_data, repr_metadata = eval(reply.get("body", {}).get("result", ""), {}, {})
                except (SyntaxError, NameError, TypeError, ValueError): repr_data, repr_metadata = {}, {}
                body = dict(data=repr_data or {}, metadata={k: v for k, v in (repr_metadata or {}).items() if k in (repr_data or {})})
                return self._response(request, True, body=body)
            return self._response(request, False, body={"data": {}, "metadata": {}}, message="evaluate failed")

        result = ip.user_expressions({var_name: var_name}).get(var_name, {})
        if result.get("status") == "ok":
            body = dict(data=result.get("data", {}), metadata=result.get("metadata", {}))
            return self._response(request, True, body=body)
        return self._response(request, False, body={"data": {}, "metadata": {}}, message="not found")

    def _copy_to_globals(self, request: dict)->dict:
        "Copy a frame variable into globals via setExpression."
        args = request.get("arguments", {}) if isinstance(request.get("arguments"), dict) else {}
        dst_var_name = args.get("dstVariableName")
        src_var_name = args.get("srcVariableName")
        src_frame_id = args.get("srcFrameId")
        if not (isinstance(dst_var_name, str) and isinstance(src_var_name, str) and isinstance(src_frame_id, int)):
            return self._response(request, False, body={}, message="invalid arguments")
        expression = f"globals()['{dst_var_name}']"
        try:
            payload = self._request_payload("setExpression", dict(expression=expression, value=src_var_name, frameId=src_frame_id))
            reply = self.client.send_request(payload)
        except TimeoutError: return self._response(request, False, body={}, message="timeout")
        return reply

    def _modules(self, request: dict)->dict:
        "Return module list for DAP `modules` request."
        args = request.get("arguments", {})
        if not isinstance(args, dict): args = {}
        modules = list(sys.modules.values())
        start_module = int(args.get("startModule", 0) or 0)
        module_count = args.get("moduleCount")
        if module_count is None: module_count = len(modules)
        else: module_count = int(module_count)
        mods = []
        end = min(len(modules), start_module + module_count)
        for i in range(start_module, end):
            module = modules[i]
            filename = getattr(getattr(module, "__spec__", None), "origin", None)
            if filename and filename.endswith(".py"): mods.append(dict(id=i, name=module.__name__, path=filename))
        return self._response(request, True, body={"modules": mods, "totalModules": len(modules)})


class MiniStream:
    def __init__(self, name:str, events: list[dict], sink: Callable[[str, str], None]|None=None):
        "Buffer stream text and emit events to `events`/`sink`."
        self.name = name
        self.events = events
        self._sink = sink
        self._buffer = ""

    def write(self, value)->int:
        "Write text to buffer and optionally emit live output."
        if value is None: return 0
        if isinstance(value, bytes): text = value.decode(errors="replace")
        elif isinstance(value, str): text = value
        else: text = str(value)
        if not text: return 0
        if self.events and self.events[-1]["name"] == self.name: self.events[-1]["text"] += text
        else: self.events.append({"name": self.name, "text": text})
        if self._sink is not None: self._emit_live(text)
        return len(text)

    def writelines(self, lines)->int:
        "Write multiple lines to the stream buffer."
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        "Flush buffered text to the sink."
        if self._sink is None: return None
        if self._buffer:
            self._sink(self.name, self._buffer)
            self._buffer = ""
        return None

    def isatty(self)->bool: return False

    def _emit_live(self, text:str):
        "Emit complete lines from buffer to the sink."
        self._buffer += text
        if "\n" not in self._buffer: return
        parts = self._buffer.split("\n")
        for line in parts[:-1]: self._sink(self.name, line + "\n")
        self._buffer = parts[-1]


class MiniDisplayPublisher(DisplayPublisher):
    def __init__(self, sender=None):
        "Collect display_pub events for IOPub."
        super().__init__()
        self.events = []
        self._sender = sender

    def set_sender(self, sender):
        "Set live display sender."
        self._sender = sender

    def publish(self, data, metadata=None, transient=None, update=False, **kwargs):
        "Record display data/update for later emission."
        buffers = kwargs.get("buffers")
        event = dict(type="display", data=data, metadata=metadata or {}, transient=transient or {},
            update=bool(update), buffers=buffers)
        if self._sender is not None: self._sender(event)
        else: self.events.append(event)

    def clear_output(self, wait:bool=False):
        event = {"type": "clear_output", "wait": bool(wait)}
        if self._sender is not None: self._sender(event)
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


class StdinNotImplementedError(RuntimeError): pass

def _maybe_json(value):
    "Parse JSON strings to objects; return {} on decode errors."
    if isinstance(value, str):
        try: return json.loads(value)
        except json.JSONDecodeError: return {}
    return value


def _env_flag(name:str)->bool|None:
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
    "Load IPython config, extensions, and startup via InteractiveShellApp."
    global _STARTUP_DONE
    if _STARTUP_DONE: return
    app = _MiniShellApp(shell)
    app.init_profile_dir()
    app.init_config_files()
    app.load_config_file()
    app.init_path()
    app.init_shell()
    app.init_extensions()
    app.init_code()
    _STARTUP_DONE = True


def _debug_tmp_directory()->str: return os.path.join(tempfile.gettempdir(), f"ipymini_{os.getpid()}")


def _debug_file_name(code:str)->str:
    "Compute debug cell filename; respects IPYMINI_CELL_NAME."
    cell_name = os.environ.get("IPYMINI_CELL_NAME")
    if cell_name is None:
        name = murmur2_x86(code, DEBUG_HASH_SEED)
        cell_name = os.path.join(_debug_tmp_directory(), f"{name}.py")
    return cell_name


class KernelBridge:
    def __init__(self, request_input: Callable[[str, bool], str], debug_event_callback: Callable[[dict], None]|None=None,
        zmq_context: zmq.Context|None=None, *, user_ns: dict|None=None, use_singleton:bool=True):
        "Initialize IPython shell, IO capture, and debugger hooks."
        from IPython.core import page

        os.environ.setdefault("MPLBACKEND", "module://matplotlib_inline.backend_inline")
        _IO_STATE.install()
        if use_singleton: self.shell = InteractiveShell.instance(user_ns=user_ns)
        else: self.shell = InteractiveShell(user_ns=user_ns)
        use_jedi = _env_flag("IPYMINI_USE_JEDI")
        if use_jedi is not None: self.shell.Completer.use_jedi = use_jedi
        experimental = _env_flag("IPYMINI_EXPERIMENTAL_COMPLETIONS")
        if experimental is None: experimental = True
        self._use_experimental_completions = bool(experimental)

        def _code_name(raw_code:str, transformed_code:str, number:int)->str: return _debug_file_name(raw_code)

        self.shell.compile.get_code_name = _code_name
        self._request_input = request_input
        self._display_sender = None
        self.shell.display_pub = MiniDisplayPublisher(self._display_sender)
        self.shell.displayhook = MiniDisplayHook(shell=self.shell)
        self.shell.display_trap.hook = self.shell.displayhook
        self._stream_events = []
        self._stream_sender = None
        self._stream_live = contextvars.ContextVar("ipymini.stream_live", default=False)
        self._current_exec_task = None
        self._stdout = MiniStream("stdout", self._stream_events, sink=self._emit_stream)
        self._stderr = MiniStream("stderr", self._stream_events, sink=self._emit_stream)
        if self.shell.display_page: hook = page.as_hook(page.display_page)
        else: hook = page.as_hook(self._payloadpage_page)
        self.shell.set_hook("show_in_pager", hook, 99)
        self.shell._last_traceback = None

        def _showtraceback(etype, evalue, stb): self.shell._last_traceback = stb
        def _enable_gui(gui=None): self.shell.active_eventloop = gui
        def _set_next_input(text:str, replace:bool=False):
            payload = dict(source="set_next_input", text=text, replace=bool(replace))
            self.shell.payload_manager.write_payload(payload)

        self.shell._showtraceback = _showtraceback
        self.shell.enable_gui = _enable_gui
        self.shell.set_next_input = _set_next_input
        _init_ipython_app(self.shell)
        kernel_modules = [module.__file__ for module in sys.modules.values() if getattr(module, "__file__", None)]
        self._debugger = MiniDebugger(debug_event_callback, zmq_context=zmq_context, kernel_modules=kernel_modules,
            debug_just_my_code=False, filter_internal_frames=True)

    def _payloadpage_page(self, strg, start:int=0, screen_lines:int=0, pager_cmd=None):
        "Send pager output as a payload starting at `start`."
        start = max(0, start)
        data = strg if isinstance(strg, dict) else {"text/plain": strg}
        payload = dict(source="page", data=data, start=start)
        self.shell.payload_manager.write_payload(payload)

    def _reset_capture_state(self):
        "Clear display/output capture state for next execution."
        self.shell.display_pub.events.clear()
        self.shell.displayhook.last = None
        self.shell.displayhook.last_metadata = None
        self.shell.displayhook.last_execution_count = None
        self.shell._last_traceback = None
        self._stream_events.clear()

    async def _run_cell(self, code:str, silent:bool, store_history:bool):
        "Run `code` using IPython's sync/async helpers."
        shell = self.shell
        if not (hasattr(shell, "run_cell_async") and hasattr(shell, "should_run_async")):
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
        if loop_running and _asyncio_runner and shell.loop_runner is _asyncio_runner and should_run_async:
            res = None
            coro = shell.run_cell_async(code, store_history=store_history, silent=silent,
                transformed_cell=transformed, preprocessing_exc_tuple=exc_tuple)
            task = asyncio.create_task(coro)
            self._current_exec_task = task
            try: res = await task
            finally:
                self._current_exec_task = None
                shell.events.trigger("post_execute")
                if not silent: shell.events.trigger("post_run_cell", res)
            return res
        return shell.run_cell(code, store_history=store_history, silent=silent)

    async def execute(self, code:str, silent:bool=False, store_history:bool=True,
        user_expressions=None, allow_stdin:bool=False)->dict:
        "Execute `code` in IPython and return captured outputs/errors."
        self._reset_capture_state()
        display_sender = self._display_sender
        if silent and display_sender is not None: self.set_display_sender(None)
        token = self._stream_live.set(not silent and self._stream_sender is not None)
        live = self._stream_live.get()
        try:
            with _thread_local_io(self.shell, self._stdout, self._stderr, self._request_input, bool(allow_stdin)):
                self._debugger.trace_current_thread()
                result = await self._run_cell(code, silent=silent, store_history=store_history)
        finally:
            if live:
                try:
                    self._stdout.flush()
                    self._stderr.flush()
                except Exception: pass
            self._stream_live.reset(token)
            if silent and display_sender is not None: self.set_display_sender(display_sender)

        payload = self.shell.payload_manager.read_payload()
        self.shell.payload_manager.clear_payload()
        payload = self._dedupe_set_next_input(payload)

        error = None
        err = getattr(result, "error_in_exec", None) or getattr(result, "error_before_exec", None)
        if err is not None: error = dict(ename=type(err).__name__, evalue=str(err), traceback=self.shell._last_traceback or [])

        if user_expressions is None: user_expressions = {}
        user_expressions = _maybe_json(user_expressions) or {}
        if error is None: user_expr = self.shell.user_expressions(user_expressions)
        else: user_expr = {}

        exec_count = getattr(result, "execution_count", self.shell.execution_count)

        streams = [] if self._stream_sender is not None else list(self._stream_events)
        result_meta = self.shell.displayhook.last_metadata or {}
        display_events = [] if self._display_sender is not None else list(self.shell.display_pub.events)
        return dict(streams=streams, display=display_events, result=self.shell.displayhook.last, result_metadata=result_meta,
            execution_count=exec_count, error=error, user_expressions=user_expr, payload=payload)

    def set_stream_sender(self, sender: Callable[[str, str], None]|None): self._stream_sender = sender

    def set_display_sender(self, sender: Callable[[dict], None]|None):
        "Set live display sender; None to buffer display events."
        self._display_sender = sender
        if hasattr(self.shell.display_pub, "set_sender"): self.shell.display_pub.set_sender(sender)

    def cancel_exec_task(self, loop: asyncio.AbstractEventLoop|None)->bool:
        "Cancel the currently running async execution task, if any."
        task = self._current_exec_task
        if task is None or task.done() or loop is None: return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def _emit_stream(self, name:str, text:str):
        if self._stream_live.get() and self._stream_sender is not None and text: self._stream_sender(name, text)

    def _dedupe_set_next_input(self, payload: list[dict])->list[dict]:
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

    def complete(self, code:str, cursor_pos:int|None=None)->dict:
        "Return completion matches for `code` at `cursor_pos`."
        if self._use_experimental_completions:
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
            return dict(matches=matches, cursor_start=cursor_start, cursor_end=cursor_end, metadata={
                _EXPERIMENTAL_COMPLETIONS_KEY: [dict(start=c.start, end=c.end, text=c.text, type=c.type, signature=c.signature)
                    for c in completions]}, status="ok")
        if cursor_pos is None: cursor_pos = len(code)
        from IPython.utils.tokenutil import line_at_cursor

        line, offset = line_at_cursor(code, cursor_pos)
        line_cursor = cursor_pos - offset
        txt, matches = self.shell.complete("", line, line_cursor)
        return dict(matches=matches, cursor_start=cursor_pos - len(txt), cursor_end=cursor_pos, metadata={}, status="ok")

    def inspect(self, code:str, cursor_pos:int|None=None, detail_level:int=0)->dict:
        "Return inspection data for `code` at `cursor_pos`."
        if cursor_pos is None: cursor_pos = len(code)
        from IPython.utils.tokenutil import token_at_cursor

        name = token_at_cursor(code, cursor_pos)
        if not name: return dict(status="ok", found=False, data={}, metadata={})
        bundle = self.shell.object_inspect_mime(name, detail_level=detail_level)
        if not self.shell.enable_html_pager: bundle.pop("text/html", None)
        return dict(status="ok", found=True, data=bundle, metadata={})

    def is_complete(self, code:str)->dict:
        "Report completeness status and indentation for `code`."
        tm = getattr(self.shell, "input_transformer_manager", None)
        if tm is None: tm = self.shell.input_splitter
        status, indent_spaces = tm.check_complete(code)
        reply = {"status": status}
        if status == "incomplete": reply["indent"] = " " * indent_spaces
        return reply

    def history(self, hist_access_type:str, output: bool, raw: bool, session:int=0, start:int=0,
        stop=None, n=None, pattern=None, unique:bool=False)->dict:
        "Return history entries based on `hist_access_type` query."
        if hist_access_type == "tail": hist = self.shell.history_manager.get_tail(n, raw=raw, output=output, include_latest=True)
        elif hist_access_type == "range":
            hist = self.shell.history_manager.get_range( session, start, stop, raw=raw, output=output)
        elif hist_access_type == "search":
            hist = self.shell.history_manager.search( pattern, raw=raw, output=output, n=n, unique=unique)
        else: hist = []
        return {"status": "ok", "history": list(hist)}


    def debug_request(self, request_json:str)->dict:
        "Handle a debug_request DAP message in JSON."
        try: request = json.loads(request_json)
        except json.JSONDecodeError: request = {}
        response, events = self._debugger.process_request(request)
        return {"response": response, "events": events}
