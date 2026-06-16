import statistics, threading, time

import zmq
from jupyter_client.session import Session

from ipymini.zmqthread import AsyncRouterThread
from .test_zmqthread import _free_port, _poll_recv


def _started_router(handler):
    ctx = zmq.Context.instance()
    router = AsyncRouterThread(context=ctx, session=Session(key=b""), bind_addr=f"tcp://127.0.0.1:{_free_port()}", handler=handler, log_label="shell")
    router.start()
    router.wait_started(2)
    dealer = ctx.socket(zmq.DEALER)
    dealer.linger = 0
    dealer.connect(router.bind_addr)
    time.sleep(0.05)
    return router, dealer


def test_reply_enqueued_from_other_thread_is_sent_promptly():
    "Replies must not wait out the router's poll timeout (kernel replies come from subshell threads)."
    got = {}
    seen = threading.Event()

    def handler(msg, idents):
        got.update(msg=msg, idents=idents)
        seen.set()

    router, dealer = _started_router(handler)
    client = Session(key=b"")
    try:
        client.send(dealer, "kernel_info_request", {}, parent=None)
        assert seen.wait(2)
        lats = []
        for _ in range(10):
            time.sleep(0.01)  # let the router settle into its idle wait
            t0 = time.monotonic()
            router.enqueue(("kernel_info_reply", dict(status="ok"), got["msg"], got["idents"]))
            _poll_recv(dealer, client, timeout=2000)
            lats.append(time.monotonic() - t0)
        median = statistics.median(lats)
        assert median < router.poll_ms / 1000 / 4, f"reply latency {median*1000:.1f}ms; outbox does not wake the poll loop"
    finally:
        router.stop()
        router.join(timeout=2)
        dealer.close(0)


def test_burst_replies_all_arrive_in_order():
    "A burst larger than max_send_batch must all be delivered, in order, with no drops."
    got = {}
    seen = threading.Event()

    def handler(msg, idents):
        got.update(msg=msg, idents=idents)
        seen.set()

    router, dealer = _started_router(handler)
    client = Session(key=b"")
    try:
        client.send(dealer, "kernel_info_request", {}, parent=None)
        assert seen.wait(2)
        n = 2 * router.max_send_batch + 50
        for i in range(n): router.enqueue(("kernel_info_reply", dict(status="ok", i=i), got["msg"], got["idents"]))
        received = [_poll_recv(dealer, client, timeout=2000)[1]["content"]["i"] for _ in range(n)]
        assert received == list(range(n)), f"lost or reordered: got {len(received)} of {n}"
    finally:
        router.stop()
        router.join(timeout=2)
        dealer.close(0)
