import os, signal, pytest
from ..aclient import *
from ..kernel_utils import kernel_pid


async def _interrupted(kc, cell):
    reply = await cell
    assert reply['content']['ename'] == 'KeyboardInterrupt'
    outputs = await kc.iopub_drain(parent_id(reply))
    assert iopub_msgs(outputs, 'error')[0]['content']['ename'] == 'KeyboardInterrupt'
    assert outputs[-1]['msg_type'] == 'status' and outputs[-1]['content']['execution_state'] == 'idle'


@pytest.mark.slow
async def test_interrupts_stop_user_code_and_allow_recovery():
    async with mini_kernel() as (km, kc):
        cases = [('import time; time.sleep(30)', 'manager'), ('import asyncio; await asyncio.Event().wait()', 'signal'),
            ('import asyncio; await asyncio.Event().wait()', 'signal'), ('while True: pass', 'control')]
        for code, route in cases:
            cell = kc.reply(code)
            await wait_status(kc, 'busy')
            if route == 'signal': os.kill(kernel_pid(km), signal.SIGINT)
            elif route == 'manager': await km.interrupt_kernel()
            else: await kc.interrupt()
            await _interrupted(kc, cell)
            await kc.exec_ok('41+1')

        sid = (await kc.ctl.create_subshell())['content']['subshell_id']
        try:
            parent = kc.reply('import asyncio; await asyncio.Event().wait()')
            child = kc.reply("import time; print('child-sleeping', flush=True); time.sleep(0.7)", subshell_id=sid)
            await wait_iopub(kc, lambda m: m['msg_type'] == 'stream' and 'child-sleeping' in m['content']['text'])
            await kc.interrupt()
            for reply, outputs in await gather_outputs(kc, parent, child):
                assert reply['content']['ename'] == 'KeyboardInterrupt'
                assert outputs[-1]['msg_type'] == 'status' and outputs[-1]['content']['execution_state'] == 'idle'
        finally: await kc.ctl.delete_subshell(subshell_id=sid)

        cell = kc.reply('''import time
print('looping', flush=True)
try:
    while True: time.sleep(1)
except KeyboardInterrupt: print('survived', flush=True)
while True: time.sleep(1)
''')
        def printed(text): return lambda m: m['msg_type'] == 'stream' and text in m['content']['text']
        await wait_iopub(kc, printed('looping'))
        await kc.interrupt()
        await wait_iopub(kc, printed('survived'))
        await kc.interrupt()
        await _interrupted(kc, cell)

        await kc.exec_ok('get_ipython().input_transformers_cleanup.append(lambda lines: time.sleep(0.5) or lines)')
        cell = kc.reply('while True: pass')
        await wait_status(kc, 'busy')
        await kc.interrupt()
        await _interrupted(kc, cell)
