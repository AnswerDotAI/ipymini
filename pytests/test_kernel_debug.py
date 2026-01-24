from .kernel_utils import DEBUG_INIT_ARGS, debug_request, start_kernel, wait_for_debug_event


def test_debug_smoke() -> None:
    with start_kernel() as (_, kc):
        reply = debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        assert reply.get("success"), f"initialize: {reply}"
        reply = debug_request(kc, "attach")
        assert reply.get("success"), f"attach: {reply}"
        wait_for_debug_event(kc, "initialized")
        reply = debug_request(kc, "evaluate", expression="'a' + 'b'", context="repl")
        assert reply.get("success"), f"evaluate: {reply}"
