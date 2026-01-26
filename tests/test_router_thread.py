import pytest
import zmq
from jupyter_client.session import Session
from ipymini.kernel import AsyncRouterThread


def test_async_router_thread_join_no_typeerror():
    class DummyRouter(AsyncRouterThread):
        def run(self): return

    router = DummyRouter(context=zmq.Context.instance(), session=Session(key=b""), bind_addr="tcp://127.0.0.1:0",
        handler=lambda *_: None, log_label="shell")
    router.start()
    try: router.join(timeout=1)
    except TypeError as exc: pytest.fail(f"join raised TypeError: {exc}")
