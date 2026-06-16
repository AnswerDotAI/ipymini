import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import IOPubThread


def _drive(iopub, max_steps: int = 60) -> list[str]:
    "Pop queued messages like run_service does, while user code keeps printing."
    popped = []
    for step in range(max_steps):
        item = iopub._get_next()
        if item is None: break
        popped.append(item[0])
        if popped[-1] == "status": break
        iopub.send("stream", dict(name="stdout", text=f"more{step}"), parent=None)
    return popped


def test_status_not_starved_by_sustained_output():
    "A status queued while the queue is full must still go out under sustained stream traffic."
    ctx = zmq.Context.instance()
    iopub = IOPubThread(ctx, "tcp://127.0.0.1:1", Session(key=b""), qmax=4)  # not started: drive directly
    for i in range(4): iopub.send("stream", dict(name="stdout", text=f"chunk{i}"), parent=None)
    iopub.send("status", dict(execution_state="idle"), parent=None)
    popped = _drive(iopub)
    assert "status" in popped, f"status starved behind stream traffic: {popped}"


def test_full_queue_drops_streams_but_keeps_order_and_status():
    ctx = zmq.Context.instance()
    iopub = IOPubThread(ctx, "tcp://127.0.0.1:1", Session(key=b""), qmax=2)
    for i in range(5): iopub.send("stream", dict(name="stdout", text=f"chunk{i}"), parent=None)
    iopub.send("status", dict(execution_state="idle"), parent=None)
    iopub.send("status", dict(execution_state="busy"), parent=None)
    got = []
    while (item := iopub._get_next()) is not None: got.append((item[0], item[1].get("text") or item[1].get("execution_state")))
    assert got == [("stream", "chunk0"), ("stream", "chunk1"), ("status", "idle"), ("status", "busy")]
    assert iopub.enqueued == 7
    assert iopub.dropped == 3
