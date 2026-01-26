import asyncio, pytest, zmq
from ipymini.kernel import Subshell


class _DummyKernel:
    def __init__(self):
        self.context = zmq.Context.instance()
        self.stop_on_error_timeout = 0.0

    def queue_shell_reply(self, *args, **kwargs): pass
    def send_status(self, *args, **kwargs): pass


def test_loop_cleanup_clears_event_loop():
    kernel = _DummyKernel()
    subshell = Subshell(kernel, None, {}, use_singleton=True, run_in_thread=False)
    subshell._setup_loop()
    assert asyncio.get_event_loop() is subshell.loop
    subshell._shutdown_loop()
    with pytest.raises(RuntimeError): asyncio.get_event_loop()
