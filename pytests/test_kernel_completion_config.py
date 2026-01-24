import pytest
from .kernel_utils import drain_iopub, get_shell_reply, iopub_msgs, start_kernel

try:
    from IPython.core.completer import provisionalcompleter as _provisionalcompleter
    from IPython.core.completer import rectify_completions as _rectify_completions

    _EXPERIMENTAL_AVAILABLE = True
except Exception: _EXPERIMENTAL_AVAILABLE = False


def _execute_plain(kc, code: str) -> str:
    msg_id = kc.execute(code, store_history=False)
    reply = get_shell_reply(kc, msg_id)
    assert reply["content"]["status"] == "ok"
    output_msgs = drain_iopub(kc, msg_id)
    results = iopub_msgs(output_msgs, "execute_result")
    assert results, "expected execute_result"
    return results[-1]["content"]["data"]["text/plain"]


@pytest.mark.slow
@pytest.mark.parametrize("value,expected", [("0", "False"), ("1", "True")])
def test_use_jedi_env_toggle(value: str, expected: str) -> None:
    with start_kernel(extra_env={"IPYMINI_USE_JEDI": value}) as (_, kc):
        result = _execute_plain(kc, "get_ipython().Completer.use_jedi")
        assert result == expected


@pytest.mark.slow
@pytest.mark.skipif(not _EXPERIMENTAL_AVAILABLE, reason="experimental completions not available")
@pytest.mark.parametrize("value,expected", [("1", True), ("0", False)])
def test_experimental_completions_env_toggle(value: str, expected: bool) -> None:
    with start_kernel(extra_env={"IPYMINI_EXPERIMENTAL_COMPLETIONS": value}) as (_, kc):
        msg_id = kc.complete("pri", 3)
        reply = get_shell_reply(kc, msg_id)
        metadata = reply["content"]["metadata"]
        assert ("_jupyter_types_experimental" in metadata) is expected
