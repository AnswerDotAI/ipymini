from .kernel_utils import *


def test_connect_request(kernel_harness):
    kc = kernel_harness.kc
    msg_id = kc.cmd.connect_request()
    reply = kc.shell_reply(msg_id)
    content = reply["content"]
    conn = load_connection(kernel_harness.km)

    assert content["shell_port"] == conn["shell_port"]
    assert content["iopub_port"] == conn["iopub_port"]
    assert content["stdin_port"] == conn["stdin_port"]
    assert content["control_port"] == conn["control_port"]
    assert content["hb_port"] == conn["hb_port"]
