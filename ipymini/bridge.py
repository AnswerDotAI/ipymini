import asyncio, builtins, contextvars, getpass, json, logging, os, queue, socket, sys, tempfile, threading, traceback
_debug = os.environ.get("IPYMINI_DEBUG", "").lower() in ("1", "true", "yes")
_real_stderr = sys.__stderr__  # Use original stderr, not wrapped version
def _dbg(*args):
    if _debug: print("[bridge]", *args, file=_real_stderr, flush=True)
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

experimental_completions_key = "_jupyter_types_experimental"
log = logging.getLogger("ipymini.startup")
startup_done = False


class _ThreadLocalStream:
    def __init__(self, name:str, default): self.name, self.default = name, default
    def _target(self): return io_state.get(self.name) or self.default

    def write(self, value)->int:
        t = self._target()
        return 0 if t is None else t.write(value)

    def writelines(self, lines)->int:
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        t = self._target()
        if t is not None and hasattr(t, "flush"): t.flush()

    def isatty(self)->bool:
        t = self._target()
        return bool(getattr(t, "isatty", lambda: False)())

    def __getattr__(self, k): return getattr(self._target(), k)

_chans = ("shell", "stdout", "stderr", "request_input", "allow_stdin")

class _ThreadLocalIO:
    def __init__(self):
        "Capture original IO hooks and prepare thread-local state."
        self.installed = False
        self.vars = {name: contextvars.ContextVar(f"ipymini.{name}", default=None) for name in _chans}
        self.orig_stdout = sys.stdout
        self.orig_stderr = sys.stderr
        self.orig_input = builtins.input
        self.orig_getpass = getpass.getpass
        self.orig_get_ipython = _getipython_mod.get_ipython

    def install(self):
        "Install thread-local stdout/stderr/input/getpass/get_ipython hooks."
        if self.installed: return
        sys.stdout = _ThreadLocalStream("stdout", self.orig_stdout)
        sys.stderr = _ThreadLocalStream("stderr", self.orig_stderr)
        builtins.input = _thread_local_input
        getpass.getpass = _thread_local_getpass
        _getipython_mod.get_ipython = _thread_local_get_ipython
        self.installed = True

    def get(self, name: str): return self.vars[name].get()

    def push(self, shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool)->dict:
        "Set per-thread IO bindings; returns the previous bindings."
        args = locals()
        prev = {name: self.vars[name].set(args[name]) for name in _chans}
        return prev

    def pop(self, prev: dict):
        "Restore IO bindings from `prev`."
        for name in _chans: self.vars[name].reset(prev[name])


io_state = _ThreadLocalIO()


def _thread_local_get_ipython():
    shell = io_state.get("shell")
    return shell if shell is not None else io_state.orig_get_ipython()


def _thread_local_input(prompt:str = "")->str:
    "Route input() through kernel stdin handler using `prompt`."
    handler = io_state.get("request_input")
    allow = bool(io_state.get("allow_stdin"))
    if handler is None or not allow:
        msg = "raw_input was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), False)


def _thread_local_getpass(prompt:str = "Password: ", stream=None)->str:
    "Route getpass() through stdin handler using `prompt`."
    handler = io_state.get("request_input")
    allow = bool(io_state.get("allow_stdin"))
    if handler is None or not allow:
        msg = "getpass was called, but this frontend does not support input requests."
        raise StdinNotImplementedError(msg)
    return handler(str(prompt), True)


