from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel


def _send_comm(kc, msg_type, content):
    msg = kc.session.msg(msg_type, content)
    kc.shell_channel.send(msg)
    return msg["header"]["msg_id"]


def test_comm_lifecycle() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.execute(
            "import ipymini_bridge\n"
            "mgr = ipymini_bridge.get_comm_manager()\n"
            "mgr.clear_events()\n"
            "\n"
            "def _handler(comm_id, msg):\n"
            "    mgr.events.append({'type': 'target', 'comm_id': comm_id, 'data': msg['content'].get('data', {})})\n"
            "\n"
            "mgr.register_target('test_target', _handler)\n",
            store_history=False,
        )
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"

        comm_id = "test-comm-1"
        _send_comm(
            kc,
            "comm_open",
            {"comm_id": comm_id, "target_name": "test_target", "data": {"value": 1}, "metadata": {}},
        )
        _send_comm(
            kc,
            "comm_msg",
            {"comm_id": comm_id, "data": {"value": 2}, "metadata": {}},
        )
        _send_comm(
            kc,
            "comm_close",
            {"comm_id": comm_id, "data": {}, "metadata": {}},
        )

        got = []
        while len(got) < 3:
            msg = kc.iopub_channel.get_msg(timeout=2)
            if msg["msg_type"] in {"comm_open", "comm_msg", "comm_close"}:
                got.append(msg["msg_type"])
        assert got == ["comm_open", "comm_msg", "comm_close"]

        msg_id = kc.execute(
            "import json, ipymini_bridge\n"
            "print(json.dumps(ipymini_bridge.get_comm_manager().events))\n",
            store_history=False,
        )
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        output_msgs = drain_iopub(kc, msg_id)
        streams = iopub_msgs(output_msgs, "stream")
        assert streams
        events = streams[-1]["content"]["text"]
        assert "open" in events
        assert "msg" in events
        assert "close" in events
        assert "target" in events


def test_comm_info() -> None:
    with start_kernel() as (_, kc):
        comm_id = "test-comm-info"
        _send_comm(
            kc,
            "comm_open",
            {"comm_id": comm_id, "target_name": "test_target", "data": {}, "metadata": {}},
        )
        msg_id = kc.comm_info()
        reply = get_shell_reply(kc, msg_id)
        assert reply["content"]["status"] == "ok"
        assert comm_id in reply["content"]["comms"]
