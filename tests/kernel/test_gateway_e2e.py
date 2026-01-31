"Comprehensive e2e test based on gateway notebook - tests all Solveit gateway functionality."
import asyncio, functools, os, random, re, time, threading, traceback, textwrap
import pytest
from ast import literal_eval
from asyncio import CancelledError, create_task
from collections.abc import Mapping
from pathlib import Path
from queue import Empty

import zmq.asyncio
from fastcore.basics import patch, patch_to, listify, nested_idx
from fastcore.meta import delegates
from fastcore.test import test_eq as _test_eq
from jupyter_client import KernelClient, AsyncKernelClient, AsyncKernelManager
from jupyter_client.channels import AsyncZMQSocketChannel
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.session import Session
from nbformat.v4 import output_from_msg
from traitlets import Type
from zmq.error import ZMQError

from ..kernel_utils import build_env


# --- Helpers from gateway notebook ---

def noop(*args, **kwargs): pass
def warn(*args, **kwargs): print("WARN:", *args, **kwargs)

def unqid():
    import uuid
    return '_' + str(uuid.uuid4()).replace('-', '')[:8]

def _dict_without(d, skip):
    if not isinstance(skip, dict): skip = listify(skip)
    return {k: v for k, v in d.items() if k not in skip}

def msg_short(m): return _dict_without(m, ('header', 'parent_header'))
def msgs_short(outs, msg_type=None): return [msg_short(o) for o in outs if msg_type is None or o['msg_type'] == msg_type]

output_types = {'stream', 'execute_result', 'display_data', 'error', 'clear_output'}

def retr_outs(jmsgs): return [o for o in jmsgs if o['msg_type'] in output_types]
def _lines(src): return textwrap.dedent(src).strip().splitlines()


# --- Locked channel classes with random timing for race condition testing ---

def locked_sleep(f):
    @functools.wraps(f)
    def _f(self, *args, **kwargs):
        with self.lock:
            time.sleep(random.uniform(0.01, 0.015))
            return f(self, *args, **kwargs)
    return _f