@contextmanager
def _thread_local_io( shell, stdout, stderr, request_input: Callable[[str, bool], str], allow_stdin: bool):
    prev = io_state.push(shell, stdout, stderr, request_input, allow_stdin)
    try: yield
    finally: io_state.pop(prev)


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
        self.header_pos = -1
        self.separator_pos = -1
        self.message_size = 0
        self.message_pos = -1

    def _put_message(self, raw_msg:str):
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
        self.event_callback = event_callback
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.stop = threading.Event()
        self.reader_thread = None
        self.initialized = threading.Event()
        self.outgoing = queue.Queue()
        self.routing_id = None
        self.endpoint = None
        self.message_queue = DebugpyMessageQueue(self._handle_event, self._handle_response)

    def connect(self, host:str, port:int):
        "Connect to debugpy adapter at `host:port` and start reader."
        self.endpoint = f"tcp://{host}:{port}"
        self._start_reader()

    def _start_reader(self):
        if self.reader_thread and self.reader_thread.is_alive(): return
        self.stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def close(self):
        "Stop reader thread and close debugpy socket."
        self.stop.set()
        self.initialized.clear()
        if self.reader_thread: self.reader_thread.join(timeout=1)
        self.reader_thread = None

    def _handle_event(self, msg: dict):
        if msg.get("event") == "initialized": self.initialized.set()
        if self.event_callback: self.event_callback(msg)

    def _handle_response(self, msg: dict):
        req_seq = msg.get("request_seq")
        if isinstance(req_seq, int):
            with self.pending_lock: waiter = self.pending.get(req_seq)
            if waiter is not None: waiter.put(msg)

    def _reader_loop(self):
        if self.endpoint is None: return
        debugpy.trace_this_thread(False)
        sock = self.context.socket(zmq.STREAM)
        sock.linger = 0
        sock.connect(self.endpoint)
        self.routing_id = sock.getsockopt(zmq.ROUTING_ID)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self.stop.is_set():
                self._drain_outgoing(sock)
                events = dict(poller.poll(50))
                if sock in events and events[sock] & zmq.POLLIN:
                    frames = sock.recv_multipart()
                    if len(frames) < 2: continue
                    data = frames[1]
                    if not data: continue
                    text = data.decode("utf-8", errors="replace")
                    self.message_queue.put_tcp_frame(text)
        finally: sock.close(0)

    def _drain_outgoing(self, sock: zmq.Socket):
        if self.routing_id is None: return
        while True:
            try: msg = self.outgoing.get_nowait()
            except queue.Empty: break
            payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            sock.send_multipart([self.routing_id, header + payload])

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
        with self.pending_lock: self.pending[req_seq] = waiter
        self.outgoing.put(msg)
        return req_seq, waiter

    def wait_for_response(self, req_seq:int, waiter: queue.Queue, timeout:float = 10.0)->dict:
        "Wait for a response on `waiter` until `timeout`."
        try: reply = waiter.get(timeout=timeout)
        except queue.Empty as exc: raise TimeoutError("timed out waiting for debugpy response") from exc
        finally:
            with self.pending_lock: self.pending.pop(req_seq, None)
        return reply

    def wait_initialized(self, timeout:float = 5.0)->bool: return self.initialized.wait(timeout=timeout)

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
        self.event_callback = event_callback
        context = zmq_context or zmq.Context.instance()
        self.client = MiniDebugpyClient(context, self._handle_event)
        self.started = False
        self.host = "127.0.0.1"
        self.port = None
        self.breakpoint_list = {}
        self.stopped_threads = set()
        self.traced_threads = set()
        self.removed_cleanup = {}
        self.kernel_modules = kernel_modules or []
        self.just_my_code = debug_just_my_code
        self.filter_internal_frames = filter_internal_frames
        self.empty_rich = {"data": {}, "metadata": {}}
        self.simple_handlers = dict(configurationDone=lambda r: self._ok(r), debugInfo=self._debug_info,
            inspectVariables=self._inspect_variables, richInspectVariables=self._rich_inspect_variables,
            copyToGlobals=self._copy_to_globals, modules=self._modules, source=self._source, dumpCell=self._dump_cell)

    def _get_free_port(self)->int:
        "Select a free localhost TCP port."
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _ensure_started(self):
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
        if msg.get("event") == "stopped":
            thread_id = msg.get("body", {}).get("threadId")
            if isinstance(thread_id, int): self.stopped_threads.add(thread_id)
        elif msg.get("event") == "continued":
            thread_id = msg.get("body", {}).get("threadId")
            if isinstance(thread_id, int): self.stopped_threads.discard(thread_id)
        if self.event_callback: self.event_callback(msg)
        else: self.events.append(msg)

    def process_request(self, request: dict)->tuple[dict, list]:
        "Handle a DAP request and return response plus queued events."
        self.events = []
        command = request.get("command")
        if command == "terminate":
            if self.started: self._reset_session()
            return self._ok(request), self.events
        self._ensure_started()
        if "seq" in request: self.client.next_seq = max(self.client.next_seq, int(request["seq"]) + 1)
        if (handler := self.simple_handlers.get(command)) is not None: return handler(request), self.events

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
        self.client.close()
        self.started = False
        self.breakpoint_list = {}
        self.stopped_threads = set()
        self.traced_threads.clear()
        self._restore_cleanup_transforms()

    def trace_current_thread(self):
        "Enable debugpy tracing on the current thread if needed."
        if not self.started: return
        thread_id = threading.get_ident()
        if thread_id in self.traced_threads: return
        debugpy.trace_this_thread(True)
        self.traced_threads.add(thread_id)

    def _remove_cleanup_transforms(self):
        ip = get_ipython()
        if ip is None: return
        from IPython.core.inputtransformer2 import leading_empty_lines
        cleanup_transforms = ip.input_transformer_manager.cleanup_transforms
        if leading_empty_lines in cleanup_transforms:
            index = cleanup_transforms.index(leading_empty_lines)
            self.removed_cleanup[index] = cleanup_transforms.pop(index)

    def _restore_cleanup_transforms(self):
        if not self.removed_cleanup: return
        ip = get_ipython()
        if ip is None: return
        cleanup_transforms = ip.input_transformer_manager.cleanup_transforms
        for index in sorted(self.removed_cleanup):
            func = self.removed_cleanup.pop(index)
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

    def _ok(self, request: dict, **body)->dict: return self._response(request, True, body=body or {})
    def _fail(self, request: dict, message:str, body: dict|None=None)->dict: return self._response(request, False, body=body or {}, message=message)

    def _dump_cell(self, request: dict)->dict:
        "Write debug cell to disk and return its path."
        code = request.get("arguments", {}).get("code", "")
        file_name = _debug_file_name(code)
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, "w", encoding="utf-8") as f: f.write(code)
        return self._ok(request, sourcePath=file_name)

    def _debug_info(self, request: dict)->dict:
        "Return debugInfo response."
        breakpoints = [{"source": key, "breakpoints": value} for key, value in self.breakpoint_list.items()]
        body = dict(isStarted=self.started, hashMethod="Murmur2", hashSeed=DEBUG_HASH_SEED,
            tmpFilePrefix=_debug_tmp_directory() + os.sep, tmpFileSuffix=".py", breakpoints=breakpoints,
            stoppedThreads=list(self.stopped_threads), richRendering=True, exceptionPaths=["Python Exceptions"], copyToGlobals=True)
        return self._ok(request, **body)

    def _source(self, request: dict)->dict:
        "Return source response."
        source_path = request.get("arguments", {}).get("source", {}).get("path", "")
        if source_path and os.path.isfile(source_path):
            with open(source_path, encoding="utf-8") as f: content = f.read()
            return self._ok(request, content=content)
        return self._fail(request, "source unavailable")

    def _inspect_variables(self, request: dict)->dict:
        "Return a variables response from the user namespace."
        ip = get_ipython()
        if ip is None: return self._fail(request, "no ipython", body={"variables": []})
        variables = []
        for name, value in ip.user_ns.items():
            if name.startswith("__") and name.endswith("__"): continue
            variables.append(dict(name=name, value=repr(value), type=type(value).__name__, evaluateName=name, variablesReference=0))
        return self._ok(request, variables=variables)

    def _rich_inspect_variables(self, request: dict)->dict:
        "Return rich variable data, including frame-based rendering."
        args = request.get("arguments", {}) if isinstance(request.get("arguments"), dict) else {}
        var_name = args.get("variableName")
        if not isinstance(var_name, str): return self._fail(request, "invalid variable name", body=self.empty_rich)
        if not var_name.isidentifier():
            if var_name in {"special variables", "function variables"}: return self._ok(request, **self.empty_rich)
            return self._fail(request, "invalid variable name", body=self.empty_rich)

        ip = get_ipython()
        if ip is None: return self._fail(request, "no ipython", body=self.empty_rich)

        if self.stopped_threads and args.get("frameId") is not None:
            frame_id = args.get("frameId")
            if not isinstance(frame_id, int): return self._fail(request, "invalid frame", body=self.empty_rich)
            code = f"get_ipython().display_formatter.format({var_name})"
            try:
                payload = self._request_payload("evaluate", dict(expression=code, frameId=frame_id, context="clipboard"))
                reply = self.client.send_request(payload)
            except TimeoutError: return self._fail(request, "timeout", body=self.empty_rich)
            if reply.get("success"):
                try: repr_data, repr_metadata = eval(reply.get("body", {}).get("result", ""), {}, {})
                except (SyntaxError, NameError, TypeError, ValueError): repr_data, repr_metadata = {}, {}
                body = dict(data=repr_data or {}, metadata={k: v for k, v in (repr_metadata or {}).items() if k in (repr_data or {})})
                return self._ok(request, **body)
            return self._fail(request, "evaluate failed", body=self.empty_rich)

        result = ip.user_expressions({var_name: var_name}).get(var_name, {})
        if result.get("status") == "ok": return self._ok(request, data=result.get("data", {}), metadata=result.get("metadata", {}))
        return self._fail(request, "not found", body=self.empty_rich)

    def _copy_to_globals(self, request: dict)->dict:
        "Copy a frame variable into globals via setExpression."
        args = request.get("arguments", {}) if isinstance(request.get("arguments"), dict) else {}
        dst_var_name = args.get("dstVariableName")
        src_var_name = args.get("srcVariableName")
        src_frame_id = args.get("srcFrameId")
        if not (isinstance(dst_var_name, str) and isinstance(src_var_name, str) and isinstance(src_frame_id, int)):
            return self._fail(request, "invalid arguments")
        expression = f"globals()['{dst_var_name}']"
        try:
            payload = self._request_payload("setExpression", dict(expression=expression, value=src_var_name, frameId=src_frame_id))
            reply = self.client.send_request(payload)
        except TimeoutError: return self._fail(request, "timeout")
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
        return self._ok(request, modules=mods, totalModules=len(modules))


