from ..aclient import *


async def test_comm_callbacks_buffers_and_execution_context():
    async with mini_kernel() as (_, kc):
        _, setup_outputs = await kc.exec_ok('''import asyncio
from comm import create_comm, get_comm_manager
assert get_ipython().kernel.comm_manager is get_comm_manager()
comm_ready = asyncio.Event()
def target(comm, open_msg):
    comm.send(data={'ack': open_msg['content']['data'], 'parent': get_ipython().kernel.get_parent()['header']['msg_id']}, buffers=open_msg['buffers'])
    def on_msg(msg):
        comm.send(data={'echo': msg['content']['data']}, buffers=msg['buffers'])
        print('callback-output', flush=True)
        comm_ready.set()
    comm.on_msg(on_msg)
get_comm_manager().register_target('echo', target)
outbound = create_comm(target_name='outbound', buffers=[b'open-buffer'])
outbound.close(data={'done': True})
''')
        assert bytes(iopub_msgs(setup_outputs, 'comm_open')[0]['buffers'][0]) == b'open-buffer'
        assert iopub_msgs(setup_outputs, 'comm_close')[0]['content']['data'] == {'done': True}
        cid = 'story-comm'
        opened = kc.comm_open('echo', cid, data={'n': 7}, buffers=[b'open'])
        messages = []
        while True:
            message = await kc.jmsgq.get('iopub', timeout=3)
            messages.append(message)
            if message['msg_type'] == 'comm_msg' and message['content'].get('comm_id') == cid:
                ack = message
                break
        assert ack['content']['data'] == {'ack': {'n': 7}, 'parent': opened} and bytes(ack['buffers'][0]) == b'open'
        assert parent_id(ack) == opened

        mid = kc.new_msg_id()
        cell = kc.reply("await comm_ready.wait(); print('resumed-cell', flush=True)", msg_id=mid)
        await wait_status(kc, 'busy')
        sent = kc.comm_msg(cid, data={'hi': 1}, buffers=[b'message'])
        while True:
            message = await kc.jmsgq.get('iopub', timeout=3)
            messages.append(message)
            if parent_id(message) == mid and message['msg_type'] == 'status' and message['content']['execution_state'] == 'idle': break
        comms = [m for m in messages if m['msg_type'] in ('comm_open', 'comm_msg') and m['content'].get('comm_id') == cid]
        assert [m['msg_type'] for m in comms] == ['comm_msg', 'comm_msg']
        echo = comms[1]
        assert echo['content']['data'] == {'echo': {'hi': 1}} and bytes(echo['buffers'][0]) == b'message'
        assert parent_id(echo) == sent
        stream = next(m for m in messages if m['msg_type'] == 'stream' and 'callback-output' in m['content']['text'])
        assert parent_id(stream) == sent
        reply = await cell
        assert reply['content']['status'] == 'ok'
        resumed = next(m for m in messages if m['msg_type'] == 'stream' and 'resumed-cell' in m['content']['text'])
        assert parent_id(resumed) == mid
