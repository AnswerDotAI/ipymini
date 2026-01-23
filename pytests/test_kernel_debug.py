from .kernel_utils import DEBUG_INIT_ARGS, debug_request, start_kernel, wait_for_debug_event
def test_debug_initialize() -> None:
    with start_kernel() as (_, kc):
        reply = debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        assert reply.get("success")


def test_debug_attach() -> None:
    with start_kernel() as (_, kc):
        debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        reply = debug_request(kc, "attach")
        assert reply.get("success")
        wait_for_debug_event(kc, "initialized")


def test_debug_evaluate() -> None:
    with start_kernel() as (_, kc):
        debug_request(kc, "initialize", DEBUG_INIT_ARGS)
        debug_request(kc, "attach")
        reply = debug_request(kc, "evaluate", expression="'a' + 'b'", context="repl")
        assert reply.get("success")
