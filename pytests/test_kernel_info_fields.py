from .kernel_utils import get_shell_reply, start_kernel


def test_kernel_info_fields() -> None:
    with start_kernel() as (_, kc):
        msg_id = kc.kernel_info()
        reply = get_shell_reply(kc, msg_id)
        content = reply["content"]
        assert reply["parent_header"]["msg_id"] == msg_id
        assert content["status"] == "ok"
        assert content["protocol_version"] == "5.3"
        assert content["implementation"] == "ipymini"
        assert content["implementation_version"]
        language = content["language_info"]
        assert language["name"] == "python"
        assert language["file_extension"] == ".py"
