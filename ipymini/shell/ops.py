"Kernel operations: `schemas`, `call` and `eval`, which a client sends in execute metadata in place of code."
import traceback
from binascii import b2a_base64

from fastcore.aio import maybe_await
from fastcore.funccall import call_func, get_schema_nm
from fastcore.xtras import detect_mime
from kernmini import user_code

ops = ("schemas", "call", "eval")


def fmt_result(ipy, obj):
    "MIME bundle and metadata for tool result `obj`"
    if isinstance(obj, str): data, md = {"text/plain": obj}, {}
    elif isinstance(obj, bytes) and (mime := detect_mime(obj)): data, md = {mime: obj}, {}
    else: data, md = ipy.display_formatter.format(obj)
    data = {k: b2a_base64(v, newline=False).decode() if isinstance(v, bytes) else v for k, v in data.items()}
    if getattr(obj, "full", False): md = {**md, "full": True}
    return data, md


def _schema(ns, name):
    "Schema of tool `name` in `ns`, or a message saying why there is none"
    try: return get_schema_nm(name, ns, pname="parameters", evalable=True, skip_hidden=True)
    except (KeyError, AttributeError): return f"`{name}` not found. Did you run it?"
    except Exception as e: return f"`{name}`: {e}."


def _eval(ipy, expr):
    "`user_expressions` record for `expr`, evaluated in the user namespace without awaiting"
    try:
        obj = eval(expr, ipy.user_ns)
        data, md = fmt_result(ipy, obj)
    except Exception as e: return dict(status="error", ename=type(e).__name__, evalue=str(e), traceback=traceback.format_exception(e))
    return dict(status="ok", data=data, metadata={**md, "type": type(obj).__name__})


async def run_op(ipy, name, params):
    "Bundle and metadata of operation `name`'s result, or None when a `call` returns None. User code runs inside `kernmini.user_code()`, where an interrupt can stop it."
    with user_code():
        if name == "schemas": return {"application/json": {n: _schema(ipy.user_ns, n) for n in params}}, {}
        if name == "eval": return {"application/json": {e: _eval(ipy, e) for e in params}}, {}
        res = call_func(params["name"], params["args"], ipy.user_ns)
    res = await maybe_await(res)
    return None if res is None else fmt_result(ipy, res)
