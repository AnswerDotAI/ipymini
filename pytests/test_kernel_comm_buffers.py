from .kernel_utils import execute_and_drain, iopub_msgs, iopub_streams, start_kernel


def send_shell_with_buffers(kc, msg_type: str, content: dict, buffers: list[bytes]) -> str:
    "Send a shell message with binary buffers and return msg_id."
    msg = kc.session.msg(msg_type, content)
    kc.session.send(kc.shell_channel.socket, msg, buffers=buffers)
    return msg["header"]["msg_id"]


def test_comm_buffers_from_kernel() -> None:
    with start_kernel() as (_, kc):
        code = (
            "from comm import create_comm\n"
            "c = create_comm(target_name='buf-test', buffers=[b'openbuf'])\n"
            "c.send(data={'x': 1}, buffers=[b'msgbuf'])\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"
        comm_msgs = iopub_msgs(output_msgs, "comm_msg")
        assert comm_msgs, "expected comm_msg on iopub"
        buffers = comm_msgs[-1].get("buffers") or []
        assert buffers and bytes(buffers[0]) == b"msgbuf"
        comm_opens = iopub_msgs(output_msgs, "comm_open")
        assert comm_opens, "expected comm_open on iopub"
        open_buffers = comm_opens[-1].get("buffers") or []
        assert open_buffers and bytes(open_buffers[0]) == b"openbuf"


def test_comm_buffers_to_kernel() -> None:
    with start_kernel() as (_, kc):
        setup = (
            "from comm import get_comm_manager\n"
            "received = {}\n"
            "def _handler(comm, msg):\n"
            "    received['open'] = [bytes(b) for b in (msg.get('buffers') or [])]\n"
            "    def _on_msg(m):\n"
            "        received['msg'] = [bytes(b) for b in (m.get('buffers') or [])]\n"
            "    comm.on_msg(_on_msg)\n"
            "get_comm_manager().register_target('buf_target', _handler)\n"
        )
        _, reply, _ = execute_and_drain(kc, setup, store_history=False)
        assert reply["content"]["status"] == "ok"

        comm_id = "buf-1"
        send_shell_with_buffers(kc, "comm_open", dict(comm_id=comm_id, target_name="buf_target", data={}), [b"open"])
        send_shell_with_buffers(kc, "comm_msg", dict(comm_id=comm_id, data={}), [b"msg"])

        code = (
            "import time\n"
            "deadline = time.time() + 5\n"
            "while 'msg' not in received and time.time() < deadline:\n"
            "    time.sleep(0.05)\n"
            "print(received.get('open'), received.get('msg'))\n"
        )
        _, reply, output_msgs = execute_and_drain(kc, code, store_history=False)
        assert reply["content"]["status"] == "ok"
        streams = "".join(m["content"]["text"] for m in iopub_streams(output_msgs))
        assert "b'open'" in streams
        assert "b'msg'" in streams
