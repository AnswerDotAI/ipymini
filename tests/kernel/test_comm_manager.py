from uuid import uuid4
from ..aclient import *
from ..kernel_utils import iopub_streams

async def test_kernel_comm_manager_exposed():
    "ipywidgets-style libs reach the manager via get_ipython().kernel.comm_manager."
    async with mini_kernel() as (_, kc):
        code = ("from comm import get_comm_manager\n"
            "assert get_ipython().kernel.comm_manager is get_comm_manager()\n"
            "print('cm-ok')\n")
        reply, out = await kc.exec_drain(code, store_history=False)
        assert reply["content"]["status"] == "ok", reply["content"]
        assert "cm-ok" in "".join(m["content"]["text"] for m in iopub_streams(out))


_replier = """from comm import get_comm_manager
def _target(comm, open_msg):
    comm.send(data={'ack': open_msg['content']['data'].get('n')})
    comm.on_msg(lambda m: comm.send(data={'echo': m['content']['data']}))
get_comm_manager().register_target('replier', _target)
"""


async def test_comm_callback_reply_reaches_iopub():
    "A target/on_msg callback that replies via comm.send() reaches iopub, parented to the inbound msg."
    async with mini_kernel() as (_, kc):
        assert (await kc.exec_drain(_replier, store_history=False))[0]["content"]["status"] == "ok"
        cid = "rep-1"
        open_id = kc.shell_request("comm_open", reply=False, comm_id=cid, target_name="replier", data={"n": 7})
        ack = await wait_iopub(kc, lambda m: m["msg_type"] == "comm_msg" and m["content"].get("comm_id") == cid and "ack" in m["content"].get("data", {}),
            timeout=10, err="open-callback reply never reached iopub")
        assert ack["content"]["data"] == {"ack": 7}
        assert parent_id(ack) == open_id, "reply parent should be the inbound comm_open"
        msg_id = kc.shell_request("comm_msg", reply=False, comm_id=cid, data={"hi": 1})
        echo = await wait_iopub(kc, lambda m: m["msg_type"] == "comm_msg" and m["content"].get("comm_id") == cid and "echo" in m["content"].get("data", {}),
            timeout=10, err="on_msg-callback reply never reached iopub")
        assert echo["content"]["data"] == {"echo": {"hi": 1}}
        assert parent_id(echo) == msg_id, "reply parent should be the inbound comm_msg"


_silent = """from comm import get_comm_manager
seen = []
def _target(comm, open_msg): comm.on_msg(lambda m: seen.append(m['content']['data']))
get_comm_manager().register_target('silent', _target)
"""


async def test_inbound_comm_not_echoed():
    "Inbound comm messages drive callbacks; they are not reflected back on iopub (ipykernel parity)."
    async with mini_kernel() as (_, kc):
        assert (await kc.exec_drain(_silent, store_history=False))[0]["content"]["status"] == "ok"
        cid = "sil-1"
        kc.shell_request("comm_open", reply=False, comm_id=cid, target_name="silent", data={})
        kc.shell_request("comm_msg", reply=False, comm_id=cid, data={"x": 1})
        bid = str(uuid4())
        barrier = kc.execute("assert seen == [{'x': 1}], seen", reply=True, timeout=10, msg_id=bid)  # runs after the comms (FIFO)
        echoed = []
        for rem in iter_timeout(10):
            try: m = await kc.get_iopub_msg(timeout=rem)
            except Empty: continue
            if m["msg_type"] in ("comm_open", "comm_msg") and m["content"].get("comm_id") == cid: echoed.append(m["msg_type"])
            if parent_id(m) == bid and m["msg_type"] == "status" and m["content"].get("execution_state") == "idle": break
        assert not echoed, f"inbound comm should not be echoed on iopub: {echoed}"
        assert (await barrier)["content"]["status"] == "ok"


_printer = """from comm import get_comm_manager
def _target(comm, open_msg):
    print('open-print')
    comm.on_msg(lambda m: print('msg-print', m['content']['data'].get('x')))
get_comm_manager().register_target('printer', _target)
"""


async def test_comm_callback_output_reaches_iopub():
    "stdout inside a comm target/on_msg callback is captured and published on iopub, parented to the inbound msg."
    async with mini_kernel() as (_, kc):
        assert (await kc.exec_drain(_printer, store_history=False))[0]["content"]["status"] == "ok"
        cid = "pr-1"
        open_id = kc.shell_request("comm_open", reply=False, comm_id=cid, target_name="printer", data={})
        s1 = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "open-print" in m["content"].get("text", ""),
            err="comm_open callback stdout never reached iopub")
        assert parent_id(s1) == open_id, "stream parent should be the inbound comm_open"
        mid = kc.shell_request("comm_msg", reply=False, comm_id=cid, data={"x": 9})
        s2 = await wait_iopub(kc, lambda m: m["msg_type"] == "stream" and "msg-print" in m["content"].get("text", ""),
            err="comm_msg callback stdout never reached iopub")
        assert parent_id(s2) == mid, "stream parent should be the inbound comm_msg"


async def test_kernel_get_parent():
    "get_ipython().kernel.get_parent() returns the active parent header (ipywidgets Output relies on it)."
    async with mini_kernel() as (_, kc):
        reply, out = await kc.exec_drain("print(get_ipython().kernel.get_parent()['header']['msg_id'])", store_history=False)
        assert reply["content"]["status"] == "ok", reply["content"]
        printed = "".join(m["content"]["text"] for m in iopub_streams(out)).strip()
        assert printed == parent_id(reply), (printed, parent_id(reply))
