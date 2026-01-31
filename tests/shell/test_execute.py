import asyncio

from ipymini.shell import MiniShell


def _run(coro): return asyncio.run(coro)


def test_execute_success_captures_stream_display_result(minishell):
    parent = {"header": {"msg_id": "demo"}}
    def sender(*args, **kwargs): return None

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=False, comm_sender=sender, parent=parent):
            return await minishell.execute("from IPython.display import display\nprint('hello')\ndisplay('hi')\n1+1\n",
                silent=False, store_history=False)

    res = _run(_go())
    assert res.get("error") is None
    assert any("hello" in m.get("text","") for m in res.get("streams", []))
    assert any(ev.get("type") == "display" for ev in res.get("display", []))
    assert "2" in (res.get("result") or {}).get("text/plain", "")


def test_execute_error_returns_error_dict(minishell):
    parent = {"header": {"msg_id": "demo2"}}
    def sender(*args, **kwargs): return None

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=False, comm_sender=sender, parent=parent):
            return await minishell.execute("1/0\n", silent=False, store_history=False, user_expressions={"a": "1+1"})

    res = _run(_go())
    err = res.get("error") or {}
    assert err.get("ename") in ("ZeroDivisionError", "Exception")
    assert res.get("user_expressions") == {}


def test_execute_user_expressions_accepts_json_string(minishell):
    parent = {"header": {"msg_id": "demo3"}}
    def sender(*args, **kwargs): return None

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
            return await minishell.execute("x=1\n", silent=True, store_history=False, user_expressions='{"a":"x+1"}')

    res = _run(_go())
    # Shape depends on IPython, but key should be present on success.
    assert "a" in (res.get("user_expressions") or {})
