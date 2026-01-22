import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

import pytest
import zmq
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "jupyter_kernel_test"))

from python_env import ensure_env

ensure_env(extra_pythonpath=[str(ROOT / "jupyter_kernel_test")])

import jupyter_kernel_test as jkt
from jupyter_kernel_test import validate_message
from jupyter_client.session import Session

TIMEOUT = jkt.TIMEOUT


class IPyRustKernelTests(jkt.KernelTests):
    kernel_name = "ipyrust"
    language_name = "python"
    file_extension = ".py"

    code_hello_world = "print('hello, world')"
    code_stderr = "import sys; print('test', file=sys.stderr)"
    completion_samples = [
        {"text": "pri", "matches": {"print"}},
        {"text": "from sys imp", "matches": {"import "}},
    ]
    complete_code_samples = ["1", "print('hello, world')", "def f(x):\n  return x*2\n\n\n"]
    incomplete_code_samples = ["print('''hello", "def f(x):\n  x*2"]
    invalid_code_samples = ["import = 7q"]
    code_page_something = "print?"
    code_generate_error = "1/0"
    code_execute_result = [
        {"code": "1+2+3", "result": "6"},
        {"code": "[n*n for n in range(1, 4)]", "result": "[1, 4, 9]"},
    ]
    code_display_data = [
        {
            "code": "from IPython.display import HTML, display; display(HTML('<b>test</b>'))",
            "mime": "text/html",
        },
        {
            "code": "from IPython.display import Math, display; display(Math('\\\\frac{1}{2}'))",
            "mime": "text/latex",
        },
    ]
    code_history_pattern = "1?2*"
    supported_history_operations = ("tail", "search")
    code_inspect_sample = "open"
    code_clear_output = "from IPython.display import clear_output; clear_output()"

    def _drain_channels(self, *, validate=True, ignore_types=None):
        ignore_types = set(ignore_types or ())
        for channel in (self.kc.shell_channel, self.kc.iopub_channel):
            while True:
                try:
                    msg = channel.get_msg(timeout=0.1)
                except Exception:
                    break
                else:
                    if msg.get("msg_type") in ignore_types:
                        continue
                    if validate:
                        validate_message(msg)

    def flush_channels(self):
        self._drain_channels(ignore_types={"iopub_welcome", "debug_event"})

    @classmethod
    def setUpClass(cls):
        repo_root = ROOT
        jupyter_path = repo_root / "share" / "jupyter"
        current_jupyter_path = os.environ.get("JUPYTER_PATH", "")
        os.environ["JUPYTER_PATH"] = (
            f"{jupyter_path}{os.pathsep}{current_jupyter_path}"
            if current_jupyter_path
            else str(jupyter_path)
        )
        jkt_path = str(repo_root / "jupyter_kernel_test")
        current_path = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = f"{jkt_path}{os.pathsep}{current_path}" if current_path else jkt_path

        bin_dir = repo_root / "target" / "debug"
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}" + os.environ.get("PATH", "")

        if shutil.which("ipyrust") is None:
            raise unittest.SkipTest("ipyrust binary not found in PATH; build with `cargo build` first")

        super().setUpClass()

    def test_interrupt_request(self):
        self.flush_channels()
        msg_id = self.kc.execute("import time; time.sleep(5)")

        # wait for busy
        while True:
            msg = self.kc.get_iopub_msg(timeout=2)
            if msg["msg_type"] == "status":
                if msg["content"]["execution_state"] == "busy":
                    break

        self.km.interrupt_kernel()

        reply = self.kc.get_shell_msg(timeout=10)
        validate_message(reply, "execute_reply", msg_id)
        self.assertEqual(reply["content"]["status"], "error")
        self.assertIn(reply["content"].get("ename"), {"KeyboardInterrupt", "InterruptedError"})

        # drain iopub until idle
        while True:
            msg = self.kc.get_iopub_msg(timeout=2)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break

    def test_stream_ordering(self):
        self.flush_channels()
        code = "import sys; print('out1'); print('err1', file=sys.stderr); print('out2')"
        reply, output_msgs = self.execute_helper(code=code)
        self.assertEqual(reply["content"]["status"], "ok")
        streams = [
            (msg["content"]["name"], msg["content"]["text"])
            for msg in output_msgs
            if msg["msg_type"] == "stream"
        ]
        self.assertEqual(
            streams,
            [
                ("stdout", "out1\n"),
                ("stderr", "err1\n"),
                ("stdout", "out2\n"),
            ],
        )

    def test_clear_output_wait(self):
        self.flush_channels()
        reply, output_msgs = self.execute_helper(
            code="from IPython.display import clear_output; clear_output(wait=True)"
        )
        self.assertEqual(reply["content"]["status"], "ok")
        waits = [msg["content"]["wait"] for msg in output_msgs if msg["msg_type"] == "clear_output"]
        self.assertIn(True, waits)

    def test_display_id_update(self):
        self.flush_channels()
        code = """\nfrom IPython.display import display\nh = display('first', display_id=True)\nh.update('second')\n"""
        reply, output_msgs = self.execute_helper(code=code)
        self.assertEqual(reply["content"]["status"], "ok")
        msgs = [m for m in output_msgs if m["msg_type"] in ("display_data", "update_display_data")]
        self.assertGreaterEqual(len(msgs), 2)
        first = msgs[0]
        second = msgs[1]
        self.assertEqual(first["msg_type"], "display_data")
        self.assertEqual(second["msg_type"], "update_display_data")
        display_id = first["content"].get("transient", {}).get("display_id")
        self.assertTrue(display_id)
        update_id = second["content"].get("transient", {}).get("display_id")
        self.assertEqual(update_id, display_id)

    def test_display_metadata_transient(self):
        self.flush_channels()
        code = (
            "from IPython.display import display\n"
            "display({'text/plain': 'hi'}, raw=True, metadata={'foo': 'bar'}, transient={'display_id': 'xyz'})\n"
        )
        reply, output_msgs = self.execute_helper(code=code)
        self.assertEqual(reply["content"]["status"], "ok")
        displays = [m for m in output_msgs if m["msg_type"] == "display_data"]
        self.assertTrue(displays, "expected at least one display_data message")
        content = displays[0]["content"]
        self.assertEqual(content.get("metadata", {}).get("foo"), "bar")
        self.assertEqual(content.get("transient", {}).get("display_id"), "xyz")

    def test_iopub_welcome(self):
        self.flush_channels()
        with open(self.km.connection_file, encoding="utf-8") as f:
            conn = json.load(f)
        session = Session(
            key=conn.get("key", "").encode(),
            signature_scheme=conn.get("signature_scheme", "hmac-sha256"),
        )

        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(f"{conn['transport']}://{conn['ip']}:{conn['iopub_port']}")

        deadline = time.time() + 5
        msg = None
        while time.time() < deadline:
            try:
                frames = sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.05)
                continue
            if b"<IDS|MSG>" in frames:
                idx = frames.index(b"<IDS|MSG>")
                msg = session.deserialize(frames[idx + 1 :])
            else:
                msg = session.deserialize(frames)
            if msg.get("header", {}).get("msg_type") == "iopub_welcome":
                break
        sub.close(0)

        self.assertIsNotNone(msg, "did not receive any iopub messages")
        self.assertEqual(msg["header"]["msg_type"], "iopub_welcome")

    def test_matplotlib_inline_display(self):
        pytest.importorskip("matplotlib")
        self.flush_channels()
        code = (
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "from io import BytesIO\n"
            "from IPython.display import Image, display\n"
            "fig, ax = plt.subplots()\n"
            "ax.plot([1, 2, 3], [1, 4, 9])\n"
            "buf = BytesIO()\n"
            "fig.savefig(buf, format='png')\n"
            "display(Image(data=buf.getvalue()))\n"
        )
        reply, output_msgs = self.execute_helper(code=code)
        self.assertEqual(reply["content"]["status"], "ok")
        displays = [m for m in output_msgs if m["msg_type"] == "display_data"]
        self.assertTrue(displays, "expected display_data from matplotlib image")
        data = displays[0]["content"].get("data", {})
        self.assertIn("image/png", data)

    _debug_seq = 0

    def _send_debug_request(self, command, arguments=None):
        self.__class__._debug_seq += 1
        msg = self.kc.session.msg(
            "debug_request",
            {
                "type": "request",
                "seq": self.__class__._debug_seq,
                "command": command,
                "arguments": arguments or {},
            },
        )
        self.kc.control_channel.send(msg)
        reply = self.kc.control_channel.get_msg(timeout=TIMEOUT)
        self.assertEqual(reply["header"]["msg_type"], "debug_reply")
        self.assertEqual(reply["parent_header"].get("msg_id"), msg["header"]["msg_id"])
        return reply["content"]

    def _wait_for_debug_event(self, event_name, timeout=TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.kc.iopub_channel.get_msg(timeout=timeout)
            if msg.get("msg_type") == "debug_event":
                if msg.get("content", {}).get("event") == event_name:
                    return msg
        raise AssertionError(f"debug_event {event_name} not received")

    def test_debug_initialize(self):
        self._flush_channels_raw()
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        reply = self._send_debug_request(
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        if debugpy:
            self.assertTrue(reply.get("success"), msg=reply)
        else:
            self.assertEqual(reply, {})

    def test_debug_attach(self):
        self._flush_channels_raw()
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        self._send_debug_request(
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        reply = self._send_debug_request("attach")
        if debugpy:
            self.assertTrue(reply.get("success"), msg=reply)
            self._wait_for_debug_event("initialized")
        else:
            self.assertEqual(reply, {})

    def test_debug_evaluate(self):
        self._flush_channels_raw()
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        self._send_debug_request(
            "initialize",
            {
                "clientID": "test-client",
                "clientName": "testClient",
                "adapterID": "",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "locale": "en",
            },
        )
        self._send_debug_request("attach")
        reply = self._send_debug_request(
            "evaluate", {"expression": "'a' + 'b'", "context": "repl"}
        )
        if debugpy:
            self.assertTrue(reply.get("success"), msg=reply)
        else:
            self.assertEqual(reply, {})

    def test_input_request(self):
        self.flush_channels()
        msg_id = self.kc.execute("print(input('prompt> '))", allow_stdin=True)
        stdin_msg = self.kc.get_stdin_msg(timeout=TIMEOUT)
        self.assertEqual(stdin_msg["msg_type"], "input_request")
        self.assertEqual(stdin_msg["content"]["prompt"], "prompt> ")
        self.assertFalse(stdin_msg["content"]["password"])

        text = "some text"
        self.kc.input(text)

        reply = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply, "execute_reply", msg_id)
        self.assertEqual(reply["content"]["status"], "ok")

        output_msgs = []
        while True:
            msg = self.kc.iopub_channel.get_msg(timeout=2)
            validate_message(msg, msg["msg_type"], msg_id)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break
            output_msgs.append(msg)
        streams = [
            (msg["content"]["name"], msg["content"]["text"])
            for msg in output_msgs
            if msg["msg_type"] == "stream"
        ]
        self.assertIn(("stdout", text + "\n"), streams)

    def test_input_request_disallowed(self):
        self.flush_channels()
        msg_id = self.kc.execute("input('prompt> ')", allow_stdin=False)

        with self.assertRaises(Exception):
            _ = self.kc.get_stdin_msg(timeout=1)

        reply = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply, "execute_reply", msg_id)
        self.assertEqual(reply["content"]["status"], "error")
        self.assertEqual(reply["content"]["ename"], "StdinNotImplementedError")

        self._drain_iopub_until_idle(msg_id)

    def _drain_iopub_until_idle(self, msg_id):
        while True:
            msg = self.kc.iopub_channel.get_msg(timeout=2)
            validate_message(msg, msg["msg_type"], msg_id)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break

    def _flush_channels_raw(self):
        self._drain_channels(validate=False)

    def test_execute_silent_no_output(self):
        self.flush_channels()
        msg_id = self.kc.execute("print('hi')", silent=True)
        reply = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply, "execute_reply", msg_id)
        self.assertEqual(reply["content"]["status"], "ok")

        output_msgs = []
        while True:
            msg = self.kc.iopub_channel.get_msg(timeout=2)
            validate_message(msg, msg["msg_type"], msg_id)
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break
            output_msgs.append(msg)
        self.assertFalse(
            any(msg["msg_type"] in {"stream", "execute_result", "display_data"} for msg in output_msgs)
        )

    def test_store_history_false(self):
        self.flush_channels()
        msg_id1 = self.kc.execute("1+1")
        reply1 = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply1, "execute_reply", msg_id1)
        self._drain_iopub_until_idle(msg_id1)
        count1 = reply1["content"]["execution_count"]

        msg_id2 = self.kc.execute("2+2", store_history=False)
        reply2 = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply2, "execute_reply", msg_id2)
        self._drain_iopub_until_idle(msg_id2)
        count2 = reply2["content"]["execution_count"]

        msg_id3 = self.kc.execute("3+3")
        reply3 = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply3, "execute_reply", msg_id3)
        self._drain_iopub_until_idle(msg_id3)
        count3 = reply3["content"]["execution_count"]

        self.assertEqual(count2, count1 + 1)
        self.assertEqual(count3, count2)

    def test_user_expressions(self):
        self.flush_channels()
        msg_id = self.kc.execute(
            "a = 10",
            user_expressions={"x": "a+1", "bad": "1/0"},
        )
        reply = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply, "execute_reply", msg_id)
        self._drain_iopub_until_idle(msg_id)
        expr = reply["content"]["user_expressions"]
        self.assertEqual(expr["x"]["status"], "ok")
        self.assertEqual(expr["x"]["data"]["text/plain"], "11")
        self.assertEqual(expr["bad"]["status"], "error")

    def _send_comm(self, msg_type, content):
        msg = self.kc.session.msg(msg_type, content)
        self.kc.shell_channel.send(msg)
        return msg["header"]["msg_id"]

    def test_comm_lifecycle(self):
        self._flush_channels_raw()
        self.execute_helper(
            code="""
import ipyrust_bridge
mgr = ipyrust_bridge.get_comm_manager()
mgr.clear_events()

def _handler(comm_id, msg):
    mgr.events.append({"type": "target", "comm_id": comm_id, "data": msg["content"].get("data", {})})

mgr.register_target("test_target", _handler)
"""
        )

        comm_id = "test-comm-1"
        self._send_comm(
            "comm_open",
            {"comm_id": comm_id, "target_name": "test_target", "data": {"value": 1}, "metadata": {}},
        )
        self._send_comm(
            "comm_msg",
            {"comm_id": comm_id, "data": {"value": 2}, "metadata": {}},
        )
        self._send_comm(
            "comm_close",
            {"comm_id": comm_id, "data": {}, "metadata": {}},
        )

        # Collect iopub comm messages
        got = []
        while len(got) < 3:
            msg = self.kc.iopub_channel.get_msg(timeout=2)
            if msg["msg_type"] in {"comm_open", "comm_msg", "comm_close"}:
                got.append(msg["msg_type"])
        self.assertEqual(got, ["comm_open", "comm_msg", "comm_close"])

        reply, output_msgs = self.execute_helper(
            code="""
import json, ipyrust_bridge
print(json.dumps(ipyrust_bridge.get_comm_manager().events))
"""
        )
        streams = [m for m in output_msgs if m["msg_type"] == "stream"]
        self.assertTrue(streams)
        events = streams[-1]["content"]["text"]
        self.assertIn("open", events)
        self.assertIn("msg", events)
        self.assertIn("close", events)
        self.assertIn("target", events)

    def test_comm_info(self):
        self._flush_channels_raw()
        comm_id = "test-comm-info"
        self._send_comm(
            "comm_open",
            {"comm_id": comm_id, "target_name": "test_target", "data": {}, "metadata": {}},
        )
        msg_id = self.kc.comm_info()
        reply = self.get_non_kernel_info_reply(timeout=TIMEOUT)
        validate_message(reply, "comm_info_reply", msg_id)
        self.assertIn(comm_id, reply["content"]["comms"])


if __name__ == "__main__":
    unittest.main()
