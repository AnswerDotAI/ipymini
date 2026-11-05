import pytest
from ..aclient import *


async def test_text_displays(tmp_path):
    path = tmp_path/'content.txt'
    path.write_text('loaded from file')
    env = profile_env(tmp_path, 'c = get_config()\nc.InteractiveShellApp.extensions = []\n')
    async with mini_kernel(extra_env=env) as (_, kc):
        # Positional sources stay literal; explicit sources load content. Reprs keep the text.
        _, outputs = await kc.exec_ok(rf'''from IPython.display import HTML, Markdown, display
path = {str(path)!r}
assert Markdown(path).data == path
url = 'https://example.invalid'
assert HTML(url).data == url
loaded = Markdown(filename=path, metadata={{'source': 'file'}})
assert loaded.data == 'loaded from file' and loaded.metadata == {{'source': 'file'}}
assert 'Example Domain' in HTML(url='https://example.com').data
assert 'loaded from file' in repr(loaded)
assert 'x' * 100 in repr(Markdown('x' * 100))
html = HTML('<b>hello</b>', metadata={{'source': 'example'}})
html.full = True
display(html)
''')
        content = iopub_msgs(outputs, 'display_data')[0]['content']
        assert content['data']['text/html'] == '<b>hello</b>'
        assert content['metadata'] == {'full': True, 'text/html': {'source': 'example'}}


async def test_table_metadata(tmp_path):
    pytest.importorskip('pandas')
    env = profile_env(tmp_path, 'c = get_config()\nc.InteractiveShellApp.extensions = []\n')
    async with mini_kernel(extra_env=env) as (_, kc):
        _, outputs = await kc.exec_ok('''import pandas as pd
class FullFrame(pd.DataFrame): full = True
frame = FullFrame({'name': ['example'], 'value': [1]})
frame
''')
        content = iopub_msgs(outputs, 'execute_result')[0]['content']
        assert '<table' in content['data']['text/html']
        assert content['metadata'] == {'full': True, 'text/html': {'prose': True}}

        _, outputs = await kc.exec_ok('frame.style')
        assert iopub_msgs(outputs, 'execute_result')[0]['content']['metadata']['text/html']['prose'] is True

        # Creating a child preserves the shared patches in both shells.
        sid = (await kc.ctl.create_subshell())['content']['subshell_id']
        try:
            for lane in (sid, None):
                _, outputs = await kc.exec_ok('frame', subshell_id=lane)
                meta = iopub_msgs(outputs, 'execute_result')[0]['content']['metadata']
                assert meta == {'full': True, 'text/html': {'prose': True}}
        finally: await kc.ctl.delete_subshell(subshell_id=sid)
