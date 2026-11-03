from ..aclient import *

import pytest

setup = r'''
import pandas as pd
from IPython.display import DisplayObject, HTML, Markdown, display
class Rich:
    def _repr_mimebundle_(self, include=None, exclude=None):
        return {'text/html': '<b>rich</b>'}, {'text/html': {'source': 'rich'}, 'custom': 'keep'}
class FullFrame(pd.DataFrame): full = True
frame = pd.DataFrame({'name': ['example'], 'value': [1]})
'''


async def _shown(kc, expr, subshell_id=None):
    "Return expression, display and update output content for `expr`."
    _, out = await kc.exec_ok(rf'''obj = {expr}
display(obj)
handle = display(obj, display_id=True)
handle.update(obj)
obj''', subshell_id=subshell_id)
    kinds = ('display_data', 'update_display_data', 'execute_result')
    return [m['content'] for m in out if m['msg_type'] in kinds]


async def _text(kc, expr):
    "Return a text display's data, source and metadata."
    await kc.exec_ok(rf'obj = {expr}')
    return await kc.eval_expr('(obj.data, obj.url, obj.filename, obj.metadata)')


async def test_display_metadata_repr_and_text_sources(tmp_path):
    pytest.importorskip('pandas')
    env = profile_env(tmp_path, r'''c = get_config()
c.InteractiveShellApp.extensions = []
''')
    async with mini_kernel(extra_env=env) as (_, kc):
        await kc.exec_ok(setup)

        # Full output retains MIME metadata; unrelated HTML is not marked as prose.
        for full in (True, False):
            await kc.exec_ok(rf'rich = Rich(); rich.full = {full}')
            contents = await _shown(kc, 'rich')
            assert len(contents) == 4
            for content in contents:
                meta = content['metadata']
                assert '<b>rich</b>' in content['data']['text/html']
                assert meta['text/html'] == {'source': 'rich'} and meta['custom'] == 'keep'
                assert meta.get('full', False) is full

        # Explicit file and URL loading remains available for both text displays.
        path = tmp_path/'content.txt'
        path.write_text('loaded from file')
        for kind in ('Markdown', 'HTML'):
            data, url, filename, meta = await _text(kc, rf"{kind}(filename={str(path)!r}, metadata={{'custom': 'keep'}})")
            assert data == 'loaded from file' and filename == str(path) and url is None
            assert meta == {'custom': 'keep'}
        example_url = 'https://example.com'
        for kind in ('Markdown', 'HTML'):
            data, source, filename, meta = await _text(kc, rf"{kind}(url={example_url!r}, metadata={{'custom': 'keep'}})")
            assert 'Example Domain' in data and source == example_url and filename is None
            assert meta == {'custom': 'keep'}

        # Tables carry prose through every display path, including alongside full.
        for expr, full in [('frame', False), ('frame.style', False), ('FullFrame(frame)', True)]:
            contents = await _shown(kc, expr)
            assert len(contents) == 4
            for content in contents:
                assert '<table' in content['data']['text/html']
                assert content['metadata'].get('text/html', {}).get('prose') is True
                assert content['metadata'].get('full', False) is full

        # Reprs show their source, retain long strings and shorten non-string data.
        assert '<b>hello</b>' in await kc.eval_expr(r"repr(HTML('<b>hello</b>'))")
        assert 'x' * 100 in await kc.eval_expr(r"repr(Markdown('x' * 100))")
        short = await kc.eval_expr(r"repr(DisplayObject(b'x' * 100))")
        assert 'xxxx' in short and len(short) < 50
        await kc.exec_ok(r'''obj = DisplayObject()
obj.url, obj.filename = 'source-url', 'source-file'
''')
        text = await kc.eval_expr('repr(obj)')
        assert 'source-url' in text and 'source-file' not in text
        await kc.exec_ok(r"obj.data = 'source-data'")
        text = await kc.eval_expr('repr(obj)')
        assert 'source-data' in text and 'source-url' not in text
        await kc.exec_ok(r'obj.data = obj.url = None')
        assert 'source-file' in await kc.eval_expr('repr(obj)')
        await kc.exec_ok(r'obj.filename = None')
        assert await kc.eval_expr(r'repr(obj) == object.__repr__(obj)')

        # Positional text stays literal even when it names an existing file or URL.
        for kind in ('Markdown', 'HTML'):
            data, url, filename, meta = await _text(kc, rf"{kind}({str(path)!r}, metadata={{'custom': 'keep'}})")
            assert data == str(path) and url is None and filename is None
            assert meta == {'custom': 'keep'}
        for kind in ('Markdown', 'HTML'):
            data, source, filename, meta = await _text(kc, rf"{kind}({example_url!r}, metadata={{'custom': 'keep'}})")
            assert data == example_url and source is None and filename is None
            assert meta == {'custom': 'keep'}

        # A second shell keeps both flags and does not break the parent formatter.
        sid = (await kc.ctl.create_subshell())['content']['subshell_id']
        try:
            for lane in (sid, None):
                for content in await _shown(kc, 'FullFrame(frame)', subshell_id=lane):
                    assert content['metadata']['full'] is True
                    assert content['metadata']['text/html']['prose'] is True
        finally: await kc.ctl.delete_subshell(subshell_id=sid)
