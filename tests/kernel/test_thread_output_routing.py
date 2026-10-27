from ..aclient import *


async def test_background_output_keeps_its_execution_context():
    async with mini_kernel() as (_, kc):
        launch = '''import asyncio, threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
tag = ContextVar('story-tag', default=None)
token = tag.set('first')
go = threading.Event()
task_gate = asyncio.Event()
async def later():
    await task_gate.wait()
    print('task-from-first-cell', flush=True)
task = asyncio.create_task(later())
def work():
    go.wait(5)
    assert tag.get() == 'first'
    print('thread-from-first-cell', flush=True)
class MyThread(threading.Thread):
    def run(self): work()
threads = [threading.Thread(target=work), MyThread()]
for thread in threads: thread.start()
pool = ThreadPoolExecutor(max_workers=1)
pool.submit(lambda: None).result()
tag.reset(token)
print('first-cell', flush=True)
'''
        next_cell = '''go.set()
for thread in threads: thread.join(timeout=5)
task_gate.set()
await task
assert pool.submit(tag.get).result() is None
token = tag.set('second')
assert pool.submit(tag.get).result() == 'second'
tag.reset(token)
pool.submit(print, 'pool-from-second-cell').result()
pool.shutdown()
print('second-cell', flush=True)
'''
        first, second = await gather_outputs(kc, kc.reply(launch), kc.reply(next_cell))
        assert first[0]['content']['status'] == second[0]['content']['status'] == 'ok'
        first_text, second_text = (stream_text(out) for _, out in (first, second))
        assert 'first-cell' in first_text and first_text.count('thread-from-first-cell') == 2
        assert 'thread-from-first-cell' not in second_text
        assert 'task-from-first-cell' in first_text and 'task-from-first-cell' not in second_text
        assert 'pool-from-second-cell' in second_text and 'second-cell' in second_text
        assert 'pool-from-second-cell' not in first_text
