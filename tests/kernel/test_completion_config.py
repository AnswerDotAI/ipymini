import pytest
from ..aclient import *
from ..kernel_utils import iopub_msgs

async def _execute_plain(kc, code:str)->str:
    reply, outputs = await kc.exec_drain(code, store_history=False)
    assert reply["content"]["status"] == "ok"
    results = iopub_msgs(outputs, "execute_result")
    assert results, "expected execute_result"
    return results[-1]["content"]["data"]["text/plain"]


@pytest.mark.slow
@pytest.mark.parametrize("value,expected", [("0", "False"), ("1", "True")])
async def test_use_jedi_env_toggle(value:str, expected:str):
    async with mini_kernel(extra_env={"IPYMINI_USE_JEDI": value}) as (_, kc):
        result = await _execute_plain(kc, "get_ipython().Completer.use_jedi")
        assert result == expected


@pytest.mark.slow
async def test_experimental_completions_always_on():
    async with mini_kernel() as (_, kc):
        reply = await kc.cmd.complete(code="pri", cursor_pos=3)
        metadata = reply["content"]["metadata"]
        assert "_jupyter_types_experimental" in metadata