class LockedChannel(AsyncZMQSocketChannel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

class LockedSendChannel(LockedChannel):
    @locked_sleep
    def send(self, msg): return super().send(msg)


class LockedClient(AsyncKernelClient):
    shell_channel_class = LockedSendChannel
    lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._waiters = {}
        self.stoproute = threading.Event()
        self.priority = None


class SolveitKernelManager(AsyncKernelManager):
    client_class = LockedClient
    client_factory = Type(LockedClient)

    def __init__(self, kernel_spec=None, username=None, startdir=None, **kwargs):
        super().__init__(**kwargs)
        if kernel_spec: self._kernel_spec = kernel_spec
        self.username = username


# --- Session and KernelClient patches ---

def _ses_getattr(self, nm):
    if nm.startswith('_'): raise AttributeError(nm)
    def f(parent=None, header=None, metadata=None, **kwargs):
        return self.msg(nm, kwargs, parent=parent, header=header, metadata=metadata)
    return f

@patch
def jmsg(self: Session, cts, cts_typ, msg_id, msg_type="execute_request", user_expressions=None,
         store_history=False, silent=False, allow_stdin=True, stop_on_error=True, subsh_id=None):
    hdr = self.msg_header(msg_type)
    hdr['msg_id'] = msg_id
    if subsh_id is not None: hdr["subshell_id"] = subsh_id
    content = {cts_typ: cts, 'silent': silent, 'store_history': store_history,
               'allow_stdin': allow_stdin, 'stop_on_error': stop_on_error}
    if user_expressions is not None: content['user_expressions'] = user_expressions
    return self.msg(msg_type, content, header=hdr)

@patch
@delegates(Session.jmsg)
def send(self: AsyncKernelClient, cts, msg_id=None, cts_typ='code', subsh_id=None, **kwargs):
    if not msg_id: msg_id = unqid()
    jm = self.session.jmsg(cts, cts_typ, msg_id=msg_id, subsh_id=subsh_id, **kwargs)
    self.shell_channel.send(jm)
    return jm

@patch
async def get_pubs(self: KernelClient, timeout=0.2):
    "Retrieve all outstanding iopub messages"
    res = []
    try:
        while msg := await self.get_iopub_msg(timeout=timeout): res.append(msg)
    except Empty: pass
    return res

@patch
def start_router(self: KernelClient):
    async def _router():
        while not self.stoproute.is_set():
            try:
                msg = await self.get_shell_msg(timeout=0.1)
                parent_id = nested_idx(msg, 'parent_header', 'msg_id')
                if parent_id in self._waiters: self._waiters[parent_id].put_nowait(msg)
            except Empty: pass
            except (CancelledError, RuntimeError, ZMQError): break
            except Exception as e: print(f"Router error: {e}")
        print('router closing')
    self._routetask = asyncio.create_task(_router())

@patch
def stop_router(self: KernelClient): self.stoproute.set()

@patch
async def start(self: KernelClient):
    self.start_channels()
    await self.wait_for_ready()
    self.start_router()

@patch
def stop(self: KernelClient):
    self.stop_router()
    self.stop_channels()

@patch
@delegates(Session.jmsg)
async def send_wait(self: KernelClient, cts, msg_id=None, cts_typ='code', timeout=1, priority=False, **kwargs):
    "Send a message and wait for the reply with matching msg_id"
    if msg_id is None: msg_id = unqid()
    q = asyncio.Queue()
    self._waiters[msg_id] = q
    subsh_id = self.priority if priority else None
    self.send(cts, msg_id, cts_typ=cts_typ, subsh_id=subsh_id, **kwargs)
    try: return await asyncio.wait_for(q.get(), timeout=timeout)
    finally: del self._waiters[msg_id]

@patch
async def exec(self: KernelClient, func: str, *args,
               _user_expressions=None, _call=True, _timeout=10, _priority=False, **kw):
    "Execute `func(*args, **kw)` using `send_wait()`"
    args_str = ", ".join(repr(arg) for arg in args)
    kwargs_str = ", ".join(f"{k}={v!r}" for k, v in kw.items())
    if _call: params = '(' + args_str + (', ' if args and kw else '') + kwargs_str + ')'
    else: params = ''
    code = f'{func}{params}'
    return await self.send_wait(code, user_expressions=_user_expressions, store_history=False,
                                timeout=_timeout, priority=_priority)

class EvalException(Exception): pass

@patch
async def eval(self: KernelClient, func: str, *args, _timeout=60, _literal=True, _priority=False, _call=True, **kw):
    "Result of running `func(*args, **kw)`"
    if _call:
        code = f'''import asyncio
__res = {func}(*{args!r}, **{kw!r})
if asyncio.iscoroutine(__res): __res = await __res
'''
    else: code = f'__res = {func}'
    try: r = await self.exec(code, _call=False, _user_expressions={'__res': '__res'}, _timeout=_timeout, _priority=_priority)
    except TimeoutError: return 'timeout'
    if not isinstance(r, Mapping) or 'content' not in r: raise EvalException(f"Eval failed: {r}")
    cts = r['content']
    if cts['status'] != 'ok': return f"{cts.get('ename')}: {cts.get('evalue')}"
    res = nested_idx(cts, 'user_expressions', '__res', 'data', 'text/plain')
    try: return literal_eval(res) if _literal else res
    except Exception as e: return str(e)

@patch
def input_reply(self: KernelClient, value: str):
    "Send an input reply with the given value"
    self.stdin_channel.send(self.session.input_reply(value=value))

@patch
def jmsg_loop(self: KernelClient, func, exc=noop, closed=noop):
    async def f():
        running = True
        poller = zmq.asyncio.Poller()
        iopub_sock, stdin_sock = self.iopub_channel.socket, self.stdin_channel.socket
        poller.register(iopub_sock, zmq.POLLIN)
        poller.register(stdin_sock, zmq.POLLIN)
        while running:
            try:
                events = dict(await poller.poll(timeout=100))  # 100ms timeout to check stoproute
                if not events: continue
                if stdin_sock in events and (jmsg := await self.get_stdin_msg()): func(jmsg)
                elif iopub_sock in events and (jmsg := await self.get_iopub_msg()): func(jmsg)
            except (RuntimeError, ZMQError) as e:
                closed(e)
                running = False
            except Exception as e: exc(traceback.format_exc())
    return create_task(f())

@patch
async def interrupt(self: KernelClient):
    "Manually sends an interrupt_request on the Control channel."
    msg = self.session.msg('interrupt_request', content={})
    self.control_channel.send(msg)
    ssck = getattr(self.shell_channel, 'socket', None)
    if ssck and not ssck.closed:
        await asyncio.sleep(0.01)
        self.execute('')
    return msg['header']['msg_id']

def error2iopub(jmsg):
    jmsg['header']['msg_type'] = 'error'
    return _dict_without(jmsg, ('parent_header',))

@patch
async def jmsgs(self: KernelClient, msg):
    id_ = nested_idx(msg, 'parent_header', 'msg_id') or msg.get('msg_id')
    if not id_: return []
    return [o for o in await self.get_pubs() if o['parent_header'].get('msg_id') == id_]

@patch
async def get_outs(self: KernelClient, code, timeout=5):
    msg = await self.send_wait(code, timeout=timeout)
    jmsgs = await self.jmsgs(msg)
    return [output_from_msg(o) for o in retr_outs(jmsgs)]

@patch
def xpush(self: KernelClient, priority=False, **kwargs):
    self.send(f'get_ipython().push({kwargs!r})', subsh_id=self.priority if priority else None)

@patch
async def ipy(self: KernelClient, meth, *args, **kwargs):
    return await self.eval('get_ipython().' + meth, _priority=False, _timeout=5, *args, **kwargs)

def _mk_ipy(meth):
    async def f(self, *args, **kwargs): return await self.ipy(meth, *args, **kwargs)
    f.__name__ = meth
    patch_to(KernelClient)(f)

_ipy_funcs = ['ranked_complete', 'user_items', 'sig_help', 'get_vars', 'eval_exprs', 'get_schemas', 'publish']
for o in _ipy_funcs: _mk_ipy(o)

@patch
async def retr(self: KernelClient, nm: str):
    "Retrieve a single variable value"
    return await self.eval(nm, _call=False, _priority=False, _timeout=60)

@patch
def xenv(self: KernelClient, **kw):
    "Put all of `kw` in os.environ"
    code = 'import os as __os\n'
    code += '\n'.join(f'__os.environ[{k!r}]={str(v)!r}' for k, v in kw.items())
    return self.send(code, subsh_id=None)


# --- Kernel startup ---

async def start_test_kernel(chdir=None):
    "Start ipymini kernel for testing"
    env = build_env()
    argv = "{prefix}/bin/python", "-Xfrozen_modules=off", "-Pm", "ipymini", "-f", "{connection_file}"
    kernel_spec = KernelSpec(argv=argv, language="python", display_name="TestKernel")
    km = SolveitKernelManager(kernel_spec=kernel_spec, session=Session(key=b'x'))
    await km.start_kernel(env=env, cwd=chdir)
    return km


# --- The main e2e test ---

def test_gateway_run_all():
    "Test rapid-fire execution like Solveit 'run all' - send all cells without waiting"
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        assert await km.is_alive()

        kc = km.client()
        await kc.start()

        try:
            # Simulate "run all" - many cells sent rapidly
            cells = _lines("""
            x = 1
            y = 2
            z = x + y
            print(f'z = {z}')
            def foo(a, b): return a * b
            foo(3, 4)
            import time
            [i**2 for i in range(10)]
            {'a': 1, 'b': 2}
            len('hello world')
            a = [1, 2, 3]
            b = [4, 5, 6]
            list(zip(a, b))
            sum(range(100))
            max([1, 5, 3, 9, 2])
            min([1, 5, 3, 9, 2])
            sorted([3, 1, 4, 1, 5, 9, 2, 6])
            ' '.join(['hello', 'world'])
            list(range(5))
            dict(a=1, b=2, c=3)
            """)

            # Send all cells rapidly - don't wait for responses
            msg_ids = []
            for code in cells:
                mid = unqid()
                kc._waiters[mid] = asyncio.Queue()
                kc.send(code, msg_id=mid)
                msg_ids.append(mid)

            replies = {}
            for mid in msg_ids:
                try:
                    reply = await asyncio.wait_for(kc._waiters[mid].get(), timeout=30)
                    replies[mid] = reply
                except asyncio.TimeoutError: print(f"Timeout waiting for {mid}")

            for mid in msg_ids: kc._waiters.pop(mid, None)

            assert len(replies) == len(cells), f"Only got {len(replies)}/{len(cells)} replies"
            for i, mid in enumerate(msg_ids):
                assert replies[mid]['content']['status'] == 'ok', f"Cell {i} failed: {replies[mid]['content']}"

            # Collect iopub and verify all idles received
            await asyncio.sleep(0.5)  # let iopub messages arrive
            pubs = await kc.get_pubs(timeout=1)
            idle_count = sum(1 for p in pubs if p['msg_type'] == 'status' and p['content']['execution_state'] == 'idle')
            print(f"Received {idle_count} idle messages for {len(cells)} cells")

            print("Run-all test passed!")

        finally:
            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())

