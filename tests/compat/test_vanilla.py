import pytest, zmq
from jupyter_client.session import Session
from jupyter_client import AsyncKernelClient
from ..kernel_utils import vanilla_kernel, vanilla_manager, wait_for_status, wait_for_msg, stream_text, default_timeout

pytestmark = pytest.mark.compat


def test_vanilla_jupyter_session():
    "Ordinary Jupyter clients must work without ConKernelClient's Session.send patch."
    assert not hasattr(Session, '_orig_send')
    with vanilla_kernel() as (km, kc):
        info_id = kc.kernel_info()
        info = kc.shell_reply(info_id)
        content = info['content']
        assert content['status'] == 'ok' and content['protocol_version'] == '5.3'
        assert content['implementation'] == 'ipymini' and content['implementation_version']
        assert content['language_info']['name'] == 'python' and content['language_info']['file_extension'] == '.py'
        assert info['parent_header']['msg_id'] == info_id
        connection = km.get_connection_info()
        connect_request = kc.session.msg('connect_request', {})
        kc.shell_channel.send(connect_request)
        ports = kc.shell_reply(connect_request['header']['msg_id'])['content']
        assert all(ports[f'{name}_port'] == connection[f'{name}_port'] for name in ('shell', 'iopub', 'stdin', 'control', 'hb'))
        with zmq.Context() as ctx, ctx.socket(zmq.REQ) as heartbeat:
            heartbeat.linger = 0
            heartbeat.connect(f"{connection['transport']}://{connection['ip']}:{connection['hb_port']}")
            heartbeat.send(b'ping')
            assert heartbeat.poll(1000) and heartbeat.recv() == b'ping'

        mid, reply, outputs = kc.exec_drain("print('hello'); 40+2")
        assert reply['content']['status'] == 'ok' and 'hello' in stream_text(outputs)
        assert any(m['msg_type'] == 'execute_result' and m['content']['data']['text/plain'] == '42' for m in outputs)
        for message in (info, reply, *outputs):
            assert all(message['header'].get(key) for key in ('msg_id', 'msg_type', 'session', 'username', 'version'))
            assert int(message['header']['version'].split('.')[0]) >= 5
        assert all(m['parent_header']['msg_id'] == mid for m in (reply, *outputs))
        assert [m['content']['execution_state'] for m in outputs if m['msg_type'] == 'status'] == ['busy', 'idle']

        _, reply, _ = kc.exec_drain('1/0')
        assert reply['content']['ename'] == 'ZeroDivisionError'
        mid = kc.execute("name = input('name? ')", allow_stdin=True)
        assert kc.stdin_channel.get_msg(timeout=default_timeout)['msg_type'] == 'input_request'
        kc.input('Ada')
        assert kc.shell_reply(mid)['content']['status'] == 'ok'
        kc.iopub_drain(mid)
        _, reply, _ = kc.exec_drain("assert name == 'Ada'")
        assert reply['content']['status'] == 'ok'

        mid = kc.execute('import time; time.sleep(30)')
        wait_for_status(kc, 'busy')
        kc.interrupt_request()
        assert kc.shell_reply(mid)['content']['ename'] == 'KeyboardInterrupt'
        kc.iopub_drain(mid)
        assert kc.exec_drain('40+2')[1]['content']['status'] == 'ok'
        kc.execute('time.sleep(1000)')
        wait_for_status(kc, 'busy')
        request = kc.session.msg('shutdown_request', {'restart': False})
        kc.control_channel.send(request)
        reply = wait_for_msg(kc.control_channel.get_msg, lambda m: m['parent_header'].get('msg_id') == request['header']['msg_id'])
        assert reply['msg_type'] == 'shutdown_reply' and reply['content']['status'] == 'ok'
        km.provisioner.process.wait(timeout=5)
    assert not km.is_alive()


async def _until(get_message, predicate):
    while True:
        message = await get_message(timeout=3)
        if predicate(message): return message


async def test_async_vanilla_jupyter_session():
    assert not hasattr(Session, '_orig_send')
    with vanilla_manager() as km:
        kc = AsyncKernelClient()
        kc.load_connection_info(km.get_connection_info())
        kc.start_channels()
        try:
            await kc.wait_for_ready(timeout=10)
            mid = kc.execute("print('async hello'); 42")
            reply = await _until(kc.get_shell_msg, lambda m: m['parent_header'].get('msg_id') == mid)
            assert reply['content']['status'] == 'ok'
            output = []
            while True:
                message = await kc.get_iopub_msg(timeout=3)
                if message['parent_header'].get('msg_id') != mid: continue
                output.append(message)
                if message['msg_type'] == 'status' and message['content']['execution_state'] == 'idle': break
            assert 'async hello' in stream_text(output)
            assert any(m['msg_type'] == 'execute_result' and m['content']['data']['text/plain'] == '42' for m in output)

            mid = kc.execute('import time; time.sleep(30)')
            await _until(kc.get_iopub_msg, lambda m: m['parent_header'].get('msg_id') == mid and m['msg_type'] == 'status'
                and m['content']['execution_state'] == 'busy')
            request = kc.session.msg('interrupt_request', {})
            kc.control_channel.send(request)
            reply = await _until(kc.control_channel.get_msg, lambda m: m['parent_header'].get('msg_id') == request['header']['msg_id'])
            assert reply['msg_type'] == 'interrupt_reply'
            reply = await _until(kc.get_shell_msg, lambda m: m['parent_header'].get('msg_id') == mid)
            assert reply['content']['ename'] == 'KeyboardInterrupt'
        finally: kc.stop_channels()
