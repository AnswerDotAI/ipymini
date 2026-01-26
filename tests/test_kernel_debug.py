from .kernel_utils import *


def test_debug_smoke():
    with start_kernel() as (_, kc):
        reply = kc.dap.initialize(**debug_init_args)
        assert reply.get("success"), f"initialize: {reply}"
        reply = kc.dap.attach()
        assert reply.get("success"), f"attach: {reply}"
        wait_for_debug_event(kc, "initialized")
        reply = kc.dap.evaluate(expression="'a' + 'b'", context="repl")
        assert reply.get("success"), f"evaluate: {reply}"
