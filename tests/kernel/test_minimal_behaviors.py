import time, zmq
from jupyter_client.session import Session

from ..kernel_utils import *


def _shell_addr(conn: dict)->str:
    transport = conn["transport"]
    ip = conn["ip"]
    port = conn["shell_port"]
    return f"{transport}://{ip}:{port}"


def _send_kernel_info(session: Session, sock: zmq.Socket): session.send(sock, "kernel_info_request", {})


def _recv_kernel_info(session: Session, sock: zmq.Socket, timeout:float)->dict|None:
    for _ in iter_timeout(timeout, default=timeout):
        if not sock.poll(50): continue
        try: _, msg = session.recv(sock, mode=0)
        except (ValueError, zmq.ZMQError): return None
        if msg and msg.get("msg_type") == "kernel_info_reply": return msg
    return None


def test_router_handover_same_identity():
    with start_kernel(ready_timeout=2) as (km, kc):
        kc.stop_channels()
        ctx = zmq.Context()
        sock1 = ctx.socket(zmq.DEALER)
        sock2 = ctx.socket(zmq.DEALER)
        sock1.linger = 0
        sock2.linger = 0
        identity = b"ipymini-handover"
        sock1.setsockopt(zmq.IDENTITY, identity)
        sock2.setsockopt(zmq.IDENTITY, identity)
        try:
            conn = km.get_connection_info(session=False)
            key = conn["key"]
            if isinstance(key, str): key = key.encode()
            session = Session(key=key, signature_scheme=conn["signature_scheme"])
            addr = _shell_addr(conn)
            sock1.connect(addr)
            time.sleep(0.05)

            _send_kernel_info(session, sock1)
            msg1 = _recv_kernel_info(session, sock1, timeout=0.5)
            assert msg1 is not None, "no kernel_info_reply on initial socket"

            sock2.connect(addr)
            time.sleep(0.05)
            _send_kernel_info(session, sock2)
            msg2 = _recv_kernel_info(session, sock2, timeout=0.5)
            assert msg2 is not None, "no kernel_info_reply on new socket"
        finally:
            sock1.close(0)
            sock2.close(0)
            ctx.term()


def test_execute_reply_after_keyboardinterrupt_during_send():
    with start_kernel() as (_, kc):
        patch = """import ipymini.kernel as _k
if not hasattr(_k, "_orig_send_reply"):
    _k._orig_send_reply = _k.Subshell.send_reply
_k._interrupt_reply_once = True
def _send_reply(self, msg_type, content, parent, idents):
    code = parent.get("content", {}).get("code")
    if msg_type == "execute_reply" and code == "1+1" and _k._interrupt_reply_once:
        _k._interrupt_reply_once = False
        raise KeyboardInterrupt("simulated interrupt")
    return _k._orig_send_reply(self, msg_type, content, parent, idents)
_k.Subshell.send_reply = _send_reply
"""
        msg_id = kc.execute(patch)
        reply = kc.shell_reply(msg_id, timeout=10)
        assert reply["content"]["status"] == "ok", f"patch reply: {reply.get('content')}"

        msg_id = kc.execute("1+1")
        reply = kc.shell_reply(msg_id, timeout=10)
        assert reply["content"]["status"] == "error", f"interrupt reply: {reply.get('content')}"
        assert reply["content"].get("ename") == "KeyboardInterrupt", f"interrupt ename: {reply.get('content')}"
        outputs = kc.iopub_drain(msg_id)
        errors = iopub_msgs(outputs, "error")
        assert errors, f"missing iopub error: {[m.get('msg_type') for m in outputs]}"
        assert errors[-1]["content"].get("ename") == "KeyboardInterrupt", f"iopub error: {errors[-1].get('content')}"


def test_uncollected_execute_requests_do_not_wedge_iopub():
    with start_kernel() as (_, kc):
        for _ in range(3):
            for i in range(30): kc.execute(f"__u{i}={i}")
            msg_id = kc.execute("1+1")
            reply = kc.shell_reply(msg_id, timeout=5)
            assert reply["content"]["status"] == "ok", f"reply: {reply.get('content')}"
            kc.iopub_drain(msg_id, timeout=5)
