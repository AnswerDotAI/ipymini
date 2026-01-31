import asyncio

from ipymini.zmqthread import ThreadBoundAsyncQueue


def test_thread_bound_async_queue_put_is_scheduled_not_immediate():
    q = ThreadBoundAsyncQueue()

    async def _runner():
        q.bind(asyncio.get_running_loop())
        q.put("x")
        # put() uses call_soon_threadsafe; item should not be visible until the loop cycles.
        assert q.q.empty()
        await asyncio.sleep(0)
        assert await asyncio.wait_for(q.get(), timeout=1) == "x"

    asyncio.run(_runner())
