import asyncio


def test_history_tail_includes_latest(minishell):
    parent = {"header": {"msg_id": "hist"}}
    def sender(*args, **kwargs): return None

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=False, comm_sender=sender, parent=parent):
            await minishell.execute("x = 123\n", silent=False, store_history=True)
    asyncio.run(_go())

    rep = minishell.history("tail", output=False, raw=True, n=1)
    assert rep.get("status") == "ok"
    hist = rep.get("history") or []
    assert hist, "expected at least one history entry"
    # IPython returns tuples: (session, line, input[, output])
    last = hist[-1]
    assert isinstance(last, tuple)
    assert any("x = 123" in str(part) for part in last)
