# ipymini-zmqthread

`ipymini-zmqthread` provides small, testable building blocks for running ZMQ sockets in dedicated threads — the pattern required by Jupyter kernels for correctness and thread-safety.

It was extracted from the `ipymini` research kernel to keep the kernel implementation small and focused.

## Why?

Jupyter kernels typically need:

- background threads for shell/control ROUTER sockets
- a dedicated IOPub sender thread (PUB)
- a heartbeat REP thread
- a stdin ROUTER thread for input_request/input_reply

The key invariant is: each ZMQ socket is owned by exactly one thread; other threads communicate with it via queues.

A second invariant: no ZMQ call in these threads may block without bound, because zmq's failure mode for misuse is blocking or silent dropping, and a blocked socket thread is indistinguishable from a hung kernel. Each socket satisfies it in a role-specific way: every socket is `linger=0` and closed with `close(0)`; every loop polls with a short timeout and re-checks its stop scope; PUB/XPUB sends drop at the high-water mark by design (the bounded queue in `IOPubThread` reports drops); shell/control ROUTER replies to a vanished client are deliberately dropped (there is no one to tell); the heartbeat REP is lockstep so its send cannot block; and the stdin ROUTER sets `ROUTER_MANDATORY` with a zero send timeout so an undeliverable `input_request` raises and is retried rather than silently lost. A new socket added here must state how it satisfies this invariant.

## Install

```bash
pip install ipymini-zmqthread
```

## API

```python
from ipymini_zmqthread import (AsyncRouterThread, IOPubThread,
  StdinRouterThread, HeartbeatThread)
```

### AsyncRouterThread

Runs an asyncio loop in a dedicated thread and owns an async ROUTER socket (`zmq.asyncio`).

```python
import zmq
from jupyter_client.session import Session
from ipymini_zmqthread import AsyncRouterThread

ctx = zmq.Context.instance()
session = Session(key=b"")
router = None
def on_msg(msg, idents): router.enqueue(("kernel_info_reply", {"status": "ok"}, msg, idents))
router = AsyncRouterThread(
    context=ctx, session=session,
    bind_addr="tcp://127.0.0.1:5555",
    handler=on_msg, log_label="shell")
router.start()
router.ready.wait()
...
router.stop()
router.join(timeout=1)
```

### IOPubThread

Runs an XPUB socket in a dedicated thread and sends messages via `Session.send` from inside that thread. Its bounded queue drops non-`status` messages past `qmax`, never `status` - but that guarantee covers this queue only. Below it, libzmq drops silently per slow subscriber once `sndhwm` fills (default 1000), statuses included; PUB-family sends never block, so this thread cannot see subscriber backpressure, and an unbounded `sndhwm` would let one wedged client grow kernel memory without bound. A subscriber wanting a lossless hop sets `RCVHWM=0` on its SUB and keeps draining.

```python
from ipymini_zmqthread import IOPubThread

iopub = IOPubThread(ctx, "tcp://127.0.0.1:5556", session, qmax=10000, sndhwm=None)
iopub.start()
iopub.send("status", {"execution_state": "idle"}, parent=None)
...
iopub.stop()
iopub.join(timeout=1)
```

### HeartbeatThread

Echo thread: REP socket receives bytes and sends them back.

```python
from ipymini_zmqthread import HeartbeatThread

hb = HeartbeatThread(ctx, "tcp://127.0.0.1:5557")
hb.start()
...
hb.stop()
hb.join(timeout=1)
```

### StdinRouterThread

Routes `input_request`/`input_reply` and provides a blocking `request_input(...)` API.

```python
from ipymini_zmqthread import StdinRouterThread

stdin = StdinRouterThread(ctx, "tcp://127.0.0.1:5558", session)
stdin.start()

value = stdin.request_input("Name: ", password=False, parent=parent_msg, ident=client_idents, timeout=10.0)
...
stdin.interrupt_pending()  # cancels in-flight waits
stdin.stop()
stdin.join(timeout=1)
```

## License

Apache 2.

