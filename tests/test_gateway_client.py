import asyncio, ast, os, types
from queue import Empty
from jupyter_client import AsyncKernelClient, KernelManager
from .kernel_utils import build_env, ensure_separate_process


def _jmsg(session, cts, cts_typ, msg_id, msg_type="execute_request", user_expressions=None,
    store_history=False, silent=False, allow_stdin=True, stop_on_error=True, subsh_id=None):
    hdr = session.msg_header(msg_type)
    hdr["msg_id"] = msg_id
    if subsh_id is not None: hdr["subshell_id"] = subsh_id
    content = {cts_typ: cts, "silent": silent, "store_history": store_history,
        "allow_stdin": allow_stdin, "stop_on_error": stop_on_error}
    if user_expressions is not None: content["user_expressions"] = user_expressions
    return session.msg(msg_type, content, header=hdr)


def _attach_gateway_methods(kc: AsyncKernelClient):
    async def _router():
        while not kc._gw_stop.is_set():
            try:
                msg = await kc.get_shell_msg(timeout=0.1)
                parent_id = msg.get("parent_header", {}).get("msg_id")
                waiter = kc._waiters.get(parent_id)
                if waiter is not None: waiter.put_nowait(msg)
            except Empty: pass
            except (asyncio.CancelledError, RuntimeError): break
        return None

    def start_router(self):
        self._gw_stop = asyncio.Event()
        self._waiters = {}
        self._gw_task = asyncio.create_task(_router())

    def stop_router(self):
        self._gw_stop.set()
        task = getattr(self, "_gw_task", None)
        if task is not None: task.cancel()

    async def send_wait(self, cts, msg_id=None, cts_typ="code", timeout=1, priority=False, **kwargs):
        if msg_id is None: msg_id = os.urandom(16).hex()
        q = asyncio.Queue()
        self._waiters[msg_id] = q
        subsh_id = self.priority if priority else None
        msg = _jmsg(self.session, cts, cts_typ, msg_id=msg_id, subsh_id=subsh_id, **kwargs)
        self.shell_channel.send(msg)
        try: return await asyncio.wait_for(q.get(), timeout=timeout)
        finally: self._waiters.pop(msg_id, None)

    async def exec_func(self, func, *args, _user_expressions=None, _call=True, _timeout=10, _priority=False, **kw):
        args_str = ", ".join(repr(arg) for arg in args)
        kwargs_str = ", ".join(f"{k}={v!r}" for k, v in kw.items())
        if _call: params = "(" + args_str + (", " if args and kw else "") + kwargs_str + ")"
        else: params = ""
        code = f"{func}{params}"
        return await send_wait(self, code, user_expressions=_user_expressions, store_history=False,
            timeout=_timeout, priority=_priority)

    async def eval_func(self, func, *args, _timeout=60, _literal=True, _priority=False, _call=True, **kw):
        if _call:
            code = f"""import asyncio
__res = {func}(*{args!r}, **{kw!r})
if asyncio.iscoroutine(__res): __res = await __res
"""
        else: code = f"__res = {func}"
        try:
            r = await exec_func(self, code, _call=False, _user_expressions={"__res": "__res"},
                _timeout=_timeout, _priority=_priority)
        except TimeoutError: return "timeout"
        if not isinstance(r, dict) or "content" not in r: raise RuntimeError(f"Eval failed: {r}")
        cts = r["content"]
        if cts["status"] != "ok": return f"{cts.get('ename')}: {cts.get('evalue')}"
        res = cts.get("user_expressions", {}).get("__res", {}).get("data", {}).get("text/plain")
        if not _literal: return res
        return ast.literal_eval(res) if res is not None else None

    kc.start_router = types.MethodType(start_router, kc)
    kc.stop_router = types.MethodType(stop_router, kc)
    kc.send_wait = types.MethodType(send_wait, kc)
    kc.exec = types.MethodType(exec_func, kc)
    kc.eval = types.MethodType(eval_func, kc)


async def _start_gateway_kernel():
    env = build_env()
    os.environ["JUPYTER_PATH"] = env["JUPYTER_PATH"]
    km = KernelManager(kernel_name="ipymini")
    km.start_kernel(env=env)
    ensure_separate_process(km)
    kc = AsyncKernelClient(**km.get_connection_info(session=True))
    kc.parent = km
    kc.priority = None
    kc.start_channels()
    await kc.wait_for_ready(timeout=2)
    _attach_gateway_methods(kc)
    kc.start_router()
    return km, kc


def test_gateway_eval():
    async def _run():
        km, kc = await _start_gateway_kernel()
        try:
            res = await kc.eval("asyncio.sleep", 0)
            assert res is None
            res = await kc.eval("len", [1, 2, 3])
            assert res == 3
        finally:
            kc.stop_router()
            kc.stop_channels()
            km.shutdown_kernel(now=True)

    asyncio.run(_run())
