from queue import Empty
from uuid import uuid4
from ..aclient import *
from ..kernel_utils import iopub_streams

timeout = 3


async def test_input_features():
    async with mini_kernel() as (_, kc):
        mid = str(uuid4())
        c = kc.execute("print('before'); print(input('prompt> '))", allow_stdin=True, reply=True, timeout=timeout, msg_id=mid)
        stdin_msg = await kc.get_stdin_msg(timeout=timeout)
        assert stdin_msg["msg_type"] == "input_request"
        assert stdin_msg["content"]["prompt"] == "prompt> "
        assert not stdin_msg["content"]["password"]

        stream_msg = await wait_iopub(kc, lambda m: parent_id(m) == mid and m.get("msg_type") == "stream",
            timeout=timeout, err="expected stream before input reply")
        assert stream_msg["content"]["text"] == "before\n"

        text = "some text"
        await input_reply(kc, text)

        reply = await c
        assert reply["content"]["status"] == "ok"

        output_msgs = (await collect_iopub(kc, {mid}))[mid]
        streams = [(m["content"]["name"], m["content"]["text"]) for m in iopub_streams(output_msgs)]
        assert ("stdout", text + "\n") in streams

        mid = str(uuid4())
        c = kc.execute("input('prompt> ')", allow_stdin=False, reply=True, timeout=timeout, msg_id=mid)

        try:
            await kc.get_stdin_msg(timeout=1)
            assert False, "expected no stdin message"
        except Empty: pass

        reply = await c
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "StdinNotImplementedError"
        await collect_iopub(kc, {mid})

        mid = str(uuid4())
        c = kc.execute("user_input = input('Enter something: ')", allow_stdin=True, store_history=False, reply=True, timeout=timeout, msg_id=mid)
        stdin_msg = await kc.get_stdin_msg(timeout=timeout)
        await input_reply(kc, "bbb")
        await input_reply(kc, "bbb")
        reply_msg = await c
        assert reply_msg["content"]["status"] == "ok"
        await collect_iopub(kc, {mid})

        mid2 = str(uuid4())
        c2 = kc.execute("user_input = input('Again: ')", allow_stdin=True, store_history=False, reply=True, timeout=timeout, msg_id=mid2)
        _stdin_msg2 = await kc.get_stdin_msg(timeout=timeout)
        await input_reply(kc, "ccc")
        reply_msg2 = await c2
        assert reply_msg2["content"]["status"] == "ok"
        await collect_iopub(kc, {mid2})

        mid = str(uuid4())
        c = kc.execute("input('prompt> ')", allow_stdin=True, reply=True, timeout=timeout, msg_id=mid)
        stdin_msg = await kc.get_stdin_msg(timeout=timeout)
        assert stdin_msg["msg_type"] == "input_request"

        await kc.interrupt(timeout=timeout)

        reply = await c
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "KeyboardInterrupt"
        await collect_iopub(kc, {mid})

        ok_reply, _ = await kc.exec_drain("1+1", store_history=False)
        assert ok_reply["content"]["status"] == "ok"
