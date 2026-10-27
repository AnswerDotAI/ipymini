import zmq
from jupyter_client.session import Session
from ..kernel_utils import vanilla_kernel, iter_timeout


def _kernel_info(session, sock):
    session.send(sock, 'kernel_info_request', {})
    for _ in iter_timeout(1):
        if not sock.poll(10): continue
        _, message = session.recv(sock)
        if message and message['msg_type'] == 'kernel_info_reply': return message
    raise AssertionError('kernel_info reply missing after socket handover')


def test_same_identity_router_handover():
    with vanilla_kernel() as (km, kc):
        kc.stop_channels()
        connection = km.get_connection_info()
        session = Session(key=connection['key'], signature_scheme=connection['signature_scheme'])
        with zmq.Context() as ctx, ctx.socket(zmq.DEALER) as first, ctx.socket(zmq.DEALER) as second:
            for sock in (first, second):
                sock.linger = 0
                sock.setsockopt(zmq.IDENTITY, b'ipymini-handover')
                sock.connect(f"{connection['transport']}://{connection['ip']}:{connection['shell_port']}")
                assert _kernel_info(session, sock)['content']['implementation'] == 'ipymini'
