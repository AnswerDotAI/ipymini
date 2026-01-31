import pytest
from IPython.core.completer import provisionalcompleter as _provisionalcompleter
from IPython.core.completer import rectify_completions as _rectify_completions
from ..kernel_utils import *

def _execute_plain(kc, code:str)->str:
    msg_id = kc.execute(code, store_history=False)
    reply = kc.shell_reply(msg_id)
    assert reply["content"]["status"] == "ok"
    output_msgs = kc.iopub_drain(msg_id)
    results = iopub_msgs(output_msgs, "execute_result")
    assert results, "expected execute_result"
    return results[-1]["content"]["data"]["text/plain"]


@pytest.mark.slow
@pytest.mark.parametrize("value,expected", [("0", "False"), ("1", "True")])
def test_use_jedi_env_toggle(value:str, expected:str):
    with start_kernel(extra_env={"IPYMINI_USE_JEDI": value}) as (_, kc):
        result = _execute_plain(kc, "get_ipython().Completer.use_jedi")
        assert result == expected


@pytest.mark.slow
def test_experimental_completions_always_on():
    with start_kernel() as (_, kc):
        msg_id = kc.complete("pri", 3)
        reply = kc.shell_reply(msg_id)
        metadata = reply["content"]["metadata"]
        assert "_jupyter_types_experimental" in metadata
