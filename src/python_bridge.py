import json
import socket
import sys
import time
from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher
from IPython.core.interactiveshell import InteractiveShell

try:
    import debugpy

    _DEBUGPY_AVAILABLE = True
except Exception:
    debugpy = None
    _DEBUGPY_AVAILABLE = False


class DebugpyClient:
    def __init__(self):
        self.sock = None
        self.buffer = b""
        self.next_seq = 1

    def connect(self, host, port, timeout=5.0):
        deadline = time.time() + timeout
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=1.0)
                sock.settimeout(1.0)
                self.sock = sock
                return
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
        self.buffer = b""

    def send(self, msg):
        if self.sock is None:
            raise RuntimeError("debugpy socket not connected")
        payload = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        self.sock.sendall(header + payload)

    def _recv_bytes(self):
        if self.sock is None:
            raise RuntimeError("debugpy socket not connected")
        data = self.sock.recv(4096)
        if not data:
            raise RuntimeError("debugpy socket closed")
        self.buffer += data

    def recv_message(self, timeout=5.0):
        deadline = time.time() + timeout if timeout else None
        while True:
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end == -1:
                try:
                    self._recv_bytes()
                except socket.timeout:
                    if deadline and time.time() >= deadline:
                        raise TimeoutError("timed out")
                    continue
                continue
            header = self.buffer[:header_end].decode("ascii", errors="replace")
            body_start = header_end + 4
            length = None
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
                    break
            if length is None:
                self.buffer = self.buffer[body_start:]
                continue
            if len(self.buffer) < body_start + length:
                try:
                    self._recv_bytes()
                except socket.timeout:
                    if deadline and time.time() >= deadline:
                        raise TimeoutError("timed out")
                    continue
                continue
            body = self.buffer[body_start : body_start + length]
            self.buffer = self.buffer[body_start + length :]
            return json.loads(body.decode("utf-8"))

    def next_internal_seq(self):
        seq = self.next_seq
        self.next_seq += 1
        return seq


class RustDebugger:
    def __init__(self):
        self.client = DebugpyClient()
        self.started = False
        self.events = []
        self.internal_seq = 1
        self.host = "127.0.0.1"
        self.port = None

    def _get_free_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _ensure_started(self):
        if self.started:
            return
        if not _DEBUGPY_AVAILABLE:
            raise RuntimeError("debugpy not available")
        port = self._get_free_port()
        debugpy.listen((self.host, port), in_process_debug_adapter=True)
        self.client.connect(self.host, port)
        self.port = port
        self.started = True

    def _record_event(self, msg):
        self.events.append(msg)

    def _send_and_wait(self, request_seq, timeout=10.0):
        while True:
            msg = self.client.recv_message(timeout=timeout)
            if msg.get("type") == "event":
                self._record_event(msg)
                continue
            if msg.get("type") == "response" and msg.get("request_seq") == request_seq:
                return msg

    def process_request(self, request):
        self.events = []
        command = request.get("command")

        if not _DEBUGPY_AVAILABLE:
            return {}, []

        self._ensure_started()

        if "seq" in request:
            self.client.next_seq = max(self.client.next_seq, int(request["seq"]) + 1)

        if command == "attach":
            arguments = request.get("arguments") or {}
            arguments["connect"] = {"host": self.host, "port": self.port}
            request["arguments"] = arguments
            self.client.send(request)
            response = None
            config_seq = None
            while True:
                msg = self.client.recv_message(timeout=10.0)
                if msg.get("type") == "event":
                    self._record_event(msg)
                    if msg.get("event") == "initialized":
                        config_seq = self.client.next_internal_seq()
                        config = {"type": "request", "seq": config_seq, "command": "configurationDone"}
                        self.client.send(config)
                    continue
                if (
                    msg.get("type") == "response"
                    and config_seq is not None
                    and msg.get("request_seq") == config_seq
                ):
                    # ignore configurationDone response
                    continue
                if msg.get("type") == "response" and msg.get("request_seq") == request.get("seq"):
                    response = msg
                    break
            return response or {}, self.events

        self.client.send(request)
        response = self._send_and_wait(request.get("seq"))
        if command == "disconnect":
            self.client.close()
            self.started = False
        return response or {}, self.events


class RustStream:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def write(self, value):
        if value is None:
            return 0
        if isinstance(value, bytes):
            text = value.decode(errors="replace")
        elif isinstance(value, str):
            text = value
        else:
            text = str(value)
        if not text:
            return 0
        if self.events and self.events[-1]["name"] == self.name:
            self.events[-1]["text"] += text
        else:
            self.events.append({"name": self.name, "text": text})
        return len(text)

    def writelines(self, lines):
        total = 0
        for line in lines:
            total += self.write(line) or 0
        return total

    def flush(self):
        return None

    def isatty(self):
        return False


