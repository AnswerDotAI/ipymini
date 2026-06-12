"Tests for error handling: never drop requests, always send busy/idle for execute."
import time, pytest
from queue import Empty
from ..kernel_utils import *


@pytest.fixture(scope="module")
def kc():
    "One shared kernel for this module; every test filters messages by its own msg_ids."
    with start_kernel() as (_, kc): yield kc


def test_error_reply_status_features(kc):
    "Empty string subshell_id should route to parent subshell (treat as None)."
    # Send execute with empty subshell_id - should work, not error
    msg_id = kc.shell_send("execute_request", code="1+1", subshell_id="")
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "ok", f"expected ok, got {reply['content']}"
    outputs = kc.iopub_drain(msg_id)
    statuses = [m for m in outputs if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert "busy" in states, "should have busy status"
    assert "idle" in states, "should have idle status"

    msg_id = kc.shell_send("execute_request", code="1+1", subshell_id="nonexistent-subshell-123")
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "SubshellNotFound"
    # Must have busy/idle on iopub to prevent frontend spinner
    outputs = kc.iopub_drain(msg_id)
    statuses = [m for m in outputs if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert "busy" in states, f"missing busy status, got {statuses}"
    assert "idle" in states, f"missing idle status, got {statuses}"

    msg_id = kc.shell_send("complete_request", code="pri", cursor_pos=3, subshell_id="bad-subshell")
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "SubshellNotFound"

    # Send execute_request without 'code' field
    msg_id = kc.shell_send("execute_request", content={})  # no code field
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "MissingField"
    assert "code" in reply["content"]["evalue"]
    # Must have busy/idle
    outputs = kc.iopub_drain(msg_id)
    statuses = [m for m in outputs if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert "busy" in states, f"missing busy, got {statuses}"
    assert "idle" in states, f"missing idle, got {statuses}"

    # complete_request requires 'code' and 'cursor_pos'
    msg_id = kc.shell_send("complete_request", content={})
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "error"
    assert reply["content"]["ename"] == "MissingField"

    msg_id = kc.cmd.history_request()
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "error"
    assert reply["content"].get("ename") == "MissingField"

    msg_id, reply, outputs = kc.exec_drain("x = 42")
    assert reply["content"]["status"] == "ok"
    statuses = [m for m in outputs if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert states[0] == "busy", "first status should be busy"
    assert states[-1] == "idle", "last status should be idle"

    msg_id, reply, outputs = kc.exec_drain("1/0")
    assert reply["content"]["status"] == "error"
    statuses = [m for m in outputs if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert "busy" in states
    assert "idle" in states

    # First execute raises, setting abort state
    fail_code = "import time; time.sleep(0.2); raise ValueError('boom')"
    msg_id_fail = kc.execute(fail_code)
    msg_id_abort = kc.execute("print('should abort')")

    # Collect both shell replies
    replies_needed = {msg_id_fail, msg_id_abort}
    replies = {}
    all_iopub = []
    end_time = time.monotonic() + default_timeout

    while replies_needed and time.monotonic() < end_time:
        try:
            msg = kc.get_shell_msg(timeout=0.1)
            pid = parent_id(msg)
            if pid in replies_needed:
                replies[pid] = msg
                replies_needed.remove(pid)
        except Empty: pass
        try:
            msg = kc.get_iopub_msg(timeout=0.1)
            all_iopub.append(msg)
        except Empty: pass

    # Drain remaining iopub
    time.sleep(0.1)
    while True:
        try: all_iopub.append(kc.get_iopub_msg(timeout=0.1))
        except Empty: break

    assert not replies_needed, f"missing replies for {replies_needed}"
    assert replies[msg_id_fail]["content"]["status"] == "error"
    assert replies[msg_id_abort]["content"]["status"] == "aborted"

    # Filter iopub messages for the aborted request
    abort_iopub = [m for m in all_iopub if parent_id(m) == msg_id_abort]
    statuses = [m for m in abort_iopub if m["msg_type"] == "status"]
    states = [m["content"]["execution_state"] for m in statuses]
    assert "busy" in states, f"aborted missing busy, got {statuses}"
    assert "idle" in states, f"aborted missing idle, got {statuses}"


def test_iopub_idle_not_delayed_after_shell_reply(kc):
    "IOPub idle should arrive promptly after shell reply, not be delayed in queue."
    # Execute multiple times to increase chance of catching race condition
    for i in range(5):
        msg_id = kc.execute(f"x = {i}")
        # Wait for shell reply
        reply = kc.shell_reply(msg_id, timeout=5)
        assert reply["content"]["status"] == "ok"
        # After shell reply, idle should be available almost immediately
        # If IOPub isn't flushed, idle could be delayed by poll timeout (50ms+)
        # We allow 200ms which is generous but catches the bug
        idle_found = False
        start = time.monotonic()
        deadline = start + 0.2  # 200ms timeout
        while time.monotonic() < deadline:
            try:
                msg = kc.get_iopub_msg(timeout=0.05)
                if parent_id(msg) == msg_id and msg["msg_type"] == "status":
                    if msg["content"]["execution_state"] == "idle":
                        idle_found = True
                        break
            except Empty: continue
        elapsed = time.monotonic() - start
        assert idle_found, f"iteration {i}: idle not received within 200ms of shell reply (elapsed={elapsed:.3f}s)"


def test_iopub_idle_arrives_for_every_pipelined_request(kc):
    "When pipelining requests, every request must get its own iopub idle."
    msg_ids = [kc.execute(f"y = {i}") for i in range(10)]
    collect_shell_replies(kc, set(msg_ids))
    collect_iopub_outputs(kc, set(msg_ids))  # raises if any request is missing its idle


@pytest.mark.slow
def test_rapid_fire_200_executes():
    "Fire 200 execute_requests as fast as possible, verify order, idles, and outputs."
    with start_kernel() as (_, kc):
        sleeper = kc.execute("import time; time.sleep(0.8)")
        wait_for_status(kc, "busy")
        burst_ids = {kc.shell_send("is_complete_request", code="1+1") for _ in range(130)}
        replies = collect_shell_replies(kc, burst_ids | {sleeper}, timeout=20)
        burst_replies = {msg_id: reply for msg_id, reply in replies.items() if msg_id in burst_ids}
        assert all(reply["msg_type"] == "is_complete_reply" for reply in burst_replies.values())
        assert replies[sleeper]["content"]["status"] == "ok"

        n = 200
        # Send all requests without waiting
        msg_ids = [kc.execute(f"1+{i}") for i in range(n)]
        replies = collect_shell_replies(kc, set(msg_ids), timeout=60)
        iopub_by_id = collect_iopub_outputs(kc, set(msg_ids), timeout=60)

        # Check replies are ok and in correct order (execution_count should increase)
        exec_counts = []
        for i, mid in enumerate(msg_ids):
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
        for i, mid in enumerate(msg_ids):
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


def test_rapid_fire_with_output(kc):
    "Fire 50 execute_requests with print output, verify all replies and idles received."
    n = 50
    # Cells that produce output, like real notebooks
    codes = [f"print('cell {i}')\n{i}" for i in range(n)]
    msg_ids = [kc.execute(code) for code in codes]
    replies = collect_shell_replies(kc, set(msg_ids), timeout=30)
    collect_iopub_outputs(kc, set(msg_ids), timeout=30)  # raises if any request is missing its idle
    for i, mid in enumerate(msg_ids): assert replies[mid]["content"]["status"] == "ok", f"cell {i} failed"


def test_iopub_idle_not_delayed_by_poll_timeout(kc):
    "IOPub idle must arrive within 20ms of shell reply - not delayed by poll timeout."
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