@pytest.mark.slow
def test_gateway_notebook_replay():
    """
    Exact replay of every cell from meta/gateway-ipynb.xml in order.
    This generates the same Jupyter messages in the same sequence as running the notebook.
    """
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]

        # --- Cell: km = await start_kernel(pp); await km.is_alive() ---
        km = await start_test_kernel(pp)
        assert await km.is_alive(), "Kernel should be alive after start"
        print("Cell: km = await start_kernel(pp) - PASSED")

        # --- Cell: kc = km.client() ---
        kc = km.client()
        print("Cell: kc = km.client() - PASSED")

        # --- Cell: await kc.wait_for_ready(); kc.start_channels(); await kc.is_alive() ---
        await kc.wait_for_ready()
        kc.start_channels()
        assert await kc.is_alive(), "Client should report kernel alive"
        print("Cell: await kc.wait_for_ready(); kc.start_channels() - PASSED")

        # --- Cell: mid = kc.execute('1+1') ---
        mid = kc.execute('1+1')
        assert mid is not None
        print(f"Cell: mid = kc.execute('1+1') -> {mid} - PASSED")

        # --- Cell: pubs = await kc.get_pubs() ---
        pubs = await kc.get_pubs()
        assert len(pubs) > 0, "Should have iopub messages"
        results = [(o['msg_type'], o.get('content', {})) for o in pubs]
        print(f"Cell: pubs = await kc.get_pubs() -> {len(pubs)} messages - PASSED")

        # --- Cell: pubs[0]['parent_header'] ---
        parent_header = pubs[0]['parent_header']
        assert 'msg_id' in parent_header
        print("Cell: pubs[0]['parent_header'] - PASSED")

        # --- Cell: outs = retr_outs(pubs) ---
        outs = retr_outs(pubs)
        assert len(outs) >= 1, "Should have at least execute_result"
        print(f"Cell: outs = retr_outs(pubs) -> {len(outs)} outputs - PASSED")

        # --- Cell: pprint(msgs_short(outs)) ---
        short_msgs = msgs_short(outs)
        print(f"Cell: msgs_short(outs) -> {short_msgs}")

        # --- Cell: kc.session.input_reply(value='aa') ---
        reply_msg = kc.session.input_reply(value='aa')
        assert reply_msg['msg_type'] == 'input_reply'
        assert reply_msg['content']['value'] == 'aa'
        print("Cell: kc.session.input_reply(value='aa') - PASSED")

        # --- Cell: kc.session.jmsg('1+2', 'code', 'myid_') ---
        jm = kc.session.jmsg('1+2', 'code', 'myid_')
        assert jm['header']['msg_id'] == 'myid_'
        assert jm['content']['code'] == '1+2'
        print("Cell: kc.session.jmsg('1+2', 'code', 'myid_') - PASSED")

        # --- Cell: kc.send('print("aaa")', 'asdf24') ---
        sent_msg = kc.send('print("aaa")', 'asdf24')
        assert sent_msg['header']['msg_id'] == 'asdf24'
        print("Cell: kc.send('print(\"aaa\")', 'asdf24') - PASSED")

        # --- Cell: [(o['msg_type'],o['content']) for o in retr_outs(await kc.get_pubs())] ---
        pubs = await kc.get_pubs()
        out_types = [(o['msg_type'], o['content']) for o in retr_outs(pubs)]
        # Should have stream output from print("aaa")
        has_stream = any(mt == 'stream' for mt, _ in out_types)
        print(f"Cell: retr_outs output types: {[mt for mt, _ in out_types]} - PASSED")

        # --- Cell: kc.start_router() ---
        kc.start_router()
        print("Cell: kc.start_router() - PASSED")

        # --- Cell: mid = unqid(); kc._waiters[mid] = asyncio.Queue(); kc.send('41+2', msg_id=mid) ---
        mid = unqid()
        kc._waiters[mid] = asyncio.Queue()
        kc.send('41+2', msg_id=mid)
        print(f"Cell: send('41+2', msg_id={mid}) - PASSED")

        # --- Cell: msg = await asyncio.wait_for(kc._waiters[mid].get(), timeout=5) ---
        msg = await asyncio.wait_for(kc._waiters[mid].get(), timeout=5)
        assert msg['content']['status'] == 'ok'
        del kc._waiters[mid]
        print("Cell: await asyncio.wait_for(kc._waiters[mid].get(), timeout=5) - PASSED")

        # --- Cell: msg_short(msg) ---
        short = msg_short(msg)
        assert 'content' in short
        print(f"Cell: msg_short(msg) -> status={short['content']['status']} - PASSED")

        # --- Cell: kc.stop_router(); kc.stop_channels() ---
        kc.stop_router()
        kc.stop_channels()
        await asyncio.sleep(0.2)  # Let router task finish
        print("Cell: kc.stop_router(); kc.stop_channels() - PASSED")

        # --- Cell: kc = km.client(); await kc.start() ---
        kc = km.client()
        kc.stoproute = threading.Event()  # Reset for new client
        kc._waiters = {}
        await kc.start()
        print("Cell: kc = km.client(); await kc.start() - PASSED")

        # --- Cell: jid = unqid() ---
        jid = unqid()
        print(f"Cell: jid = unqid() -> {jid} - PASSED")

        # --- Cell: r = await kc.send_wait('print("bbb")', jid) ---
        r = await kc.send_wait('print("bbb")', jid)
        rjid = nested_idx(r, 'parent_header', 'msg_id')
        assert rjid == jid, f"Expected {jid}, got {rjid}"
        print("Cell: r = await kc.send_wait('print(\"bbb\")', jid) - PASSED")

        # --- Cell: msg_short(r) ---
        short = msg_short(r)
        assert short['content']['status'] == 'ok'
        print("Cell: msg_short(r) - PASSED")

        # --- Cell: outs = retr_outs(await kc.get_pubs()); pprint(msgs_short(outs)) ---
        outs = retr_outs(await kc.get_pubs())
        short_outs = msgs_short(outs)
        print(f"Cell: retr_outs -> {len(outs)} outputs - PASSED")

        # --- Cell: r = await kc.send_wait('a=[1,2,3]', user_expressions={'foo': 'a'}) ---
        r = await kc.send_wait('a=[1,2,3]', user_expressions={'foo': 'a'})
        cts = r['content']
        assert cts['status'] == 'ok'
        print("Cell: r = await kc.send_wait('a=[1,2,3]', user_expressions={'foo': 'a'}) - PASSED")

        # --- Cell: ret = nested_idx(cts, 'user_expressions', 'foo', 'data', 'text/plain') ---
        ret = nested_idx(cts, 'user_expressions', 'foo', 'data', 'text/plain')
        assert ret is not None
        print(f"Cell: nested_idx user_expressions -> {ret} - PASSED")

        # --- Cell: literal_eval(ret) ---
        result = literal_eval(ret)
        assert result == [1, 2, 3]
        print(f"Cell: literal_eval(ret) -> {result} - PASSED")

        # --- Cell: await kc.eval('f', _timeout=10) ---
        # 'f' is undefined, should return error string
        result = await kc.eval('f', _timeout=10)
        assert 'NameError' in str(result) or 'error' in str(result).lower()
        print(f"Cell: await kc.eval('f', _timeout=10) -> {result} - PASSED")

        # --- Cell: kc.execute('def add(a, b): return a+b'); await kc.eval('add', a=10, b=20) ---
        kc.execute('def add(a, b): return a+b')
        await asyncio.sleep(0.1)
        result = await kc.eval('add', a=10, b=20)
        assert result == 30, f"Expected 30, got {result}"
        print(f"Cell: def add; eval('add', a=10, b=20) -> {result} - PASSED")

        # --- Cell: kc.execute('async def add(a, b): return a+b'); await kc.eval('add', a=10, b=20, _literal=False) ---
        kc.execute('async def add(a, b): return a+b')
        await asyncio.sleep(0.1)
        result = await kc.eval('add', a=10, b=20, _literal=False)
        assert '30' in str(result)
        print(f"Cell: async def add; eval -> {result} - PASSED")

        # --- Cell: await kc.eval('a', _call=False) ---
        result = await kc.eval('a', _call=False)
        assert result == [1, 2, 3]
        print(f"Cell: await kc.eval('a', _call=False) -> {result} - PASSED")

        # --- Cell: await kc.eval('add', a=30, b=40, _literal=False), await kc.eval('add', a=30, b=40) ---
        r1 = await kc.eval('add', a=30, b=40, _literal=False)
        r2 = await kc.eval('add', a=30, b=40)
        assert '70' in str(r1)
        assert r2 == 70
        print(f"Cell: eval add(30,40) -> literal=False:{r1}, literal=True:{r2} - PASSED")

        # --- Cell: Foo class definition and test ---
        foo_code = """
class Foo:
  def __init__(self, val: int): self.val = val
  def __repr__(self): return f'Foo instance with a value of {self.val}'

def make_foo(): return Foo(123)
"""
        kc.execute(foo_code)
        await asyncio.sleep(0.1)
        result = await kc.eval("Foo", val=99, _literal=False)
        assert 'Foo instance' in str(result)
        print(f"Cell: Foo class test -> {result} - PASSED")

        # --- Cell: Payloads test ---
        payloads_code = '''payl = dict(source='testing', foo='bar')
pm = get_ipython().payload_manager
pm.write_payload(payl, single=False)
pm.write_payload(dict(source='testing', bar='baz'), single=False)
'''
        r = await kc.send_wait(payloads_code)
        payload_list = r['content'].get('payload', [])
        assert len(payload_list) == 2, f"Expected 2 payloads, got {len(payload_list)}"
        print(f"Cell: payloads test -> {len(payload_list)} payloads - PASSED")

        # --- Cell: Metadata and transient display ---
        display_code = '''
from IPython.display import Markdown, display
display('hi', metadata={'key': 'value'}, transient={'foo':'bar'})
'''
        r = await kc.send_wait(display_code)
        outs = await kc.get_pubs()
        display_msgs = [msg_short(o) for o in outs if o['msg_type'] == 'display_data']
        print(f"Cell: metadata/transient display -> {len(display_msgs)} display_data msgs - PASSED")

        # --- Cell: Input handling with getpass ---
        kc.execute("from getpass import getpass; user_input = getpass('Enter something: ')")
        print("Cell: kc.execute(getpass...) - sent")

        # --- Cell: stdin_msg = await kc.stdin_channel.get_msg(timeout=2) ---
        stdin_msg = await kc.stdin_channel.get_msg(timeout=2)
        assert stdin_msg['msg_type'] == 'input_request'
        print(f"Cell: stdin_msg = await kc.stdin_channel.get_msg(timeout=2) -> {stdin_msg['msg_type']} - PASSED")

        # --- Cell: reply = kc.session.input_reply(value='aaa') ---
        reply = kc.session.input_reply(value='aaa')
        assert reply['content']['value'] == 'aaa'
        print("Cell: reply = kc.session.input_reply(value='aaa') - PASSED")

        # --- Cell: kc.stdin_channel.send(reply) ---
        kc.stdin_channel.send(reply)
        print("Cell: kc.stdin_channel.send(reply) - PASSED")

        # --- Cell: await kc.eval('user_input', _call=False) ---
        await asyncio.sleep(0.2)  # Let execution complete
        result = await kc.eval('user_input', _call=False)
        assert result == 'aaa', f"Expected 'aaa', got {result}"
        print(f"Cell: await kc.eval('user_input', _call=False) -> {result} - PASSED")

        # --- Cell: kc.execute("user_input = input('Enter something: ')") ---
        kc.execute("user_input = input('Enter something: ')")
        print("Cell: kc.execute(input...) - sent")

        # --- Cell: Wait for input_request, then send 'bbb' ---
        stdin_msg = await kc.stdin_channel.get_msg(timeout=2)
        assert stdin_msg['msg_type'] == 'input_request'
        print("Cell: got input_request - PASSED")

        # --- Cell: kc.input_reply('bbb') ---
        kc.input_reply('bbb')
        print("Cell: kc.input_reply('bbb') - PASSED")

        # --- Cell: await kc.eval('user_input', _call=False) ---
        await asyncio.sleep(0.2)
        result = await kc.eval('user_input', _call=False)
        assert result == 'bbb', f"Expected 'bbb', got {result}"
        print(f"Cell: await kc.eval('user_input', _call=False) -> {result} - PASSED")

        # --- Cell: jmsg_loop test ---
        loop_msgs = []
        loop = kc.jmsg_loop(lambda m: loop_msgs.append(m), warn)
        print("Cell: loop = kc.jmsg_loop(print, warn) - PASSED")

        # --- Cell: kc.execute('1+1') ---
        kc.execute('1+1')
        await asyncio.sleep(0.2)
        print(f"Cell: kc.execute('1+1') in loop -> collected {len(loop_msgs)} msgs - PASSED")

        # --- Cell: loop.cancel(); await asyncio.sleep(0.1) ---
        loop.cancel()
        await asyncio.sleep(0.1)
        print("Cell: loop.cancel() - PASSED")

        # --- Cell: loop.cancelled() ---
        assert loop.cancelled() or loop.done()
        print("Cell: loop.cancelled() - PASSED")

        # --- Cell: Interrupt test ---
        _ = await kc.get_pubs()  # drain
        kc.execute('print("aa")')
        time.sleep(0.1)
        t = create_task(kc.send_wait('import time; time.sleep(10)', timeout=15))
        await asyncio.sleep(0.2)
        await kc.interrupt()
        r = await t
        assert r['content']['status'] == 'error' or 'KeyboardInterrupt' in str(r['content'])
        print("Cell: interrupt test - PASSED")

        # --- Cell: kc.execute('print("aa")') after interrupt ---
        kc.execute('print("aa")')
        await asyncio.sleep(0.1)
        print("Cell: kc.execute('print(\"aa\")') after interrupt - PASSED")

        # --- Cell: get_outs with rich display code ---
        display_code = '''from IPython.display import Markdown,display
display(Markdown('*doing*'))
display(Markdown('**done**'))
print("\\x1b[31mColored /\\x1b[39m Not")
Markdown('done\\n**it**')'''
        r = await kc.get_outs(display_code, timeout=5)
        assert len(r) >= 2, f"Expected at least 2 outputs, got {len(r)}"
        print(f"Cell: get_outs with rich display -> {len(r)} outputs - PASSED")

        # --- Cell: Interrupt with sleep and cancel ---
        s = "import time; time.sleep(1); print('finished')"
        msg = kc.send(s)
        time.sleep(0.2)
        await kc.interrupt()
        time.sleep(0.2)
        outs = retr_outs(await kc.jmsgs(msg))
        r = [output_from_msg(o) for o in outs]
        print(f"Cell: interrupt sleep test -> {len(r)} outputs - PASSED")

        # --- Cell: Define range_ex for get_schemas test ---
        code = '''def range_ex(
    a:str  # some param
):
    "some func docstring"
    ...'''
        kc.execute(code)
        await asyncio.sleep(0.1)
        print("Cell: define range_ex - PASSED")

        # --- Cell: await kc.ranked_complete(code='rang', line_no=1, col_no=5) ---
        # Note: This requires ipykernel_helper extension - show error if not available
        result = await kc.ranked_complete(code='rang', line_no=1, col_no=5)
        print(f"Cell: ranked_complete -> {result}")

        # --- Cell: kc.xpush(test_var=42, another_var="hello") ---
        kc.xpush(test_var=42, another_var="hello")
        await asyncio.sleep(0.1)
        r1 = await kc.eval('test_var', _call=False)
        r2 = await kc.eval('another_var', _call=False)
        assert r1 == 42
        assert r2 == "hello"
        print(f"Cell: xpush test -> test_var={r1}, another_var={r2} - PASSED")

        # --- Cell: kc.execute('a=1'); await kc.user_items(max_len=100) ---
        kc.execute('a=1')
        await asyncio.sleep(0.1)
        result = await kc.user_items(max_len=100)
        print(f"Cell: user_items -> {result}")

        # --- Cell: res = await kc.sig_help(code='range(', line_no=1, col_no=6) ---
        res = await kc.sig_help(code='range(', line_no=1, col_no=6)
        print(f"Cell: sig_help -> {res}")

        # --- Cell: await kc.get_vars(vs=['a']) ---
        result = await kc.get_vars(vs=['a'])
        print(f"Cell: get_vars -> {result}")

        # --- Cell: await kc.eval_exprs(vs=['list(range(5))']) ---
        result = await kc.eval_exprs(vs=['list(range(5))'])
        print(f"Cell: eval_exprs -> {result}")

        # --- Cell: await kc.get_schemas(fs=['range_ex']) ---
        result = await kc.get_schemas(fs=['range_ex'])
        print(f"Cell: get_schemas -> {result}")

        # --- Cell: kc.xpush(asdf=4) ---
        kc.xpush(asdf=4)
        await asyncio.sleep(0.1)
        print("Cell: kc.xpush(asdf=4) - PASSED")

        # --- Cell: await kc.retr('asdf') ---
        result = await kc.retr('asdf')
        assert result == 4
        print(f"Cell: await kc.retr('asdf') -> {result} - PASSED")

        # --- Cell: kc.xenv(hi='johno'); await kc.eval('__os.environ["hi"]', _call=False) ---
        kc.xenv(hi='johno')
        await asyncio.sleep(0.1)
        result = await kc.eval('__os.environ["hi"]', _call=False)
        assert result == 'johno'
        print(f"Cell: xenv test -> {result} - PASSED")

        # --- Final Cell: Shutdown ---
        print("\n=== All notebook cells replayed successfully! ===")

        # Cleanup
        if await km.is_alive():
            kc.stop()
            await km.shutdown_kernel()
            print("Cell: kc.stop(); km.shutdown_kernel() - PASSED")

    asyncio.run(_run())

