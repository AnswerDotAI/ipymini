from ..aclient import *


async def test_configured_ipython_session(tmp_path):
    env = profile_env(tmp_path, "c = get_config()\nc.InteractiveShellApp.extensions = ['extmod']\nc.InteractiveShellApp.exec_lines = ['EXEC_LINE = 123']\n")
    (tmp_path/'extmod.py').write_text("def load_ipython_extension(ip): ip.user_ns['EXT_LOADED'] = 'ok'\n")
    startup = tmp_path/'ipython'/'profile_default'/'startup'
    startup.mkdir()
    (startup/'00-startup.py').write_text('STARTUP_OK = 456\n')

    async with mini_kernel(extra_env=env) as (_, kc):
        await kc.exec_ok("assert EXT_LOADED == 'ok' and EXEC_LINE == 123 and STARTUP_OK == 456")
        await kc.exec_ok("def range_ex(a, b): 'Add'\nprobe = 123")
        reply = await kc.cmd.complete(code='rang', cursor_pos=4)
        ranks = {c['text']: c['rank'] for c in reply['content']['metadata']['_jupyter_types_experimental']}
        assert ranks['range_ex'] < ranks['range']
        reply = await kc.cmd.inspect(code='range_ex(1, ', cursor_pos=12)
        assert reply['content']['found'] and 'text/plain' in reply['content']['data']
        sig = reply['content']['data']['application/json'][0]
        assert sig['label'] == 'range_ex' and sig['idx'] == 1
        reply = await kc.cmd.inspect(code='no_such_name_xyz', cursor_pos=16)
        assert reply['content']['status'] == 'ok' and not reply['content']['found']
        reply = await kc.cmd.history(hist_access_type='tail', n=1, output=False, raw=True)
        assert 'probe = 123' in reply['content']['history'][-1][2]
        assert (await kc.cmd.is_complete(code='for i in range(2):\n'))['content']['status'] == 'incomplete'
        for code, status in (("print('hello')", 'complete'), ('import = 7q', 'invalid')):
            assert (await kc.cmd.is_complete(code=code))['content']['status'] == status
        assert 'import ' in (await kc.cmd.complete(code='from sys imp', cursor_pos=12))['content']['matches']
        for code in ('history_probe = 1', 'history_probe = 2', 'history_probe = 1'): await kc.exec_ok(code)
        unique = await kc.cmd.history(hist_access_type='search', pattern='history_probe*', output=False, raw=True, unique=True)
        assert len(unique['content']['history']) == 2
        limited = await kc.cmd.history(hist_access_type='search', pattern='history_probe*', output=False, raw=True, n=2)
        assert len(limited['content']['history']) == 2