class RustDisplayPublisher(DisplayPublisher):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, data, metadata=None, transient=None, update=False, **kwargs):
        self.events.append(
            {
                "type": "display",
                "data": data,
                "metadata": metadata or {},
                "transient": transient or {},
                "update": bool(update),
            }
        )

    def clear_output(self, wait=False):
        self.events.append({"type": "clear_output", "wait": bool(wait)})


class RustDisplayHook(DisplayHook):
    def __init__(self, shell=None):
        super().__init__(shell=shell)
        self.last = None
        self.last_metadata = None
        self.last_execution_count = None

    def write_output_prompt(self):
        self.last_execution_count = self.prompt_count

    def write_format_data(self, format_dict, md_dict=None):
        self.last = format_dict
        self.last_metadata = md_dict or {}

    def finish_displayhook(self):
        self._is_active = False


class RustCommManager:
    def __init__(self):
        self.comms = {}
        self.targets = {}
        self.events = []

    def register_target(self, name, handler):
        self.targets[name] = handler

    def comm_open(self, comm_id, target_name, data=None, metadata=None):
        data = data or {}
        metadata = metadata or {}
        self.comms[comm_id] = {"target_name": target_name}
        msg = {"content": {"comm_id": comm_id, "target_name": target_name, "data": data, "metadata": metadata}}
        self.events.append({"type": "open", "comm_id": comm_id, "target_name": target_name, "data": data})
        handler = self.targets.get(target_name)
        if handler is not None:
            handler(comm_id, msg)

    def comm_msg(self, comm_id, data=None, metadata=None):
        data = data or {}
        metadata = metadata or {}
        msg = {"content": {"comm_id": comm_id, "data": data, "metadata": metadata}}
        self.events.append({"type": "msg", "comm_id": comm_id, "data": data})
        comm = self.comms.get(comm_id)
        if comm is not None:
            handler = self.targets.get(comm.get("target_name"))
            if handler is not None:
                handler(comm_id, msg)

    def comm_close(self, comm_id, data=None, metadata=None):
        data = data or {}
        metadata = metadata or {}
        self.events.append({"type": "close", "comm_id": comm_id, "data": data})
        self.comms.pop(comm_id, None)

    def comm_info(self):
        return {comm_id: {"target_name": info["target_name"]} for comm_id, info in self.comms.items()}

    def clear_events(self):
        self.events.clear()


_COMM_MANAGER = RustCommManager()


class StdinNotImplementedError(RuntimeError):
    pass


def get_comm_manager():
    return _COMM_MANAGER


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value


