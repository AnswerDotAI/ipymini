import asyncio
from ..aclient import *


async def test_subshell_shares_namespace_and_history_without_blocking_parent():
    async with mini_kernel() as (_, kc):
        await kc.exec_ok('import threading\nfrom IPython import get_ipython as imported_get_ipython\nshared = 123\ngate = threading.Event()')
        sid = (await kc.ctl.create_subshell())['content']['subshell_id']
        try:
            reply, outputs = await kc.exec_ok('shared += 1', subshell_id=sid)
            assert reply['content']['execution_count'] == 1 and reply['parent_header']['subshell_id'] == sid
            assert iopub_msgs(outputs, 'execute_input')[0]['parent_header']['subshell_id'] == sid
            assert await kc.eval_expr('shared') == 124
            await kc.exec_ok('assert shared == 124', subshell_id='')
            await kc.cmd.complete(code='shared', cursor_pos=6, subshell_id='named-completion')
            assert {sid, 'named-completion'} <= set((await kc.ctl.list_subshell())['content']['subshell_id'])
            await kc.ctl.delete_subshell(subshell_id='named-completion')
            probe = '''import IPython, builtins
ip = get_ipython()
assert IPython.get_ipython() is builtins.get_ipython() is imported_get_ipython() is ip
assert In is ip.history_manager.input_hist_parsed
assert Out is ip.history_manager.output_hist
assert any('shared = 123' in s for s in In)
'''
            for lane in (None, sid): await kc.exec_ok(probe, subshell_id=lane, store_history=False)
            waiting = kc.reply("print('parent-waiting', flush=True); assert gate.wait(5); print(shared)")
            await wait_iopub(kc, lambda m: m['msg_type'] == 'stream' and 'parent-waiting' in m['content']['text'])
            extra = (await kc.ctl.create_subshell())['content']['subshell_id']
            parent, child = await gather_outputs(kc, waiting, kc.reply('gate.set()', subshell_id=sid))
            assert parent[0]['content']['status'] == child[0]['content']['status'] == 'ok'
            assert '124' in stream_text(parent[1])

            task_id = kc.new_msg_id()
            await kc.exec_ok('''import asyncio
child_gate = asyncio.Event()
async def child_later():
    await child_gate.wait()
    print('child-task-output', flush=True)
child_task = asyncio.create_task(child_later())
''', subshell_id=sid, msg_id=task_id)
            await kc.exec_ok('child_gate.set(); await child_task', subshell_id=sid)
            await wait_iopub(kc, lambda m: parent_id(m) == task_id and m['msg_type'] == 'stream' and 'child-task-output' in m['content']['text'])
            try:
                for failing, independent in ((None, sid), (sid, None), (sid, extra)):
                    codes = [("import asyncio; await asyncio.sleep(0.1); raise ValueError()", failing), ('1+1', failing),
                        ('2+2', failing), ('3+3', independent)]
                    results = await gather_outputs(kc, *(kc.reply(code, subshell_id=lane) for code, lane in codes))
                    assert [r['content']['status'] for r, _ in results] == ['error', 'aborted', 'aborted', 'ok']
                    assert [r['parent_header'].get('subshell_id') for r, _ in results] == [lane for _, lane in codes]
                    for _, messages in results:
                        assert messages[-1]['msg_type'] == 'status' and messages[-1]['content']['execution_state'] == 'idle'
                    await kc.exec_ok('1+1', subshell_id=failing)
            finally: await kc.ctl.delete_subshell(subshell_id=extra)
        finally: await kc.ctl.delete_subshell(subshell_id=sid)


async def test_subshell_routes_only_its_client_session():
    async with mini_kernel() as (_, kc):
        async with clone(kc) as other:
            caller = kc.reply('''import asyncio
loop = asyncio.get_running_loop()
done = asyncio.Event()
with get_ipython().kernel.subshell():
    print('subshell-ready', flush=True)
    await done.wait()
    assert 'hijack_flag' not in globals()
''')
            await wait_iopub(kc, lambda m: m['msg_type'] == 'stream' and 'subshell-ready' in m['content']['text'])
            queued = other.reply('hijack_flag = True')
            await other.cmd.kernel_info()
            released = kc.reply('loop.call_soon_threadsafe(done.set)')
            for reply, _ in await gather_outputs(kc, caller, released, queued): assert reply['content']['status'] == 'ok'
            assert await kc.eval_expr('hijack_flag') is True


async def test_deleting_busy_subshell_allows_recreation():
    async with mini_kernel() as (km, kc):
        await kc.exec_ok('import threading\ncleanup_gate = threading.Event()')
        sid = (await kc.ctl.create_subshell())['content']['subshell_id']
        running = kc.reply("try:\n    print('running', flush=True)\n    while True: pass\nfinally:\n    print('cleaning-up', flush=True)\n    cleanup_gate.wait(5)", subshell_id=sid)
        await wait_iopub(kc, lambda m: m['msg_type'] == 'stream' and 'running' in m['content']['text'])
        deleting = asyncio.create_task(kc.ctl.delete_subshell(subshell_id=sid))
        await wait_iopub(kc, lambda m: m['msg_type'] == 'stream' and 'cleaning-up' in m['content']['text'])
        late = kc.reply('recreated = True', subshell_id=sid)
        await kc.exec_ok('cleanup_gate.set()')
        assert (await deleting)['content']['status'] == 'ok'
        old, new = await gather_outputs(kc, running, late)
        assert old[0]['content']['status'] == 'error' and new[0]['content']['status'] == 'ok'
        assert sid in (await kc.ctl.list_subshell())['content']['subshell_id']
        kc.execute('while True: pass', subshell_id=sid)
        await wait_status(kc, 'busy')
        assert (await kc.ctl.shutdown(restart=False))['content']['status'] == 'ok'
        km.provisioner.process.wait(timeout=5)
