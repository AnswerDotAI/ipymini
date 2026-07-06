import zmq
from uuid import uuid4
from ..aclient import *
from ..kernel_utils import load_connection

async def test_kernel_info_and_connect_fields():
    async with mini_kernel() as (km, kc):
        msg_id = str(uuid4())
        reply = await kc.shell_request("kernel_info_request", msg_id=msg_id)
        content = reply["content"]
        assert reply["parent_header"]["msg_id"] == msg_id
        assert content["status"] == "ok"
        assert content["protocol_version"] == "5.3"
        assert content["implementation"] == "ipymini"
        assert content["implementation_version"]
        language = content["language_info"]
        assert language["name"] == "python"
        assert language["file_extension"] == ".py"

        reply = await kc.cmd.connect()
        content = reply["content"]
        conn = load_connection(km)

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