class MiniStream:
    def __init__(self, name:str, events: list[dict], sink: Callable[[str, str], None]|None=None):
        "Buffer stream text and emit events to `events`/`sink`."
        self.name = name
        self.events = events
        self.sink = sink
        self.buffer = ""

    def write(self, value)->int:
        "Write text to buffer or emit live output."
        if value is None: return 0
        if isinstance(value, bytes): text = value.decode(errors="replace")
        elif isinstance(value, str): text = value
        else: text = str(value)
        if not text: return 0
        if self.sink is not None: self._emit_live(text)
        if self.events is None: return len(text)
        if self.events and self.events[-1]["name"] == self.name: self.events[-1]["text"] += text
        else: self.events.append({"name": self.name, "text": text})
        return len(text)

    def writelines(self, lines)->int:
        "Write multiple lines to the stream buffer."
        total = 0
        for line in lines: total += self.write(line) or 0
        return total

    def flush(self):
        "Flush buffered text to the sink."
        if self.sink is None: return None
        if self.buffer:
            self.sink(self.name, self.buffer)
            self.buffer = ""
        return None

    def isatty(self)->bool: return False

    def _emit_live(self, text:str):
        self.buffer += text
        if "\n" not in self.buffer: return
        parts = self.buffer.split("\n")
        for line in parts[:-1]: self.sink(self.name, line + "\n")
        self.buffer = parts[-1]


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
        event = dict(type="display", data=data, metadata=metadata or {}, transient=transient or {},
            update=bool(update), buffers=buffers)
        live = self.live_var.get() if self.live_var is not None else False
        if self.sender is not None and live: self.sender(event)
        else: self.events.append(event)

    def clear_output(self, wait:bool=False):
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


