from .kernel_utils import get_shell_reply, load_connection, start_kernel


def test_connect_request() -> None:
    with start_kernel() as (km, kc):
        msg = kc.session.msg("connect_request", content={})
        kc.shell_channel.send(msg)
        msg_id = msg["header"]["msg_id"]
        reply = get_shell_reply(kc, msg_id)
        content = reply["content"]
        conn = load_connection(km)

        assert content["shell_port"] == conn["shell_port"]
        assert content["iopub_port"] == conn["iopub_port"]
        assert content["stdin_port"] == conn["stdin_port"]
        assert content["control_port"] == conn["control_port"]
        assert content["hb_port"] == conn["hb_port"]