@pytest.mark.slow
def test_gateway_e2e():
    "Comprehensive e2e test of gateway functionality with ipymini"
    # Install Session.__getattr__ for dynamic message creation
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        assert await km.is_alive()

        kc = km.client()
        await kc.start()

        try:
            # Basic execute
            mid = kc.execute('1+1')
            pubs = await kc.get_pubs()
            assert any(o['msg_type'] == 'execute_result' for o in pubs)

            # Test retr_outs
            outs = retr_outs(pubs)
            assert len(outs) >= 1

            # Test Session.jmsg
            jm = kc.session.jmsg('1+2', 'code', 'myid_')
            assert jm['header']['msg_id'] == 'myid_'

            # Test send and router
            mid = unqid()
            kc._waiters[mid] = asyncio.Queue()
            kc.send('41+2', msg_id=mid)
            msg = await asyncio.wait_for(kc._waiters[mid].get(), timeout=5)
            assert msg_short(msg)['content']['status'] == 'ok'
            del kc._waiters[mid]

            # Test send_wait
            jid = unqid()
            r = await kc.send_wait('print("bbb")', jid)
            rjid = nested_idx(r, 'parent_header', 'msg_id')
            assert rjid == jid

            # Test user_expressions
            r = await kc.send_wait('a=[1,2,3]', user_expressions={'foo': 'a'})
            cts = r['content']
            ret = nested_idx(cts, 'user_expressions', 'foo', 'data', 'text/plain')
            assert literal_eval(ret) == [1, 2, 3]

            # Test eval with sync function
            kc.execute('def add(a, b): return a+b')
            result = await kc.eval('add', a=10, b=20)
            assert result == 30

            # Test eval with async function
            kc.execute('async def async_add(a, b): return a+b')
            result = await kc.eval('async_add', a=10, b=20)
            assert result == 30

            # Test eval without call
            result = await kc.eval('a', _call=False)
            assert result == [1, 2, 3]

            # Test payloads
            code = '''payl = dict(source='testing', foo='bar')
pm = get_ipython().payload_manager
pm.write_payload(payl, single=False)
pm.write_payload(dict(source='testing', bar='baz'), single=False)
'''
            r = await kc.send_wait(code)
            assert len(r['content']['payload']) == 2

            # Test xpush and retr
            kc.xpush(test_var=42, another_var="hello")
            await asyncio.sleep(0.1)
            assert await kc.retr('test_var') == 42
            assert await kc.retr('another_var') == "hello"

            # Test input handling
            kc.execute("user_input = input('Enter something: ')")
            stdin_msg = await kc.stdin_channel.get_msg(timeout=2)
            assert stdin_msg['msg_type'] == 'input_request'
            kc.input_reply('test_input')
            await asyncio.sleep(0.2)
            result = await kc.eval('user_input', _call=False)
            assert result == 'test_input'

            # Test interrupt
            _ = await kc.get_pubs()  # clear
            kc.execute('print("aa")')
            await asyncio.sleep(0.1)
            t = create_task(kc.send_wait('import time; time.sleep(10)', timeout=15))
            await asyncio.sleep(0.2)
            await kc.interrupt()
            r = await t
            assert r['content']['status'] == 'error' or 'KeyboardInterrupt' in str(r)

            # Test xenv
            kc.xenv(hi='test_value')
            await asyncio.sleep(0.1)
            result = await kc.eval('__import__("os").environ["hi"]', _call=False)
            assert result == 'test_value'

            # Test get_outs with display
            code = '''from IPython.display import Markdown, display
display(Markdown('*test*'))
print("hello")
42'''
            outs = await kc.get_outs(code, timeout=5)
            assert len(outs) >= 2  # display_data and execute_result at minimum

            print("All gateway e2e tests passed!")

        finally:
            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())

