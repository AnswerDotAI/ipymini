from ..kernel_utils import *


def _stream_text(outputs: list[dict])->str: return "".join(m["content"].get("text", "") for m in iopub_streams(outputs, "stdout"))


def test_thread_stdout_stays_with_launch_cell():
    with start_kernel() as (_, kc):
        code_launch = (
            "import threading\n"
            "thread_ready = threading.Event()\n"
            "thread_go = threading.Event()\n"
            "def _bg_worker():\n"
            "    thread_ready.set()\n"
            "    thread_go.wait(5)\n"
            "    print('THREAD-LINE', flush=True)\n"
            "bg = threading.Thread(target=_bg_worker)\n"
            "bg.start()\n"
            "thread_ready.wait(5)\n"
            "print('CELL1-LINE', flush=True)\n"
        )
        code_next = "thread_go.set(); bg.join(timeout=5); print('CELL2-LINE', flush=True)"

        msg_launch = kc.execute(code_launch, store_history=False)
        msg_next = kc.execute(code_next, store_history=False)
        replies = collect_shell_replies(kc, {msg_launch, msg_next})
        outputs = collect_iopub_outputs(kc, {msg_launch, msg_next})

        assert replies[msg_launch]["content"]["status"] == "ok"
        assert replies[msg_next]["content"]["status"] == "ok"

        launch_text = _stream_text(outputs[msg_launch])
        next_text = _stream_text(outputs[msg_next])
        assert "CELL1-LINE\n" in launch_text
        assert "CELL2-LINE\n" in next_text
        assert "THREAD-LINE\n" in launch_text
        assert "THREAD-LINE\n" not in next_text


def test_thread_subclass_run_stdout_stays_with_launch_cell():
    with start_kernel() as (_, kc):
        code_launch = (
            "import threading\n"
            "thread_ready = threading.Event()\n"
            "thread_go = threading.Event()\n"
            "class MyThread(threading.Thread):\n"
            "    def run(self):\n"
            "        thread_ready.set()\n"
            "        thread_go.wait(5)\n"
            "        print('THREAD-SUB-LINE', flush=True)\n"
            "bg = MyThread()\n"
            "bg.start()\n"
            "thread_ready.wait(5)\n"
            "print('CELL1-SUB-LINE', flush=True)\n"
        )
        code_next = "thread_go.set(); bg.join(timeout=5); print('CELL2-SUB-LINE', flush=True)"

        msg_launch = kc.execute(code_launch, store_history=False)
        msg_next = kc.execute(code_next, store_history=False)
        replies = collect_shell_replies(kc, {msg_launch, msg_next})
        outputs = collect_iopub_outputs(kc, {msg_launch, msg_next})

        assert replies[msg_launch]["content"]["status"] == "ok"
        assert replies[msg_next]["content"]["status"] == "ok"

        launch_text = _stream_text(outputs[msg_launch])
        next_text = _stream_text(outputs[msg_next])
        assert "CELL1-SUB-LINE\n" in launch_text
        assert "CELL2-SUB-LINE\n" in next_text
        assert "THREAD-SUB-LINE\n" in launch_text
        assert "THREAD-SUB-LINE\n" not in next_text
