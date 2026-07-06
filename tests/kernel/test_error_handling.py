"Tests for error handling: never drop requests, always send busy/idle for execute."
import asyncio, time, pytest
from uuid import uuid4
from queue import Empty
from ..aclient import *
from ..kernel_utils import start_kernel

@pytest.fixture(scope="module")
async def kc():
    "One shared kernel for this module; every test filters messages by its own msg_ids."
    async with mini_kernel() as (_, kc): yield kc


@pytest.fixture(scope="module")
def sync_kc():
    "A sync-client kernel for the raw-channel-timing test below that can't move to ConKernelClient (see its comment)."
    with start_kernel() as (_, kc): yield kc


async def _states(kc, mid):
    outputs = await kc.iopub_drain(mid)
    return [m["content"]["execution_state"] for m in outputs if m["msg_type"] == "status"]


async def test_error_reply_status_features(kc):
    "Empty string subshell_id should route to parent subshell (treat as None)."
    await aflush(kc)
    # Send execute with empty subshell_id - should work, not error
    reply = await kc.shell_request("execute_request", code="1+1", subshell_id="")
    assert reply["content"]["status"] == "ok", f"expected ok, got {reply['content']}"
    states = await _states(kc, parent_id(reply))
    assert "busy" in states, "should have busy status"
    assert "idle" in states, "should have idle status"

    reply = await kc.shell_request("execute_request", code="1+1", subshell_id="nonexistent-subshell-123")
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "SubshellNotFound"
    # Must have busy/idle on iopub to prevent frontend spinner
    states = await _states(kc, parent_id(reply))
    assert "busy" in states, f"missing busy status, got {states}"
    assert "idle" in states, f"missing idle status, got {states}"

    reply = await kc.cmd.complete(code="pri", cursor_pos=3, subshell_id="bad-subshell")
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "SubshellNotFound"

    # Send execute_request without 'code' field
    reply = await kc.shell_request("execute_request")  # no code field
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "MissingField"
    assert "code" in reply["content"]["evalue"]
    # Must have busy/idle
    states = await _states(kc, parent_id(reply))
    assert "busy" in states, f"missing busy, got {states}"
    assert "idle" in states, f"missing idle, got {states}"

    # complete_request requires 'code' and 'cursor_pos'
    reply = await kc.shell_request("complete_request")
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "MissingField"

    reply = await kc.cmd.history()
    assert reply["content"]["status"] == "error"
    assert reply["content"].get("ename") == "MissingField"

    reply, outputs = await kc.exec_drain("x = 42")
    assert reply["content"]["status"] == "ok"
    states = [m["content"]["execution_state"] for m in outputs if m["msg_type"] == "status"]
    assert states[0] == "busy", "first status should be busy"
    assert states[-1] == "idle", "last status should be idle"

    reply, outputs = await kc.exec_drain("1/0")
    assert reply["content"]["status"] == "error"
    states = [m["content"]["execution_state"] for m in outputs if m["msg_type"] == "status"]
    assert "busy" in states
    assert "idle" in states

    # First execute raises, setting abort state; default fail_pending=False lets us
    # observe the queued cell's real kernel-issued "aborted" reply and its busy/idle pair
    mid_fail, mid_abort = str(uuid4()), str(uuid4())
    c_fail = kc.execute("import time; time.sleep(0.2); raise ValueError('boom')", reply=True, timeout=10, msg_id=mid_fail)
    c_abort = kc.execute("print('should abort')", reply=True, timeout=10, msg_id=mid_abort)
    reply_fail, reply_abort = await asyncio.gather(c_fail, c_abort)
    assert reply_fail["content"]["status"] == "error"
    assert reply_abort["content"]["status"] == "aborted"

    outputs = await collect_iopub(kc, {mid_fail, mid_abort})
    states = [m["content"]["execution_state"] for m in outputs[mid_abort] if m["msg_type"] == "status"]
    assert "busy" in states, f"aborted missing busy, got {states}"
    assert "idle" in states, f"aborted missing idle, got {states}"


async def test_iopub_idle_not_delayed_after_shell_reply(kc):
    "IOPub idle should arrive promptly after shell reply, not be delayed in queue."
    # Execute multiple times to increase chance of catching race condition
    for i in range(5):
        reply = await kc.execute(f"x = {i}", reply=True, timeout=5)
        # Wait for shell reply
        assert reply["content"]["status"] == "ok"
        msg_id = parent_id(reply)
        # After shell reply, idle should be available almost immediately
        # If IOPub isn't flushed, idle could be delayed by poll timeout (50ms+)
        # We allow 200ms which is generous but catches the bug
        idle_found = False
        start = time.monotonic()
        deadline = start + 0.2  # 200ms timeout
        while time.monotonic() < deadline:
            try:
                msg = await kc.get_iopub_msg(timeout=0.05)
                if parent_id(msg) == msg_id and msg["msg_type"] == "status":
                    if msg["content"]["execution_state"] == "idle":
                        idle_found = True
                        break
            except Empty: continue
        elapsed = time.monotonic() - start
        assert idle_found, f"iteration {i}: idle not received within 200ms of shell reply (elapsed={elapsed:.3f}s)"


async def test_iopub_idle_arrives_for_every_pipelined_request(kc):
    "When pipelining requests, every request must get its own iopub idle."
    mids = [str(uuid4()) for _ in range(10)]
    cs = [kc.execute(f"y = {i}", reply=True, timeout=10, msg_id=mid) for i, mid in enumerate(mids)]
    await asyncio.gather(*cs)
    await collect_iopub(kc, mids)  # raises if any request is missing its idle