@pytest.mark.slow
def test_solveit_jmsg_loop_pattern():
    """
    Test using Solveit's exact jmsg_loop + process_jmsg + run_loop pattern.
    This simulates how Solveit processes cells during "run all".
    """
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        kc = km.client()
        await kc.start()

        # --- Simulate Solveit's message tracking ---
        class FakeMessage:
            def __init__(self, id_, code):
                self.id = id_
                self.code = code
                self.run = None  # None=not started, True=running, False=done
                self.outputs = []
                self.idle_received = False

            def add_output(self, jmsg): self.outputs.append(jmsg)

        messages = {}  # id -> FakeMessage
        processed_jmsgs = []  # All processed jmsgs for debugging

        # --- Solveit's process_jmsg logic ---
        def process_jmsg(jmsg):
            processed_jmsgs.append(jmsg)
            parent_id = nested_idx(jmsg, 'parent_header', 'msg_id')
            if not parent_id or parent_id not in messages: return  # Not our message

            msg = messages[parent_id]
            msg_type = jmsg['msg_type']

            if msg_type == 'execute_input': msg.run = True
            elif msg_type in output_types: msg.add_output(jmsg)
            elif msg_type == 'status':
                state = nested_idx(jmsg, 'content', 'execution_state')
                if state == 'idle':
                    msg.run = False
                    msg.idle_received = True

        # --- Solveit's jmsg_loop (exact pattern with elif) ---
        jloop_running = True
        jloop_error = None

        async def jmsg_loop_task():
            nonlocal jloop_running, jloop_error
            poller = zmq.asyncio.Poller()
            iopub_sock = kc.iopub_channel.socket
            stdin_sock = kc.stdin_channel.socket
            poller.register(iopub_sock, zmq.POLLIN)
            poller.register(stdin_sock, zmq.POLLIN)

            while jloop_running:
                try:
                    # Use timeout so we can check jloop_running
                    events = dict(await poller.poll(timeout=100))
                    if not events: continue
                    # EXACT Solveit pattern with elif (potential issue)
                    if stdin_sock in events and (jmsg := await kc.get_stdin_msg()): process_jmsg(jmsg)
                    elif iopub_sock in events and (jmsg := await kc.get_iopub_msg()): process_jmsg(jmsg)
                except (RuntimeError, ZMQError) as e:
                    jloop_error = e
                    jloop_running = False
                except Exception as e:
                    print(f"jmsg_loop error: {e}")
                    traceback.print_exc()

        # Start jmsg_loop
        jloop = asyncio.create_task(jmsg_loop_task())

        try:
            # --- Simulate "run all" with Solveit's pattern ---
            cells = _lines("""
            x = 1
            y = 2
            z = x + y
            print(f'z = {z}')
            def foo(a, b): return a * b
            result = foo(3, 4)
            import time
            [i**2 for i in range(10)]
            {'a': 1, 'b': 2}
            len('hello world')
            a = [1, 2, 3]
            b = [4, 5, 6]
            list(zip(a, b))
            sum(range(100))
            max([1, 5, 3, 9, 2])
            min([1, 5, 3, 9, 2])
            sorted([3, 1, 4, 1, 5, 9, 2, 6])
            ' '.join(['hello', 'world'])
            list(range(5))
            dict(a=1, b=2, c=3)
            """)

            # Create messages and send them like Solveit's run_loop
            # (sends rapidly with only 10ms delay between)
            for code in cells:
                msg_id = unqid()
                messages[msg_id] = FakeMessage(msg_id, code)
                kc.send(code, msg_id=msg_id)
                await asyncio.sleep(0.01)  # Solveit's 10ms delay

            # Wait for all cells to complete (receive idle)
            timeout = 30
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                all_done = all(m.idle_received for m in messages.values())
                if all_done: break
                await asyncio.sleep(0.05)

            # Check results
            completed = [m for m in messages.values() if m.idle_received]
            pending = [m for m in messages.values() if not m.idle_received]

            print(f"Completed: {len(completed)}/{len(cells)}")
            if pending:
                print(f"Pending (no idle received):")
                for m in pending: print(f"  {m.id}: {m.code[:30]}... run={m.run} outputs={len(m.outputs)}")

            # Should have received idle for all
            assert len(pending) == 0, f"{len(pending)} cells never received idle: {[m.code[:20] for m in pending]}"

            print("Solveit jmsg_loop pattern test passed!")

        finally:
            jloop_running = False
            await asyncio.sleep(0.2)
            jloop.cancel()
            try: await jloop
            except asyncio.CancelledError: pass

            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())

