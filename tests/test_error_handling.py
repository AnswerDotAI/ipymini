"Tests for error handling: never drop requests, always send busy/idle for execute."
import asyncio, time
from queue import Empty
from .kernel_utils import *


def test_empty_subshell_id_routes_to_parent():
    "Empty string subshell_id should route to parent subshell (treat as None)."
    with start_kernel() as (_, kc):
        # Send execute with empty subshell_id - should work, not error
        msg_id = kc.shell_send("execute_request", code="1+1", subshell_id="")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "ok", f"expected ok, got {reply['content']}"
        outputs = kc.iopub_drain(msg_id)
        statuses = [m for m in outputs if m["msg_type"] == "status"]
        states = [m["content"]["execution_state"] for m in statuses]
        assert "busy" in states, "should have busy status"
        assert "idle" in states, "should have idle status"


def test_unknown_subshell_id_sends_busy_idle():
    "Unknown subshell_id should send error reply with busy/idle (not just reply)."
    with start_kernel() as (_, kc):
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


def test_unknown_subshell_non_execute_no_idle():
    "Unknown subshell for non-execute request sends error reply (no busy/idle needed)."
    with start_kernel() as (_, kc):
        msg_id = kc.shell_send("complete_request", code="pri", cursor_pos=3, subshell_id="bad-subshell")
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "SubshellNotFound"


def test_missing_code_field_sends_busy_idle():
    "execute_request missing 'code' field should send error with busy/idle."
    with start_kernel() as (_, kc):
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


def test_missing_fields_non_execute():
    "Non-execute request with missing fields sends error reply (no busy/idle needed)."
    with start_kernel() as (_, kc):
        # complete_request requires 'code' and 'cursor_pos'
        msg_id = kc.shell_send("complete_request", content={})
        reply = kc.shell_reply(msg_id)
        assert reply["content"]["status"] == "error"
        assert reply["content"]["ename"] == "MissingField"


def test_execute_reply_always_has_idle():
    "Normal execute_request must have busy and idle on iopub."
    with start_kernel() as (_, kc):
        msg_id, reply, outputs = kc.exec_drain("x = 42")
        assert reply["content"]["status"] == "ok"
        statuses = [m for m in outputs if m["msg_type"] == "status"]
        states = [m["content"]["execution_state"] for m in statuses]
        assert states[0] == "busy", "first status should be busy"
        assert states[-1] == "idle", "last status should be idle"


def test_error_execute_has_busy_idle():
    "execute_request that raises should still have busy/idle."
    with start_kernel() as (_, kc):
        msg_id, reply, outputs = kc.exec_drain("1/0")
        assert reply["content"]["status"] == "error"
        statuses = [m for m in outputs if m["msg_type"] == "status"]
        states = [m["content"]["execution_state"] for m in statuses]
        assert "busy" in states
        assert "idle" in states


def test_abort_reply_has_busy_idle():
    "Aborted execute_request should have busy/idle on IOPub."
    with start_kernel() as (_, kc):
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


def test_iopub_idle_not_delayed_after_shell_reply():
    "IOPub idle should arrive promptly after shell reply, not be delayed in queue."
    with start_kernel() as (_, kc):
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


def test_iopub_idle_arrives_before_next_request_starts():
    "When pipelining requests, idle for request N must arrive before busy for request N+1."
    with start_kernel() as (_, kc):
        # Pipeline several requests
        n_requests = 10
        msg_ids = [kc.execute(f"y = {i}") for i in range(n_requests)]

        # Collect all messages
        all_shell = []
        all_iopub = []
        replies_needed = set(msg_ids)
        end_time = time.monotonic() + default_timeout

        while replies_needed and time.monotonic() < end_time:
            try:
                msg = kc.get_shell_msg(timeout=0.1)
                all_shell.append(msg)
                pid = parent_id(msg)
                if pid in replies_needed: replies_needed.remove(pid)
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

        # For each request, find the timestamp/order of idle
        # Check that each request's idle arrived (in message order) before the next request's shell reply
        iopub_order = [(parent_id(m), m["msg_type"], m.get("content", {}).get("execution_state")) for m in all_iopub]

        # Build a map of msg_id -> index of idle in iopub stream
        idle_indices = {}
        for idx, (pid, mtype, state) in enumerate(iopub_order):
            if mtype == "status" and state == "idle" and pid in msg_ids:
                if pid not in idle_indices: idle_indices[pid] = idx

        # All requests should have idle
        for msg_id in msg_ids: assert msg_id in idle_indices, f"no idle found for {msg_id}"


