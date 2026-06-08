import zmq
from ..kernel_utils import *


def test_kernel_info_and_connect_fields(kernel_harness):
    msg_id, reply = kernel_harness.send_wait("kernel_info_request")
    content = reply["content"]
    assert reply["parent_header"]["msg_id"] == msg_id
    assert content["status"] == "ok"
    assert content["protocol_version"] == "5.3"
    assert content["implementation"] == "ipymini"
    assert content["implementation_version"]
    language = content["language_info"]
    assert language["name"] == "python"
    assert language["file_extension"] == ".py"

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

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.linger = 0
    sock.connect(f"{conn['transport']}://{conn['ip']}:{conn['hb_port']}")
    payload = b"ping"
    sock.send(payload)
    reply = sock.recv()
    sock.close(0)
    assert reply == payload
