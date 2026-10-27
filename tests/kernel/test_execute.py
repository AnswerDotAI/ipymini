import asyncio
from ..aclient import *


async def test_execution_errors_expressions_and_async_magic():
    async with mini_kernel() as (_, kc):
        first, _ = await kc.exec_ok('a = 10')
        reply, _ = await kc.exec_ok('a += 1', store_history=False, user_expressions={'value': 'a', 'bad': '1/0'})
        assert reply['content']['execution_count'] == first['content']['execution_count']
        expressions = reply['content']['user_expressions']
        assert expressions['value']['data']['text/plain'] == '11'
        assert expressions['bad']['ename'] == 'ZeroDivisionError'
        next_reply, _ = await kc.exec_ok('assert a == 11')
        assert next_reply['content']['execution_count'] == first['content']['execution_count'] + 1

        reply, outputs = await kc.exec_drain('1/0', user_expressions={'value': 'a'})
        assert reply['content']['status'] == 'error'
        assert iopub_msgs(outputs, 'error')[0]['content']['ename'] == 'ZeroDivisionError'
        await kc.exec_ok('assert a == 11')

        calls = [kc.reply(code) for code in ('import asyncio; ordered = [await asyncio.sleep(0.1, 1)]', 'ordered.append(2)', 'assert ordered == [1, 2]')]
        assert all(r['content']['status'] == 'ok' for r, _ in await gather_outputs(kc, *calls))
        fail = "import time; time.sleep(0.2); raise ValueError('boom')"
        for stop, expected in ((True, 'aborted'), (False, 'ok')):
            failed, tail = kc.reply(fail, stop_on_error=stop), kc.reply('1+1')
            queries = asyncio.gather(kc.cmd.kernel_info(), kc.cmd.comm_info(), kc.cmd.inspect(code='print', cursor_pos=5))
            results = await gather_outputs(kc, failed, tail)
            assert [r['content']['status'] for r, _ in results] == ['error', expected]
            assert all(r['content']['status'] == 'ok' for r in await queries)
            for _, outputs in results:
                assert [m['content']['execution_state'] for m in outputs if m['msg_type'] == 'status'] == ['busy', 'idle']
        for kind in ('execute_request', 'complete_request', 'history_request'):
            reply = await kc.shell_request(kind)
            assert reply['content']['status'] == 'error' and reply['content']['ename'] == 'MissingField'
            if kind == 'execute_request':
                outputs = await kc.iopub_drain(parent_id(reply))
                assert [m['content']['execution_state'] for m in outputs if m['msg_type'] == 'status'] == ['busy', 'idle']

        for i in range(20): kc.execute(f'uncollected = {i}')
        assert (await kc.exec_ok('assert uncollected == 19'))[0]['content']['status'] == 'ok'
        results = await gather_outputs(kc, *(kc.reply(f"print('cell {i}'); {i}") for i in range(200)))
        assert len({r['content']['execution_count'] for r, _ in results}) == 200
        for i, (reply, outputs) in enumerate(results):
            assert reply['content']['status'] == 'ok' and f'cell {i}' in stream_text(outputs)
            assert iopub_msgs(outputs, 'execute_result')[0]['content']['data']['text/plain'] == str(i)
            assert [m['content']['execution_state'] for m in outputs if m['msg_type'] == 'status'] == ['busy', 'idle']

        running = kc.reply('import time; time.sleep(0.8)')
        await wait_status(kc, 'busy')
        replies = await asyncio.gather(*(kc.cmd.is_complete(code='1+1') for _ in range(130)))
        assert all(r['msg_type'] == 'is_complete_reply' and r['content']['status'] == 'complete' for r in replies)
        reply = await running
        assert reply['content']['status'] == 'ok'
        await kc.iopub_drain(parent_id(reply))

        await kc.exec_ok("async def amag(line): return f'async:{line}'\nget_ipython().register_magic_function(amag, 'line', 'amag')")
        _, outputs = await kc.exec_ok('%amag hi')
        assert 'async:hi' in iopub_msgs(outputs, 'execute_result')[0]['content']['data']['text/plain']