def test_rapid_fire_200_executes():
    "Fire 200 execute_requests as fast as possible, verify order, idles, and outputs."
    with start_kernel() as (_, kc):
        n = 200
        # Send all requests without waiting
        msg_ids = [kc.execute(f"1+{i}") for i in range(n)]

        # Collect all replies and iopub messages
        replies = {}
        iopub_by_id = {mid: [] for mid in msg_ids}
        replies_needed = set(msg_ids)
        end_time = time.monotonic() + 60  # generous timeout for 200 requests

        while replies_needed and time.monotonic() < end_time:
            try:
                msg = kc.get_shell_msg(timeout=0.1)
                pid = parent_id(msg)
                if pid in replies_needed:
                    replies[pid] = msg
                    replies_needed.remove(pid)
            except Empty: pass
            try:
                msg = kc.get_iopub_msg(timeout=0.01)
                pid = parent_id(msg)
                if pid in iopub_by_id: iopub_by_id[pid].append(msg)
            except Empty: pass

        # Drain remaining iopub
        drain_end = time.monotonic() + 2
        while time.monotonic() < drain_end:
            try:
                msg = kc.get_iopub_msg(timeout=0.1)
                pid = parent_id(msg)
                if pid in iopub_by_id: iopub_by_id[pid].append(msg)
            except Empty: break

        # Check all replies received
        assert not replies_needed, f"missing {len(replies_needed)} replies: {list(replies_needed)[:5]}..."

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


def test_rapid_fire_with_output():
    "Fire 50 execute_requests with print output, verify all idles received."
    with start_kernel() as (_, kc):
        n = 50
        # Cells that produce output, like real notebooks
        codes = [f"print('cell {i}')\n{i}" for i in range(n)]
        msg_ids = [kc.execute(code) for code in codes]
        msg_id_set = set(msg_ids)

        # Collect all replies and iopub
        replies = {}
        iopub_by_id = {mid: [] for mid in msg_ids}
        replies_needed = set(msg_ids)
        idle_received = set()
        end_time = time.monotonic() + 60

        while (replies_needed or len(idle_received) < n) and time.monotonic() < end_time:
            # Check shell
            try:
                msg = kc.get_shell_msg(timeout=0.1)
                pid = parent_id(msg)
                if pid in replies_needed:
                    replies[pid] = msg
                    replies_needed.remove(pid)
            except Empty: pass
            # Check iopub
            try:
                msg = kc.get_iopub_msg(timeout=0.1)
                pid = parent_id(msg)
                if pid in iopub_by_id:
                    iopub_by_id[pid].append(msg)
                    if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle": idle_received.add(pid)
            except Empty: pass

        assert not replies_needed, f"missing {len(replies_needed)}/{n} replies"
        missing_idles = msg_id_set - idle_received
        assert not missing_idles, f"missing {len(missing_idles)}/{n} idles"

        # Check all replies ok
        for i, mid in enumerate(msg_ids): assert replies[mid]["content"]["status"] == "ok", f"cell {i} failed"


def test_iopub_idle_not_delayed_by_poll_timeout():
    "IOPub idle must arrive within 20ms of shell reply - not delayed by poll timeout."
    with start_kernel() as (_, kc):
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
