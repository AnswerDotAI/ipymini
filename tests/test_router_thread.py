import pytest
from ipymini.kernel import AsyncRouterThread


def test_async_router_thread_join_no_typeerror():
    class DummyKernel: pass

    class DummyRouter(AsyncRouterThread):
        def run(self): return

    router = DummyRouter(DummyKernel(), 0, "shell_socket", lambda *_: None, "shell")
    router.start()
    try: router.join(timeout=1)
    except TypeError as exc: pytest.fail(f"join raised TypeError: {exc}")
