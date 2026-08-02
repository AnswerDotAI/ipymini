from contextlib import contextmanager
import zmq
from jupyter_client.session import Session, DELIM

from ipymini.zmqthread import IOPubThread


def _recv_msg(sock, session):
    "Receive one iopub message, tolerating an optional leading topic frame."
    parts = sock.recv_multipart()
    if DELIM in parts: parts = parts[parts.index(DELIM):]
    _, msg_list = session.feed_identities(parts)
    return session.deserialize(msg_list)


@contextmanager
def _iopub(xpub=True):
    "A started IOPubThread plus a connect() helper; every socket is closed on exit even when an assertion fails."
    ctx = zmq.Context()
    session = Session(key=b"abc")
    iopub = IOPubThread(ctx, "tcp://127.0.0.1:0", session, qmax=100, xpub=xpub)
    iopub.start()
    iopub.wait_started(5)
    subs = []
    def connect():
        s = ctx.socket(zmq.SUB)
        s.linger = 0
        s.rcvtimeo = 5000
        s.connect(iopub.bound_addr)
        s.subscribe(b"")
        subs.append(s)
        return s
    try: yield session, iopub, connect
    finally:
        for s in subs: s.close()
        iopub.stop()
        ctx.term()


def test_new_subscriber_gets_welcome():
    "A fresh subscriber's first message is iopub_welcome, so it knows its subscription is live."
    with _iopub() as (session, iopub, connect):
        msg = _recv_msg(connect(), session)
        assert msg["header"]["msg_type"] == "iopub_welcome"
        assert msg["content"] == {"subscription": ""}


def test_every_subscriber_gets_welcome():
    "Second subscriber to the same topic must also get a welcome (needs XPUB_VERBOSE; plain XPUB dedups)."
    with _iopub() as (session, iopub, connect):
        assert _recv_msg(connect(), session)["header"]["msg_type"] == "iopub_welcome"
        assert _recv_msg(connect(), session)["header"]["msg_type"] == "iopub_welcome"


def test_welcome_then_normal_traffic():
    "Messages queued after the welcome arrive in order; nothing published post-welcome is lost."
    with _iopub() as (session, iopub, connect):
        sub = connect()
        assert _recv_msg(sub, session)["header"]["msg_type"] == "iopub_welcome"
        iopub.send("status", dict(execution_state="busy"), parent=None)
        iopub.send("stream", dict(name="stdout", text="hi"), parent=None)
        got = [_recv_msg(sub, session)["header"]["msg_type"] for _ in range(2)]
        assert got == ["status", "stream"]


def test_plain_pub_mode_sends_no_welcome():
    "With xpub=False (IPYMINI_IOPUB_XPUB=0) the socket is plain PUB, emulating a pre-JEP-65 kernel: no welcome, just traffic."
    with _iopub(xpub=False) as (session, iopub, connect):
        sub = connect()
        for _ in range(100):
            iopub.send("status", dict(execution_state="busy"), parent=None)
            if sub.poll(50): break
        assert _recv_msg(sub, session)["header"]["msg_type"] == "status"


def test_welcome_first_even_with_inflight_traffic():
    "A subscriber joining while traffic is being enqueued still sees its welcome first (warm-connect ordering, JEP 65)."
    import time
    with _iopub() as (session, iopub, connect):
        for _ in range(20):
            sub = connect()
            time.sleep(0.005)  # let the subscription register while the send loop is parked on its queue
            iopub.send("status", dict(execution_state="busy"), parent=None)
            assert _recv_msg(sub, session)["header"]["msg_type"] == "iopub_welcome"
            while sub.poll(100): _recv_msg(sub, session)  # drain the status (and any broadcast welcomes) before the next round
            sub.close()
