import asyncio

import comm
from IPython.core.async_helpers import _asyncio_runner
from ipymini import shell as shell_mod
from ipymini.shell import comm_context

def _run(coro): return asyncio.run(coro)


def test_shell_features(minishell):
    assert shell_mod.__version__
    assert shell_mod.MiniShell

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

    parent = {"header": {"msg_id": "demo2"}}

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=False, comm_sender=sender, parent=parent):
            return await minishell.execute("1/0\n", silent=False, store_history=False, user_expressions={"a": "1+1"})

    res = _run(_go())
    err = res.get("error") or {}
    assert err.get("ename") in ("ZeroDivisionError", "Exception")
    assert res.get("user_expressions") == {}

    parent = {"header": {"msg_id": "demo3"}}

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
            return await minishell.execute("x=1\n", silent=True, store_history=False, user_expressions='{"a":"x+1"}')

    res = _run(_go())
    # Shape depends on IPython, but key should be present on success.
    assert "a" in (res.get("user_expressions") or {})

    rep = minishell.complete("str.", cursor_pos=4)
    assert rep.get("status") == "ok"
    assert isinstance(rep.get("matches"), list)
    assert isinstance(rep.get("cursor_start"), int)
    assert isinstance(rep.get("cursor_end"), int)
    assert "metadata" in rep

    rep = minishell.inspect("len", cursor_pos=3, detail_level=0)
    assert rep.get("status") == "ok"
    assert rep.get("found") is True
    assert isinstance(rep.get("data"), dict)

    rep = minishell.is_complete("for i in range(2):\n")
    assert rep.get("status") in ("incomplete", "complete", "invalid")
    if rep.get("status") == "incomplete": assert rep.get("indent") == " " * 4

    parent = {"header": {"msg_id": "hist"}}

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=False, comm_sender=sender, parent=parent):
            await minishell.execute("x = 123\n", silent=False, store_history=True)
    _run(_go())

    rep = minishell.history("tail", output=False, raw=True, n=1)
    assert rep.get("status") == "ok"
    hist = rep.get("history") or []
    assert hist, "expected at least one history entry"
    last = hist[-1]
    assert isinstance(last, tuple)
    assert any("x = 123" in str(part) for part in last)

    minishell.capture.consume_payload()
    parent = {"header": {"msg_id": "payload"}}
    with minishell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
        minishell.ipy.set_next_input("first")
        minishell.ipy.set_next_input("second")

    payload = minishell.capture.consume_payload()
    next_inputs = [p for p in payload if p.get("source") == "set_next_input"]
    assert len(next_inputs) == 1
    assert next_inputs[0].get("text") == "second"

    seen = []
    parent = {"header": {"msg_id": "p"}}

    def comm_sender(msg_type, parent_msg, **kwargs): seen.append((msg_type, parent_msg, kwargs))

    with comm_context(comm_sender, parent):
        c = comm.create_comm(target_name="demo", primary=False)
        c.open(data={"a": 1})
        c.send(data={"b": 2})
        c.close(data={"c": 3})

    assert [m for (m, _p, _k) in seen][:3] == ["comm_open", "comm_msg", "comm_close"]
    for msg_type, parent_msg, kw in seen:
        assert parent_msg == parent
        content = kw.get("content") or {}
        assert content.get("comm_id")
        assert "data" in content

    called = {"n": 0}
    def comm_sender(*args, **kwargs): called["n"] += 1

    parent = {"header": {"msg_id": "p2"}}
    c = comm.create_comm(target_name="demo2")
    c.open(data={"x": 1})
    assert called["n"] == 0

    with comm_context(comm_sender, parent): c.send(data={"y": 2})
    assert called["n"] == 1

    code = "import asyncio\nawait asyncio.sleep(1)\n"
    shell = minishell.ipy
    if hasattr(shell, "run_cell_async") and hasattr(shell, "should_run_async"):
        try:
            transformed = shell.transform_cell(code)
            exc_tuple = None
        except Exception:
            transformed = code
            exc_tuple = None
        should = shell.should_run_async(code, transformed_cell=transformed, preprocessing_exc_tuple=exc_tuple)
        if should and shell.loop_runner is _asyncio_runner:
            parent = {"header": {"msg_id": "cancel"}}

            async def _go():
                with minishell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
                    task = asyncio.create_task(minishell.execute(code, silent=True, store_history=False))
                    await asyncio.sleep(0.05)
                    cancelled = minishell.cancel_exec_task(asyncio.get_running_loop())
                    assert cancelled is True
                    res = await task
                    err = res.get("error") or {}
                    assert err.get("ename") in ("KeyboardInterrupt", "CancelledError", "Exception")
            _run(_go())