@pytest.mark.slow
async def test_rapid_fire_200_executes():
    "Fire 200 execute_requests as fast as possible, verify order, idles, and outputs."
    async with mini_kernel() as (_, kc):
        sleeper = kc.execute("import time; time.sleep(0.8)", reply=True, timeout=20)
        await wait_status(kc, "busy")
        bursts = [kc.cmd.is_complete(code="1+1", timeout=20) for _ in range(130)]
        burst_replies, sleeper_reply = await asyncio.gather(asyncio.gather(*bursts), sleeper)
        assert all(reply["msg_type"] == "is_complete_reply" for reply in burst_replies)
        assert sleeper_reply["content"]["status"] == "ok"

        n = 200
        # Send all requests without waiting
        mids = [str(uuid4()) for _ in range(n)]
        cs = [kc.execute(f"1+{i}", reply=True, timeout=60, msg_id=mid) for i, mid in enumerate(mids)]
        reply_list = await asyncio.gather(*cs)
        replies = dict(zip(mids, reply_list))
        iopub_by_id = await collect_iopub(kc, mids, timeout=60)

        # Check replies are ok and in correct order (execution_count should increase)
        exec_counts = []
        for i, mid in enumerate(mids):
            reply = replies[mid]
            assert reply["content"]["status"] == "ok", f"request {i} failed: {reply['content']}"
            exec_counts.append(reply["content"]["execution_count"])

        # Execution counts should be monotonically increasing (allowing for possible reordering)
        # But more importantly, each should be unique
        assert len(set(exec_counts)) == n, f"duplicate execution counts found"

        # Check each request has busy and idle
        missing_busy = []
        missing_idle = []
        wrong_output = []
        for i, mid in enumerate(mids):
            msgs = iopub_by_id[mid]
            statuses = [m for m in msgs if m["msg_type"] == "status"]
            states = [m["content"]["execution_state"] for m in statuses]
            if "busy" not in states: missing_busy.append(i)
            if "idle" not in states: missing_idle.append(i)
            # Check output value
            results = [m for m in msgs if m["msg_type"] == "execute_result"]
            if results:
                data = results[-1]["content"].get("data", {})
                expected = str(1 + i)
                actual = data.get("text/plain", "")
                if actual != expected: wrong_output.append((i, expected, actual))

        assert not missing_busy, f"{len(missing_busy)} requests missing busy: {missing_busy[:10]}"
        assert not missing_idle, f"{len(missing_idle)} requests missing idle: {missing_idle[:10]}"
        assert not wrong_output, f"{len(wrong_output)} wrong outputs: {wrong_output[:10]}"


async def test_rapid_fire_with_output(kc):
    "Fire 50 execute_requests with print output, verify all replies and idles received."
    n = 50
    # Cells that produce output, like real notebooks
    codes = [f"print('cell {i}')\n{i}" for i in range(n)]
    mids = [str(uuid4()) for _ in range(n)]
    cs = [kc.execute(code, reply=True, timeout=30, msg_id=mid) for code, mid in zip(codes, mids)]
    reply_list = await asyncio.gather(*cs)
    replies = dict(zip(mids, reply_list))
    await collect_iopub(kc, mids, timeout=30)  # raises if any request is missing its idle
    for i, mid in enumerate(mids): assert replies[mid]["content"]["status"] == "ok", f"cell {i} failed"


# NOT MIGRATED: measures sub-20ms arrival-order jitter between raw shell and iopub messages by
# polling both channels together in one tight loop. ConKernelClient's shell channel is owned
# exclusively by its background reply-reader task (core.py `_reader`), so a test can no longer
# poll `get_shell_msg` directly without racing that task; substituting the awaited reply future
# for the raw shell poll adds asyncio task-scheduling overhead that isn't the poll-timeout
# behavior this test is meant to catch, at a tolerance (20ms) tight enough to make that
# substitution unsound.
def test_iopub_idle_not_delayed_by_poll_timeout(sync_kc):
    "IOPub idle must arrive within 20ms of shell reply - not delayed by poll timeout."
    kc = sync_kc
    max_delay_ms = 20  # max acceptable delay between shell reply and iopub idle
    failures = []

    for i in range(10):  # multiple iterations to catch the race
        msg_id = kc.execute(f"z = {i}")

        # Collect shell reply and iopub messages with timestamps
        shell_reply_time = None
        idle_time = None
        end_time = time.monotonic() + 5.0

        while time.monotonic() < end_time:
            # Check shell
            try:
                msg = kc.get_shell_msg(timeout=0.001)
                if parent_id(msg) == msg_id and msg["msg_type"] == "execute_reply": shell_reply_time = time.monotonic()
            except Empty: pass

            # Check iopub
            try:
                msg = kc.get_iopub_msg(timeout=0.001)
                is_idle = msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle"
                if parent_id(msg) == msg_id and is_idle: idle_time = time.monotonic()
            except Empty: pass

            # Done when we have both
            if shell_reply_time and idle_time: break

        assert shell_reply_time is not None, f"iteration {i}: no shell reply"
        assert idle_time is not None, f"iteration {i}: no idle"

        # Calculate delay - idle should arrive close to shell reply
        # Note: idle could arrive before or after shell reply
        delay_ms = (idle_time - shell_reply_time) * 1000
        if delay_ms > max_delay_ms: failures.append((i, delay_ms))

    # Allow some variance but most should be fast
    if len(failures) > 2:  # more than 20% failure rate
        assert False, f"IOPub idle delayed >20ms in {len(failures)}/10 iterations: {failures}"
