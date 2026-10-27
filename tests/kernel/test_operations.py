from ..aclient import *

setup = '''
from fastcore.basics import PrettyString
from PIL import Image
class Calc:
    def add(self, a:int, b:int) -> int:
        "Add two numbers"
        return a + b
calc = Calc()
pdf = b'%PDF-1.4 demo'
def shout(s:str):
    "Upper-case `s`"
    print("shouting")
    return s.upper()
'''


async def _op(kc, **op):
    "Run the kernel operation in `op`; return its reply content and iopub messages"
    reply, outputs = await kc.exec_drain("", store_history=False, stop_on_error=False, metadata=op)
    return reply["content"], outputs


def _result(outputs):
    "The data of the `execute_result` in `outputs`"
    return iopub_msgs(outputs, "execute_result")[0]["content"]["data"]


async def test_schemas_call_and_eval():
    async with mini_kernel() as (_, kc):
        reply, _ = await kc.exec_drain(setup)
        assert reply["content"]["status"] == "ok"

        # Schemas keep the names as written, and say why a name has none.
        _, out = await _op(kc, schemas=["calc.add", "nope"])
        schemas = _result(out)["application/json"]
        assert schemas["calc.add"]["name"] == "calc.add"
        assert schemas["calc.add"]["parameters"]["properties"]["a"]["type"] == "integer"
        assert "not found" in schemas["nope"]

        # `call` runs a tool with the arguments given. Its prints go out as usual, and a str result arrives as its text.
        _, out = await _op(kc, call=dict(name="calc.add", args=dict(a=2, b=3)))
        assert _result(out)["text/plain"] == "5"
        _, out = await _op(kc, call=dict(name="shout", args=dict(s="hi")))
        assert stream_text(out) == "shouting\n"
        assert _result(out)["text/plain"] == "HI"
        reply, _ = await _op(kc, call=dict(name="nope", args={}))
        assert reply["status"] == "error"

        # `eval` gives a `user_expressions` record per expression, with the value's type in its metadata. `full` marks results not to truncate. Images and media bytes are base64.
        _, out = await _op(kc, eval=["shout('x')", "PrettyString('p')", "Image.new('RGB', (1, 1))", "pdf", "b'hi'", "undefined_name"])
        recs = _result(out)["application/json"]
        assert recs["shout('x')"] == dict(status="ok", data={"text/plain": "X"}, metadata={"type": "str"})
        assert recs["PrettyString('p')"]["metadata"] == {"full": True, "type": "PrettyString"}
        assert isinstance(recs["Image.new('RGB', (1, 1))"]["data"]["image/png"], str)
        assert list(recs["pdf"]["data"]) == ["application/pdf"]
        assert recs["b'hi'"]["data"]["text/plain"] == "b'hi'"
        assert recs["undefined_name"]["status"] == "error" and recs["undefined_name"]["ename"] == "NameError"

        # The kernel advertises the operations it supports.
        info = (await kc.cmd.kernel_info())["content"]
        assert {"schemas", "call", "eval"} <= set(info["supported_features"])