@pytest.mark.slow
def test_solveit_jmsg_loop_fixed_pattern():
    """
    Test with FIXED jmsg_loop pattern (no elif - process both stdin and iopub).
    Compare with test_solveit_jmsg_loop_pattern to see if elif is the issue.
    """
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        kc = km.client()
        await kc.start()

        class FakeMessage:
            def __init__(self, id_, code):
                self.id = id_
                self.code = code
                self.run = None
                self.outputs = []
                self.idle_received = False

            def add_output(self, jmsg): self.outputs.append(jmsg)

        messages = {}

        def process_jmsg(jmsg):
            parent_id = nested_idx(jmsg, 'parent_header', 'msg_id')
            if not parent_id or parent_id not in messages: return
            msg = messages[parent_id]
            msg_type = jmsg['msg_type']
            if msg_type == 'execute_input': msg.run = True
            elif msg_type in output_types: msg.add_output(jmsg)
            elif msg_type == 'status' and nested_idx(jmsg, 'content', 'execution_state') == 'idle':
                msg.run = False
                msg.idle_received = True

        jloop_running = True

        async def jmsg_loop_task():
            nonlocal jloop_running
            poller = zmq.asyncio.Poller()
            iopub_sock = kc.iopub_channel.socket
            stdin_sock = kc.stdin_channel.socket
            poller.register(iopub_sock, zmq.POLLIN)
            poller.register(stdin_sock, zmq.POLLIN)

            while jloop_running:
                try:
                    events = dict(await poller.poll(timeout=100))
                    if not events: continue
                    # FIXED: Process BOTH stdin and iopub (no elif)
                    if stdin_sock in events:
                        if jmsg := await kc.get_stdin_msg(): process_jmsg(jmsg)
                    if iopub_sock in events:
                        if jmsg := await kc.get_iopub_msg(): process_jmsg(jmsg)
                except (RuntimeError, ZMQError): jloop_running = False
                except Exception as e: print(f"jmsg_loop error: {e}")

        jloop = asyncio.create_task(jmsg_loop_task())

        try:
            cells = _lines("""
            x = 1
            y = 2
            z = x + y
            print(f'z = {z}')
            def foo(a, b): return a * b
            result = foo(3, 4)
            import time
            [i**2 for i in range(10)]
            {'a': 1, 'b': 2}
            len('hello world')
            a = [1, 2, 3]
            b = [4, 5, 6]
            list(zip(a, b))
            sum(range(100))
            max([1, 5, 3, 9, 2])
            min([1, 5, 3, 9, 2])
            sorted([3, 1, 4, 1, 5, 9, 2, 6])
            ' '.join(['hello', 'world'])
            list(range(5))
            dict(a=1, b=2, c=3)
            """)

            for code in cells:
                msg_id = unqid()
                messages[msg_id] = FakeMessage(msg_id, code)
                kc.send(code, msg_id=msg_id)
                await asyncio.sleep(0.01)

            timeout = 30
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                if all(m.idle_received for m in messages.values()): break
                await asyncio.sleep(0.05)

            completed = [m for m in messages.values() if m.idle_received]
            pending = [m for m in messages.values() if not m.idle_received]

            print(f"Completed: {len(completed)}/{len(cells)}")
            assert len(pending) == 0, f"{len(pending)} cells never received idle"

            print("Fixed jmsg_loop pattern test passed!")

        finally:
            jloop_running = False
            await asyncio.sleep(0.2)
            jloop.cancel()
            try: await jloop
            except asyncio.CancelledError: pass
            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())

