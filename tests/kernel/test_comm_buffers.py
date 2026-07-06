from ..aclient import *
from ..kernel_utils import iopub_msgs

async def test_comm_buffer_roundtrips():
    async with mini_kernel() as (_, kc):
        code = (
            "from comm import create_comm\n"
            "c = create_comm(target_name='buf-test', buffers=[b'openbuf'])\n"
            "c.send(data={'x': 1}, buffers=[b'msgbuf'])\n")
        reply, output_msgs = await kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        comm_msgs = iopub_msgs(output_msgs, "comm_msg")
        assert comm_msgs, "expected comm_msg on iopub"
        buffers = comm_msgs[-1].get("buffers") or []
        assert buffers and bytes(buffers[0]) == b"msgbuf"
        comm_opens = iopub_msgs(output_msgs, "comm_open")
        assert comm_opens, "expected comm_open on iopub"
        open_buffers = comm_opens[-1].get("buffers") or []
        assert open_buffers and bytes(open_buffers[0]) == b"openbuf"

        setup = (
            "from comm import get_comm_manager\n"
            "received = {}\n"
            "def _handler(comm, msg):\n"
            "    received['open'] = [bytes(b) for b in (msg.get('buffers') or [])]\n"
            "    def _on_msg(m):\n"
            "        received['msg'] = [bytes(b) for b in (m.get('buffers') or [])]\n"
            "    comm.on_msg(_on_msg)\n"
            "get_comm_manager().register_target('buf_target', _handler)\n")
        reply, _ = await kc.exec_drain(setup, store_history=False)
        assert reply["content"]["status"] == "ok"

        comm_id = "buf-1"
        kc.shell_request("comm_open", reply=False, comm_id=comm_id, target_name="buf_target", data={}, buffers=[b"open"])
        kc.shell_request("comm_msg", reply=False, comm_id=comm_id, data={}, buffers=[b"msg"])

        code = (
            "import time\n"
            "deadline = time.monotonic() + 5\n"
            "while 'msg' not in received and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n")
        reply, _ = await kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok"
        assert (r := await kc.eval_expr("received")) == dict(open=[b"open"], msg=[b"msg"]), r
