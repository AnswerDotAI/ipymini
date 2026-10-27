from ..aclient import *


async def test_interactive_input_and_recovery():
    async with mini_kernel() as (_, kc):
        cell = kc.reply("print('before'); name = input('Name: ')", allow_stdin=True)
        request = await kc.jmsgq.get('stdin', timeout=3)
        assert request['content']['prompt'] == 'Name: ' and not request['content']['password']
        before = await wait_iopub(kc, lambda m: m['msg_type'] == 'stream')
        assert 'before' in before['content']['text']
        await kc.input_reply('Ada')
        reply = await cell
        assert reply['content']['status'] == 'ok'
        await kc.iopub_drain(parent_id(reply))

        cell = kc.reply("import getpass; password = getpass.getpass('Password: ')", allow_stdin=True)
        request = await kc.jmsgq.get('stdin', timeout=3)
        assert request['content']['password']
        await kc.input_reply('secret')
        reply = await cell
        assert reply['content']['status'] == 'ok'
        await kc.iopub_drain(parent_id(reply))
        await kc.exec_ok("assert name == 'Ada' and password == 'secret'")

        for value in ('first', 'second'):
            cell = kc.reply('typed = input()', allow_stdin=True)
            await kc.jmsgq.get('stdin', timeout=3)
            await kc.input_reply(value)
            if value == 'first': await kc.input_reply(value)
            reply = await cell
            assert reply['content']['status'] == 'ok'
            await kc.iopub_drain(parent_id(reply))
            assert await kc.eval_expr('typed') == value

        reply, _ = await kc.exec_drain('input()', allow_stdin=False)
        assert reply['content']['ename'] == 'StdinNotImplementedError'
        cell = kc.reply('input()', allow_stdin=True)
        await kc.jmsgq.get('stdin', timeout=3)
        await kc.interrupt()
        reply = await cell
        assert reply['content']['ename'] == 'KeyboardInterrupt'
        await kc.iopub_drain(parent_id(reply))
        await kc.exec_ok('1+1')