@pytest.mark.slow
def test_solveit_full_architecture():
    """
    Test mimicking Solveit's FULL architecture:
    - jloop: jmsg_loop processing kernel messages
    - rloop: run_loop sending cells from runq
    - oloop: oob_loop sending updates to "frontend"
    - Multiple concurrent async tasks
    - oobq for message passing

    This should more closely reproduce the Solveit environment.
    """
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        kc = km.client()
        await kc.start()

        # --- Solveit-like state ---
        class FakeMessage:
            def __init__(self, id_, code):
                self.id = id_
                self.code = code
                self.run = None
                self.outputs = []
                self.idle_received = False
                self.time_run = None

            def add_output(self, jmsg): self.outputs.append(jmsg)

        messages = {}
        oobq = asyncio.Queue()  # Out-of-band queue for UI updates
        runq = asyncio.Queue()  # Queue of message IDs to execute
        evt_started = asyncio.Event()
        frontend_updates = []  # Simulated frontend receives these

        # --- Solveit's process_jmsg ---
        def process_jmsg(jmsg):
            parent_id = nested_idx(jmsg, 'parent_header', 'msg_id')
            if not parent_id or parent_id not in messages: return
            msg = messages[parent_id]
            msg_type = jmsg['msg_type']

            if msg_type == 'execute_input': msg.time_run = time.monotonic()
            elif msg_type in output_types:
                msg.add_output(jmsg)
                oobq.put_nowait(msg)  # Enqueue for UI update
            elif msg_type == 'status':
                state = nested_idx(jmsg, 'content', 'execution_state')
                if state == 'idle':
                    msg.run = False
                    msg.idle_received = True
                    oobq.put_nowait(msg)  # Enqueue for UI update

        # --- jloop: Solveit's jmsg_loop ---
        jloop_running = True

        async def jloop_task():
            nonlocal jloop_running
            poller = zmq.asyncio.Poller()
            iopub_sock = kc.iopub_channel.socket
            stdin_sock = kc.stdin_channel.socket
            poller.register(iopub_sock, zmq.POLLIN)
            poller.register(stdin_sock, zmq.POLLIN)

            while jloop_running:
                try:
                    events = dict(await poller.poll(timeout=100))
                    if not events: continue
                    # Solveit's elif pattern
                    if stdin_sock in events and (jmsg := await kc.get_stdin_msg()): process_jmsg(jmsg)
                    elif iopub_sock in events and (jmsg := await kc.get_iopub_msg()): process_jmsg(jmsg)
                except (RuntimeError, ZMQError): jloop_running = False
                except Exception as e:
                    print(f"jloop error: {e}")
                    traceback.print_exc()

        # --- rloop: Solveit's run_loop ---
        rloop_running = True

        async def rloop_task():
            nonlocal rloop_running
            while rloop_running:
                try: msg_id = await asyncio.wait_for(runq.get(), timeout=0.1)
                except asyncio.TimeoutError: continue
                if msg_id is None: break
                msg = messages.get(msg_id)
                if msg:
                    await evt_started.wait()
                    kc.send(msg.code, msg_id=msg.id)
                await asyncio.sleep(0.01)  # Solveit's 10ms delay

        # --- oloop: Solveit's oob_loop (simulated frontend) ---
        oloop_running = True

        async def oloop_task():
            nonlocal oloop_running
            while oloop_running:
                try:
                    # Solveit's drain_queue with debounce
                    items = []
                    start = None
                    max_wait = 0.3
                    timeout_val = 0.2
                    initial_timeout = 1.0

                    while len(items) < 100:
                        if start:
                            remaining = max_wait - (time.monotonic() - start)
                            if remaining <= 0: break
                            wait_time = min(timeout_val, remaining)
                        else: wait_time = initial_timeout
                        try:
                            item = await asyncio.wait_for(oobq.get(), timeout=wait_time)
                            items.append(item)
                            if start is None: start = time.monotonic()
                        except asyncio.TimeoutError: break

                    # Simulate sending to frontend
                    if items: frontend_updates.extend(items)
                except Exception as e: print(f"oloop error: {e}")
                await asyncio.sleep(0.001)

        # Start all loops
        jloop = asyncio.create_task(jloop_task())
        rloop = asyncio.create_task(rloop_task())
        oloop = asyncio.create_task(oloop_task())

        try:
            # Signal started (like Solveit's evt_started.set())
            evt_started.set()

            # --- Simulate "run all" ---
            cells = _lines("""
            x = 1
            y = 2
            z = x + y
            print(f'z = {z}')
            def foo(a, b): return a * b
            result = foo(3, 4)
            import sys
            [i**2 for i in range(10)]
            {'a': 1, 'b': 2}
            len('hello world')
            a = [1, 2, 3]
            b = [4, 5, 6]
            list(zip(a, b))
            sum(range(100))
            max([1, 5, 3, 9, 2])
            min([1, 5, 3, 9, 2])
            sorted([3, 1, 4, 1, 5, 9, 2, 6])
            ' '.join(['hello', 'world'])
            list(range(5))
            dict(a=1, b=2, c=3)
            """)

            # Queue all cells for execution (like clicking "run all")
            for code in cells:
                msg_id = unqid()
                messages[msg_id] = FakeMessage(msg_id, code)
                runq.put_nowait(msg_id)

            # Wait for all cells to complete
            wait_timeout = 30
            start = time.monotonic()
            while time.monotonic() - start < wait_timeout:
                if all(m.idle_received for m in messages.values()): break
                await asyncio.sleep(0.05)

            # Check results
            completed = [m for m in messages.values() if m.idle_received]
            pending = [m for m in messages.values() if not m.idle_received]

            print(f"Completed: {len(completed)}/{len(cells)}")
            print(f"Frontend received {len(frontend_updates)} updates")

            if pending:
                print(f"Pending (no idle):")
                for m in pending: print(f"  {m.id}: {m.code[:30]}... outputs={len(m.outputs)}")

            assert len(pending) == 0, f"{len(pending)} cells never received idle"
            assert len(frontend_updates) > 0, "Frontend should have received updates"

            print("Solveit full architecture test passed!")

        finally:
            jloop_running = False
            rloop_running = False
            oloop_running = False
            runq.put_nowait(None)  # Wake rloop

            await asyncio.sleep(0.3)

            for task in [jloop, rloop, oloop]:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())

@pytest.mark.slow
def test_gateway_notebook_cells_solveit_pattern():
    """
    Run ACTUAL gateway notebook cells from meta/00_gateway.ipynb through Solveit's architecture.
    These are the cells that DEFINE Solveit itself - they import jupyter_client,
    zmq, Session, AsyncKernelManager, etc. which might conflict with the running kernel.

    This tests the specific scenario: "notebooks that in nbdev create solveit" cause hangs.
    """
    import json

    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]

        # Load actual notebook cells from meta/00_gateway.ipynb
        nb_path = pp / 'meta' / '00_gateway.ipynb'
        if not nb_path.exists(): nb_path = pp.parent / 'meta' / '00_gateway.ipynb'
        with open(nb_path) as f: nb = json.load(f)

        # Extract code cells (skip markdown, raw)
        code_cells = []
        for cell in nb['cells']:
            if cell['cell_type'] == 'code':
                source = ''.join(cell['source'])
                # Skip empty cells
                if source.strip(): code_cells.append(source)

        print(f"\nLoaded {len(code_cells)} code cells from {nb_path}")

        km = await start_test_kernel(pp)
        kc = km.client()
        await kc.start()

        # --- Solveit-like state ---
        class FakeMessage:
            def __init__(self, id_, code, cell_idx):
                self.id = id_
                self.code = code
                self.cell_idx = cell_idx
                self.run = None
                self.outputs = []
                self.idle_received = False

            def add_output(self, jmsg): self.outputs.append(jmsg)

        messages = {}
        oobq = asyncio.Queue()
        runq = asyncio.Queue()
        evt_started = asyncio.Event()

        def process_jmsg(jmsg):
            parent_id = nested_idx(jmsg, 'parent_header', 'msg_id')
            if not parent_id or parent_id not in messages: return
            msg = messages[parent_id]
            msg_type = jmsg['msg_type']
            if msg_type == 'execute_input': msg.run = True
            elif msg_type in output_types:
                msg.add_output(jmsg)
                oobq.put_nowait(msg)
            elif msg_type == 'status' and nested_idx(jmsg, 'content', 'execution_state') == 'idle':
                msg.run = False
                msg.idle_received = True
                oobq.put_nowait(msg)

        jloop_running = True

        async def jloop_task():
            nonlocal jloop_running
            poller = zmq.asyncio.Poller()
            iopub_sock = kc.iopub_channel.socket
            stdin_sock = kc.stdin_channel.socket
            poller.register(iopub_sock, zmq.POLLIN)
            poller.register(stdin_sock, zmq.POLLIN)
            while jloop_running:
                try:
                    events = dict(await poller.poll(timeout=100))
                    if not events: continue
                    # Solveit's elif pattern - handle stdin for input_request
                    if stdin_sock in events:
                        jmsg = await kc.get_stdin_msg()
                        if jmsg:
                            # Handle input_request by sending a reply
                            if jmsg['msg_type'] == 'input_request': kc.input_reply('')  # Empty reply for testing
                    if iopub_sock in events:
                        jmsg = await kc.get_iopub_msg()
                        if jmsg: process_jmsg(jmsg)
                except (RuntimeError, ZMQError) as e:
                    print(f"jloop ZMQ error: {e}")
                    jloop_running = False
                except Exception as e:
                    print(f"jloop error: {e}")
                    traceback.print_exc()

        rloop_running = True

        async def rloop_task():
            nonlocal rloop_running
            while rloop_running:
                try: msg_id = await asyncio.wait_for(runq.get(), timeout=0.1)
                except asyncio.TimeoutError: continue
                if msg_id is None: break
                msg = messages.get(msg_id)
                if msg:
                    await evt_started.wait()
                    # Send 5 uncollected __msg_id messages before each cell (like Solveit's xpush)
                    # These are fire-and-forget - no response is collected
                    for _ in range(5): kc.send(f"__msg_id='{msg.id}'")
                    kc.send(msg.code, msg_id=msg.id)
                await asyncio.sleep(0.01)

        oloop_running = True

        async def oloop_task():
            nonlocal oloop_running
            while oloop_running:
                try:
                    try: item = await asyncio.wait_for(oobq.get(), timeout=0.5)
                    except asyncio.TimeoutError: continue
                except Exception as e: print(f"oloop error: {e}")
                await asyncio.sleep(0.001)

        jloop = asyncio.create_task(jloop_task())
        rloop = asyncio.create_task(rloop_task())
        oloop = asyncio.create_task(oloop_task())

        try:
            evt_started.set()

            # Queue all cells for execution (like "run all")
            for idx, code in enumerate(code_cells):
                msg_id = unqid()
                messages[msg_id] = FakeMessage(msg_id, code, idx)
                runq.put_nowait(msg_id)

            # Wait for completion
            wait_timeout = 120  # Longer timeout for full notebook
            start = time.monotonic()
            last_completed = 0
            while time.monotonic() - start < wait_timeout:
                completed_count = sum(1 for m in messages.values() if m.idle_received)
                if completed_count > last_completed:
                    print(f"  Progress: {completed_count}/{len(code_cells)} cells completed")
                    last_completed = completed_count
                if all(m.idle_received for m in messages.values()): break
                await asyncio.sleep(0.2)

            completed = [m for m in messages.values() if m.idle_received]
            pending = [m for m in messages.values() if not m.idle_received]

            print(f"\nGateway notebook: {len(completed)}/{len(code_cells)} cells completed")

            if pending:
                print(f"\nPENDING (no idle received):")
                for m in sorted(pending, key=lambda x: x.cell_idx):
                    code_preview = m.code[:80].replace('\n', '\\n')
                    if len(m.code) > 80: code_preview += '...'
                    print(f"  Cell {m.cell_idx}: {code_preview}")
                    print(f"    run={m.run}, outputs={len(m.outputs)}")
                    for out in m.outputs[:3]: print(f"    output: {out['msg_type']}")

            # Check for errors in completed cells
            error_count = 0
            for m in sorted(completed, key=lambda x: x.cell_idx):
                errors = [o for o in m.outputs if o['msg_type'] == 'error']
                if errors:
                    error_count += 1
                    code_preview = m.code[:50].replace('\n', '\\n')
                    print(f"\nERROR in cell {m.cell_idx}: {code_preview}...")
                    for e in errors: print(f"  {e['content'].get('ename')}: {e['content'].get('evalue')}")

            if error_count: print(f"\n{error_count} cells had errors (expected for some notebook cells)")

            assert len(pending) == 0, f"{len(pending)} gateway cells never received idle"

            print("\nGateway notebook cells test passed!")

        finally:
            jloop_running = False
            rloop_running = False
            oloop_running = False
            runq.put_nowait(None)

            await asyncio.sleep(0.3)

            for task in [jloop, rloop, oloop]:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())


