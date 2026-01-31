import json, os, queue, socket, sys, threading
from typing import Callable

import debugpy
import zmq
from IPython.core.getipython import get_ipython

from .cells import DEBUG_HASH_SEED, debug_cell_filename, debug_tmp_directory


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

    def _put_message(self, raw_msg: str):
        msg = json.loads(raw_msg)
        if msg.get("type") == "event": self.event_callback(msg)
        else: self.response_callback(msg)

    def put_tcp_frame(self, frame: str):
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
    def __init__(self, context: zmq.Context, event_callback: Callable[[dict], None] | None):
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

    def connect(self, host: str, port: int):
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

    def send_request(self, msg: dict, timeout: float = 10.0) -> dict:
        "Send a debugpy request and wait for a response."
        req_seq = msg.get("seq")
        if not isinstance(req_seq, int) or req_seq <= 0:
            req_seq = self.next_internal_seq()
            msg["seq"] = req_seq
        req_seq, waiter = self.send_request_async(msg)
        return self.wait_for_response(req_seq, waiter, timeout=timeout)

    def send_request_async(self, msg: dict) -> tuple[int, queue.Queue]:
        "Send a request and return `(seq, waiter)` without waiting."
        req_seq = msg.get("seq")
        if not isinstance(req_seq, int) or req_seq <= 0:
            req_seq = self.next_internal_seq()
            msg["seq"] = req_seq
        waiter = queue.Queue()
        with self.pending_lock: self.pending[req_seq] = waiter
        self.outgoing.put(msg)
        return req_seq, waiter

    def wait_for_response(self, req_seq: int, waiter: queue.Queue, timeout: float = 10.0) -> dict:
        "Wait for a response on `waiter` until `timeout`."
        try: reply = waiter.get(timeout=timeout)
        except queue.Empty as exc: raise TimeoutError("timed out waiting for debugpy response") from exc
        finally:
            with self.pending_lock: self.pending.pop(req_seq, None)
        return reply

    def wait_initialized(self, timeout: float = 5.0) -> bool: return self.initialized.wait(timeout=timeout)

    def next_internal_seq(self) -> int:
        "Return the next internal sequence number."
        seq = self.next_seq
        self.next_seq += 1
        return seq


class Debugger:
    def __init__(self, event_callback=None, *, zmq_context=None, kernel_modules=None, debug_just_my_code=False, filter_internal_frames=True):
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
        self.simple_handlers = dict(configurationDone=lambda r: self._ok(r), debugInfo=self._debug_info, inspectVariables=self._inspect_variables)
        self.simple_handlers.update(richInspectVariables=self._rich_inspect_variables, copyToGlobals=self._copy_to_globals)
        self.simple_handlers.update(modules=self._modules, source=self._source, dumpCell=self._dump_cell)

    def _get_free_port(self) -> int:
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

    def process_request(self, request: dict) -> tuple[dict, list]:
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
                    bps = response.get("body", {}).get("breakpoints", [])
                    self.breakpoint_list[src] = [{"line": bp["line"]} for bp in bps if isinstance(bp, dict) and "line" in bp]
            return response or {}, self.events

        response = self.client.send_request(request)
        if command == "disconnect" and self.started: self._reset_session()
        return response or {}, self.events

    def process_request_json(self, request_json: str) -> dict:
        try: request = json.loads(request_json)
        except json.JSONDecodeError: request = {}
        response, events = self.process_request(request)
        return {"response": response, "events": events}

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

    def _request_payload(self, command: str, arguments: dict | None = None, seq: int | None = None) -> dict:
        "Build a DAP request payload for `command`."
        if seq is None: seq = self.client.next_internal_seq()
        if arguments is None: arguments = {}
        return dict(type="request", command=command, seq=seq, arguments=arguments)

    def _response(self, request: dict, success: bool, body: dict | None = None, message: str | None = None) -> dict:
        "Build a DAP response dict for `request`."
        reply = dict(type="response", request_seq=request.get("seq"), success=bool(success), command=request.get("command"))
        if message: reply["message"] = message
        if body is not None: reply["body"] = body
        return reply

    def _ok(self, request: dict, **body) -> dict: return self._response(request, True, body=body or {})

    def _fail(self, request: dict, message: str, body: dict | None = None) -> dict:
        return self._response(request, False, body=body or {}, message=message)

    def _dump_cell(self, request: dict) -> dict:
        "Write debug cell to disk and return its path."
        code = request.get("arguments", {}).get("code", "")
        file_name = debug_cell_filename(code)
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, "w", encoding="utf-8") as f: f.write(code)
        return self._ok(request, sourcePath=file_name)

    def _debug_info(self, request: dict) -> dict:
        "Return debugInfo response."
        breakpoints = [{"source": key, "breakpoints": value} for key, value in self.breakpoint_list.items()]
        body = dict(isStarted=self.started, hashMethod="Murmur2", hashSeed=DEBUG_HASH_SEED)
        body.update(tmpFilePrefix=debug_tmp_directory() + os.sep, tmpFileSuffix=".py", breakpoints=breakpoints)
        body.update(stoppedThreads=list(self.stopped_threads), richRendering=True, exceptionPaths=["Python Exceptions"], copyToGlobals=True)
        return self._ok(request, **body)

    def _source(self, request: dict) -> dict:
        "Return source response."
        source_path = request.get("arguments", {}).get("source", {}).get("path", "")
        if source_path and os.path.isfile(source_path):
            with open(source_path, encoding="utf-8") as f: content = f.read()
            return self._ok(request, content=content)
        return self._fail(request, "source unavailable")

    def _inspect_variables(self, request: dict) -> dict:
        "Return a variables response from the user namespace."
        ip = get_ipython()
        if ip is None: return self._fail(request, "no ipython", body={"variables": []})
        variables = []
        for name, value in ip.user_ns.items():
            if name.startswith("__") and name.endswith("__"): continue
            variables.append(dict(name=name, value=repr(value), type=type(value).__name__, evaluateName=name, variablesReference=0))
        return self._ok(request, variables=variables)

    def _rich_inspect_variables(self, request: dict) -> dict:
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

    def _copy_to_globals(self, request: dict) -> dict:
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

    def _modules(self, request: dict) -> dict:
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
