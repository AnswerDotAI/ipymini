from queue import Empty
from ..kernel_utils import *


def test_kernel_comm_manager_exposed():
    "ipywidgets-style libs reach the manager via get_ipython().kernel.comm_manager."
    with start_kernel() as (_, kc):
        code = ("from comm import get_comm_manager\n"
                "assert get_ipython().kernel.comm_manager is get_comm_manager()\n"
                "print('cm-ok')\n")
        _, reply, out = kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok", reply["content"]
        assert "cm-ok" in "".join(m["content"]["text"] for m in iopub_streams(out))


_replier = """from comm import get_comm_manager
def _target(comm, open_msg):
    comm.send(data={'ack': open_msg['content']['data'].get('n')})
    comm.on_msg(lambda m: comm.send(data={'echo': m['content']['data']}))
get_comm_manager().register_target('replier', _target)
"""


def test_comm_callback_reply_reaches_iopub():
    "A target/on_msg callback that replies via comm.send() reaches iopub, parented to the inbound msg."
    with start_kernel() as (_, kc):
        assert kc.exec_drain(_replier, store_history=False)[1]["content"]["status"] == "ok"
        cid = "rep-1"
        open_id = kc.cmd.comm_open(comm_id=cid, target_name="replier", data={"n": 7})
        ack = wait_for_msg(kc.get_iopub_msg,
            lambda m: m["msg_type"] == "comm_msg" and m["content"].get("comm_id") == cid and "ack" in m["content"].get("data", {}),
            timeout=10, err="open-callback reply never reached iopub")
        assert ack["content"]["data"] == {"ack": 7}
        assert parent_id(ack) == open_id, "reply parent should be the inbound comm_open"
        msg_id = kc.cmd.comm_msg(comm_id=cid, data={"hi": 1})
        echo = wait_for_msg(kc.get_iopub_msg,
            lambda m: m["msg_type"] == "comm_msg" and m["content"].get("comm_id") == cid and "echo" in m["content"].get("data", {}),
            timeout=10, err="on_msg-callback reply never reached iopub")
        assert echo["content"]["data"] == {"echo": {"hi": 1}}
        assert parent_id(echo) == msg_id, "reply parent should be the inbound comm_msg"


_silent = """from comm import get_comm_manager
seen = []
def _target(comm, open_msg): comm.on_msg(lambda m: seen.append(m['content']['data']))
get_comm_manager().register_target('silent', _target)
"""


def test_inbound_comm_not_echoed():
    "Inbound comm messages drive callbacks; they are not reflected back on iopub (ipykernel parity)."
    with start_kernel() as (_, kc):
        assert kc.exec_drain(_silent, store_history=False)[1]["content"]["status"] == "ok"
        cid = "sil-1"
        kc.cmd.comm_open(comm_id=cid, target_name="silent", data={})
        kc.cmd.comm_msg(comm_id=cid, data={"x": 1})
        bid = kc.execute("assert seen == [{'x': 1}], seen")  # barrier: runs after the comms (FIFO)
        echoed = []
        for _ in iter_timeout(10):
            try: m = kc.get_iopub_msg(timeout=0.5)
            except Empty: continue
            if m["msg_type"] in ("comm_open", "comm_msg") and m["content"].get("comm_id") == cid: echoed.append(m["msg_type"])
            if parent_id(m) == bid and m["msg_type"] == "status" and m["content"].get("execution_state") == "idle": break
        assert not echoed, f"inbound comm should not be echoed on iopub: {echoed}"