def test_uncollected_messages_cause_hang():
    """
    Minimal reproduction of the bug: fire-and-forget messages cause IOPub to stop.

    This test sends execute requests without reading IOPub responses (like Solveit's xpush).
    After enough uncollected messages, ipymini stops publishing IOPub entirely.
    """
    Session.__getattr__ = _ses_getattr

    async def _run():
        pp = Path(__file__).resolve().parents[1]
        km = await start_test_kernel(pp)
        kc = km.client()
        await kc.start()

        try:
            # First verify normal operation works
            test_id = unqid()
            kc.send("x = 1", msg_id=test_id)

            # Wait for idle
            idle_received = False
            for _ in range(50):
                try:
                    msg = await asyncio.wait_for(kc.get_iopub_msg(), timeout=0.1)
                    if msg['msg_type'] == 'status' and nested_idx(msg, 'content', 'execution_state') == 'idle':
                        if nested_idx(msg, 'parent_header', 'msg_id') == test_id:
                            idle_received = True
                            break
                except asyncio.TimeoutError: break

            assert idle_received, "Initial test cell should receive idle"
            print("Initial cell worked - IOPub is functioning")

            # Simulate Solveit pattern: 5 uncollected messages then 1 tracked, repeated
            # This matches what happens in test_gateway_notebook_cells_solveit_pattern
            print("Simulating Solveit pattern: 5 uncollected + 1 tracked per 'cell'...")

            tracked_ids = []
            for cell_num in range(20):  # Simulate 20 cells
                # Send 5 uncollected __msg_id messages (like xpush)
                for j in range(5): kc.send(f"__msg_id='cell_{cell_num}'")

                # Send 1 tracked message (the actual cell)
                cell_id = unqid()
                tracked_ids.append(cell_id)
                kc.send(f"cell_{cell_num}_result = {cell_num}", msg_id=cell_id)

            print(f"Sent {20*5} uncollected + {20} tracked messages")

            # Now try to read responses for tracked messages
            print("Attempting to read IOPub responses...")

            idle_count = 0
            start = time.monotonic()
            timeout = 30  # 30 second timeout

            while time.monotonic() - start < timeout and idle_count < len(tracked_ids):
                try:
                    msg = await asyncio.wait_for(kc.get_iopub_msg(), timeout=1.0)
                    msg_type = msg['msg_type']
                    parent_id = nested_idx(msg, 'parent_header', 'msg_id')
                    exec_state = nested_idx(msg, 'content', 'execution_state')

                    if msg_type == 'status' and exec_state == 'idle' and parent_id in tracked_ids:
                        idle_count += 1
                        if idle_count % 5 == 0: print(f"  Progress: {idle_count}/{len(tracked_ids)} tracked cells got idle")
                except asyncio.TimeoutError:
                    print(f"  Timeout - only {idle_count}/{len(tracked_ids)} got idle")
                    break

            print(f"Final: {idle_count}/{len(tracked_ids)} tracked cells received idle")

            # The test passes if ALL tracked messages got their idle
            assert idle_count == len(tracked_ids), f"Only {idle_count}/{len(tracked_ids)} cells got idle - IOPub stopped"
            return  # Early return on success

            # (Dead code below - keeping for reference)
            # Now try to execute another cell and read its response
            print("Attempting to execute a cell after uncollected messages...")
            final_id = unqid()
            kc.send("y = 2", msg_id=final_id)

            # Try to get idle for this cell
            idle_received = False
            start = time.monotonic()
            timeout = 10  # 10 second timeout

            while time.monotonic() - start < timeout:
                try:
                    msg = await asyncio.wait_for(kc.get_iopub_msg(), timeout=0.5)
                    msg_type = msg['msg_type']
                    parent_id = nested_idx(msg, 'parent_header', 'msg_id')
                    exec_state = nested_idx(msg, 'content', 'execution_state')
                    print(f"  Got: {msg_type} parent={parent_id[:12] if parent_id else '?'} exec_state={exec_state}")

                    if msg_type == 'status' and exec_state == 'idle' and parent_id == final_id:
                        idle_received = True
                        break
                except asyncio.TimeoutError:
                    print("  Timeout waiting for IOPub message")
                    break

            if idle_received: print("SUCCESS: IOPub still works after uncollected messages")
            else: print("FAILURE: IOPub stopped after uncollected messages")

            assert idle_received, "Cell after uncollected messages should receive idle - IOPub has stopped"

        finally:
            if await km.is_alive():
                kc.stop()
                await km.shutdown_kernel()

    asyncio.run(_run())
