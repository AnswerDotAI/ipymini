import asyncio
from ..aclient import *
from ..kernel_utils import iopub_streams

def _stream_text(outputs: list[dict])->str: return "".join(m["content"].get("text", "") for m in iopub_streams(outputs, "stdout"))


async def test_thread_stdout_stays_with_launch_cell():
    async with mini_kernel() as (_, kc):
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
            "print('CELL1-LINE', flush=True)\n")
        code_next = "thread_go.set(); bg.join(timeout=5); print('CELL2-LINE', flush=True)"

        c_launch = kc.execute(code_launch, reply=True, timeout=10, store_history=False)
        c_next = kc.execute(code_next, reply=True, timeout=10, store_history=False)
        reply_launch, reply_next = await asyncio.gather(c_launch, c_next)
        mid_launch, mid_next = parent_id(reply_launch), parent_id(reply_next)

        assert reply_launch["content"]["status"] == "ok"
        assert reply_next["content"]["status"] == "ok"

        outputs = await collect_iopub(kc, {mid_launch, mid_next})
        launch_text = _stream_text(outputs[mid_launch])
        next_text = _stream_text(outputs[mid_next])
        assert "CELL1-LINE\n" in launch_text
        assert "CELL2-LINE\n" in next_text
        assert "THREAD-LINE\n" in launch_text
        assert "THREAD-LINE\n" not in next_text

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
            "print('CELL1-SUB-LINE', flush=True)\n")
        code_next = "thread_go.set(); bg.join(timeout=5); print('CELL2-SUB-LINE', flush=True)"

        c_launch = kc.execute(code_launch, reply=True, timeout=10, store_history=False)
        c_next = kc.execute(code_next, reply=True, timeout=10, store_history=False)
        reply_launch, reply_next = await asyncio.gather(c_launch, c_next)
        mid_launch, mid_next = parent_id(reply_launch), parent_id(reply_next)

        assert reply_launch["content"]["status"] == "ok"
        assert reply_next["content"]["status"] == "ok"

        outputs = await collect_iopub(kc, {mid_launch, mid_next})
        launch_text = _stream_text(outputs[mid_launch])
        next_text = _stream_text(outputs[mid_next])
        assert "CELL1-SUB-LINE\n" in launch_text
        assert "CELL2-SUB-LINE\n" in next_text
        assert "THREAD-SUB-LINE\n" in launch_text
        assert "THREAD-SUB-LINE\n" not in next_text
