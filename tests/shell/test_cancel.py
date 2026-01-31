import asyncio

import pytest
from IPython.core.async_helpers import _asyncio_runner


def test_cancel_exec_task_cancels_async_cell(minishell):
    code = "import asyncio\nawait asyncio.sleep(1)\n"
    shell = minishell.ipy
    if not (hasattr(shell, "run_cell_async") and hasattr(shell, "should_run_async")): pytest.skip("IPython async APIs not available")
    try:
        transformed = shell.transform_cell(code)
        exc_tuple = None
    except Exception:
        transformed = code
        exc_tuple = None
    should = shell.should_run_async(code, transformed_cell=transformed, preprocessing_exc_tuple=exc_tuple)
    if not should or shell.loop_runner is not _asyncio_runner: pytest.skip("async cancel path not active for this IPython config")

    parent = {"header": {"msg_id": "cancel"}}
    def sender(*args, **kwargs): return None

    async def _go():
        with minishell.execution_context(allow_stdin=False, silent=True, comm_sender=sender, parent=parent):
            task = asyncio.create_task(minishell.execute(code, silent=True, store_history=False))
            await asyncio.sleep(0.05)
            cancelled = minishell.cancel_exec_task(asyncio.get_running_loop())
            assert cancelled is True
            res = await task
            err = res.get("error") or {}
            assert err.get("ename") in ("KeyboardInterrupt", "CancelledError", "Exception")
    asyncio.run(_go())