class StdinNotImplementedError(RuntimeError): pass

def _maybe_json(value):
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
        io_state.install()
        if use_singleton: self.shell = InteractiveShell.instance(user_ns=user_ns)
        else: self.shell = InteractiveShell(user_ns=user_ns)
        use_jedi = _env_flag("IPYMINI_USE_JEDI")
        if use_jedi is not None: self.shell.Completer.use_jedi = use_jedi
        def _code_name(raw_code:str, transformed_code:str, number:int)->str: return _debug_file_name(raw_code)

        self.shell.compile.get_code_name = _code_name
        self.request_input = request_input
        self.display_sender = None
        self.display_live = contextvars.ContextVar("ipymini.display_live", default=False)
        self.shell.display_pub = MiniDisplayPublisher(self.display_sender, self.display_live)
        self.shell.displayhook = MiniDisplayHook(shell=self.shell)
        self.shell.display_trap.hook = self.shell.displayhook
        self.stream_events = []
        self.stream_sender = None
        self.stream_live = contextvars.ContextVar("ipymini.stream_live", default=False)
        self.current_exec_task = None
        self.stdout = MiniStream("stdout", self.stream_events, sink=self._emit_stream)
        self.stderr = MiniStream("stderr", self.stream_events, sink=self._emit_stream)
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
        self.debugger = MiniDebugger(debug_event_callback, zmq_context=zmq_context, kernel_modules=kernel_modules,
            debug_just_my_code=False, filter_internal_frames=True)

    def _payloadpage_page(self, strg, start:int=0, screen_lines:int=0, pager_cmd=None):
        start = max(0, start)
        data = strg if isinstance(strg, dict) else {"text/plain": strg}
        payload = dict(source="page", data=data, start=start)
        self.shell.payload_manager.write_payload(payload)

    def _reset_capture_state(self):
        self.shell.display_pub.events.clear()
        self.shell.displayhook.last = None
        self.shell.displayhook.last_metadata = None
        self.shell.displayhook.last_execution_count = None
        self.shell._last_traceback = None
        self.stream_events.clear()

    async def _run_cell(self, code:str, silent:bool, store_history:bool):
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

    def _exc_to_error(self, exc: BaseException)->dict:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return dict(ename=type(exc).__name__, evalue=str(exc), traceback=tb)

    async def execute(self, code:str, silent:bool=False, store_history:bool=True,
        user_expressions=None, allow_stdin:bool=False)->dict:
        "Execute `code` in IPython and return captured outputs/errors (never raises)."
        _dbg(f"execute start: {code[:30]!r}...")
        self._reset_capture_state()
        display_token = self.display_live.set(not silent and self.display_sender is not None)
        token = self.stream_live.set(not silent and self.stream_sender is not None)
        result = None
        raised = None
        try:
            with _thread_local_io(self.shell, self.stdout, self.stderr, self.request_input, bool(allow_stdin)):
                self.debugger.trace_current_thread()
                _dbg("execute: calling _run_cell")
                result = await self._run_cell(code, silent=silent, store_history=store_history)
                _dbg("execute: _run_cell done")
        except BaseException as exc:
            _dbg(f"execute: exception {type(exc).__name__}: {exc}")
            raised = exc
        finally:
            try:
                self.stdout.flush()
                self.stderr.flush()
            except Exception: pass
            self.stream_live.reset(token)
            self.display_live.reset(display_token)

        payload = self.shell.payload_manager.read_payload()
        self.shell.payload_manager.clear_payload()
        payload = self._dedupe_set_next_input(payload)
        streams = [] if self.stream_sender is not None else list(self.stream_events)
        display_events = [] if self.display_sender is not None else list(self.shell.display_pub.events)

        if raised is not None:
            return dict(streams=streams, display=display_events, result=None, result_metadata={},
                execution_count=self.shell.execution_count, error=self._exc_to_error(raised), user_expressions={}, payload=payload)

        error = None
        err = getattr(result, "error_in_exec", None) or getattr(result, "error_before_exec", None)
        if err is not None: error = dict(ename=type(err).__name__, evalue=str(err), traceback=self.shell._last_traceback or [])

        if user_expressions is None: user_expressions = {}
        user_expressions = _maybe_json(user_expressions) or {}
        user_expr = self.shell.user_expressions(user_expressions) if error is None else {}

        exec_count = getattr(result, "execution_count", self.shell.execution_count)
        result_meta = self.shell.displayhook.last_metadata or {}
        return dict(streams=streams, display=display_events, result=self.shell.displayhook.last, result_metadata=result_meta,
            execution_count=exec_count, error=error, user_expressions=user_expr, payload=payload)

    def set_stream_sender(self, sender: Callable[[str, str], None]|None):
        self.stream_sender = sender
        ev = None if sender is not None else self.stream_events
        self.stdout.events, self.stderr.events = ev, ev

    def set_display_sender(self, sender: Callable[[dict], None]|None):
        "Set live display sender; None to buffer display events."
        self.display_sender = sender
        if hasattr(self.shell.display_pub, "set_sender"): self.shell.display_pub.set_sender(sender)

    def cancel_exec_task(self, loop: asyncio.AbstractEventLoop|None)->bool:
        "Cancel the currently running async execution task, if any."
        task = self.current_exec_task
        if task is None or task.done() or loop is None: return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def _emit_stream(self, name:str, text:str):
        if self.stream_live.get() and self.stream_sender is not None and text: self.stream_sender(name, text)

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
        response, events = self.debugger.process_request(request)
        return {"response": response, "events": events}