class RustKernelBridge:
    def __init__(self):
        from IPython.core import page
        import builtins
        import getpass

        self.shell = InteractiveShell.instance()
        # Force non-jedi completions for consistent behavior in embedded mode.
        self.shell.Completer.use_jedi = False
        self._allow_stdin = False
        self.shell.display_pub = RustDisplayPublisher()
        self.shell.displayhook = RustDisplayHook(shell=self.shell)
        self.shell.display_trap.hook = self.shell.displayhook
        self._stream_events = []
        self._stdout = RustStream("stdout", self._stream_events)
        self._stderr = RustStream("stderr", self._stream_events)
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self.shell.set_hook("show_in_pager", page.as_hook(self._payloadpage_page), 99)
        self.shell._last_traceback = None

        from ipyrust_bridge import request_input

        def _rust_input(prompt=""):
            if not self._allow_stdin:
                msg = "raw_input was called, but this frontend does not support input requests."
                raise StdinNotImplementedError(msg)
            return request_input(str(prompt), False)

        def _rust_getpass(prompt="Password: ", stream=None):
            if not self._allow_stdin:
                msg = "getpass was called, but this frontend does not support input requests."
                raise StdinNotImplementedError(msg)
            return request_input(str(prompt), True)

        builtins.input = _rust_input
        getpass.getpass = _rust_getpass

        def _showtraceback(etype, evalue, stb):
            self.shell._last_traceback = stb

        self.shell._showtraceback = _showtraceback
        self._debugger = RustDebugger()

    def _payloadpage_page(self, strg, start=0, screen_lines=0, pager_cmd=None):
        start = max(0, start)
        data = strg if isinstance(strg, dict) else {"text/plain": strg}
        payload = {
            "source": "page",
            "data": data,
            "start": start,
        }
        self.shell.payload_manager.write_payload(payload)

    def _reset_capture_state(self):
        self.shell.display_pub.events.clear()
        self.shell.displayhook.last = None
        self.shell.displayhook.last_metadata = None
        self.shell.displayhook.last_execution_count = None
        self.shell._last_traceback = None
        self._stream_events.clear()

    def execute(self, code, silent=False, store_history=True, user_expressions=None, allow_stdin=False):
        self._reset_capture_state()
        self._allow_stdin = bool(allow_stdin)
        try:
            result = self.shell.run_cell(code, store_history=store_history, silent=silent)
        finally:
            self._allow_stdin = False

        payload = self.shell.payload_manager.read_payload()
        self.shell.payload_manager.clear_payload()

        error = None
        err = getattr(result, "error_in_exec", None) or getattr(result, "error_before_exec", None)
        if err is not None:
            error = {
                "ename": type(err).__name__,
                "evalue": str(err),
                "traceback": self.shell._last_traceback or [],
            }

        if user_expressions is None:
            user_expressions = {}
        user_expressions = _maybe_json(user_expressions) or {}
        if error is None:
            user_expr = self.shell.user_expressions(user_expressions)
        else:
            user_expr = {}

        exec_count = getattr(result, "execution_count", self.shell.execution_count)

        return json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "streams": list(self._stream_events),
                "display": list(self.shell.display_pub.events),
                "result": self.shell.displayhook.last,
                "result_metadata": self.shell.displayhook.last_metadata or {},
                "execution_count": exec_count,
                "error": error,
                "user_expressions": user_expr,
                "payload": payload,
            }
        )

    def complete(self, code, cursor_pos=None):
        if cursor_pos is None:
            cursor_pos = len(code)
        from IPython.utils.tokenutil import line_at_cursor

        line, offset = line_at_cursor(code, cursor_pos)
        line_cursor = cursor_pos - offset
        txt, matches = self.shell.complete("", line, line_cursor)
        return json.dumps(
            {
                "matches": matches,
                "cursor_start": cursor_pos - len(txt),
                "cursor_end": cursor_pos,
                "metadata": {},
                "status": "ok",
            }
        )

    def inspect(self, code, cursor_pos=None, detail_level=0):
        if cursor_pos is None:
            cursor_pos = len(code)
        try:
            from IPython.utils.tokenutil import token_at_cursor

            name = token_at_cursor(code, cursor_pos)
            bundle = self.shell.object_inspect_mime(name, detail_level=detail_level)
            if not self.shell.enable_html_pager:
                bundle.pop("text/html", None)
            return json.dumps(
                {
                    "status": "ok",
                    "found": True,
                    "data": bundle,
                    "metadata": {},
                }
            )
        except Exception:
            return json.dumps({"status": "ok", "found": False, "data": {}, "metadata": {}})

    def is_complete(self, code):
        tm = getattr(self.shell, "input_transformer_manager", None)
        if tm is None:
            tm = self.shell.input_splitter
        status, indent_spaces = tm.check_complete(code)
        reply = {"status": status}
        if status == "incomplete":
            reply["indent"] = " " * indent_spaces
        return json.dumps(reply)

    def history(
        self,
        hist_access_type,
        output,
        raw,
        session=0,
        start=0,
        stop=None,
        n=None,
        pattern=None,
        unique=False,
    ):
        if hist_access_type == "tail":
            hist = self.shell.history_manager.get_tail(
                n, raw=raw, output=output, include_latest=True
            )
        elif hist_access_type == "range":
            hist = self.shell.history_manager.get_range(
                session, start, stop, raw=raw, output=output
            )
        elif hist_access_type == "search":
            hist = self.shell.history_manager.search(
                pattern, raw=raw, output=output, n=n, unique=unique
            )
        else:
            hist = []
        return json.dumps({"status": "ok", "history": list(hist)})

    def _comm_action(self, action, *args, data=None, metadata=None):
        data = _maybe_json(data)
        metadata = _maybe_json(metadata)
        action(*args, data, metadata)
        return json.dumps({"status": "ok"})

    def comm_open(self, comm_id, target_name, data=None, metadata=None):
        return self._comm_action(_COMM_MANAGER.comm_open, comm_id, target_name, data=data, metadata=metadata)

    def comm_msg(self, comm_id, data=None, metadata=None):
        return self._comm_action(_COMM_MANAGER.comm_msg, comm_id, data=data, metadata=metadata)

    def comm_close(self, comm_id, data=None, metadata=None):
        return self._comm_action(_COMM_MANAGER.comm_close, comm_id, data=data, metadata=metadata)

    def comm_info(self):
        return json.dumps({"status": "ok", "comms": _COMM_MANAGER.comm_info()})

    def debug_available(self):
        return bool(_DEBUGPY_AVAILABLE)

    def debug_request(self, request_json):
        try:
            request = json.loads(request_json)
        except Exception:
            request = {}
        response, events = self._debugger.process_request(request)
        return json.dumps({"response": response, "events": events})
