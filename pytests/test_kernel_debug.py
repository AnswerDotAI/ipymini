from .kernel_utils import DEBUG_INIT_ARGS, debug_request, start_kernel, wait_for_debug_event

def test_debug_initialize() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        reply = debug_request(
            kc,
            "initialize",
            DEBUG_INIT_ARGS,
        )
        if debugpy:
            assert reply.get("success")
        else:
            assert reply == {}


def test_debug_attach() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        debug_request(
            kc,
            "initialize",
            DEBUG_INIT_ARGS,
        )
        reply = debug_request(kc, "attach")
        if debugpy:
            assert reply.get("success")
            wait_for_debug_event(kc, "initialized")
        else:
            assert reply == {}


def test_debug_evaluate() -> None:
    with start_kernel() as (_, kc):
        try:
            import debugpy  # noqa: F401
        except Exception:
            debugpy = None
        debug_request(
            kc,
            "initialize",
            DEBUG_INIT_ARGS,
        )
        debug_request(kc, "attach")
        reply = debug_request(kc, "evaluate", {"expression": "'a' + 'b'", "context": "repl"})
        if debugpy:
            assert reply.get("success")
        else:
            assert reply == {}
