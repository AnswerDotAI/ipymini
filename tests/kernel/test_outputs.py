import pytest
from ..aclient import *


async def test_rich_output_and_payloads(tmp_path):
    env = profile_env(tmp_path, 'c = get_config()\nc.InteractiveShell.display_page = False\n')
    env['KERNMINI_IOPUB_QMAX'] = '1'
    async with mini_kernel(extra_env=env) as (_, kc):
        await kc.exec_ok("from IPython.display import display, HTML, clear_output\nget_ipython().ast_node_interactivity = 'all'")
        code = '''1+1
None
def f(): ...
display(HTML('<b>hello</b>'))
print('out', end='')
4-3'''
        reply, outputs = await kc.exec_ok(code)
        kinds = [m['msg_type'] for m in outputs if m['msg_type'] != 'status']
        assert kinds == ['execute_input', 'execute_result', 'display_data', 'stream', 'execute_result']
        results = iopub_msgs(outputs, 'execute_result')
        assert [m['content']['data']['text/plain'] for m in results] == ['2', '1']
        assert all(m['content']['execution_count'] == reply['content']['execution_count'] for m in results)
        assert 'hello' in iopub_msgs(outputs, 'display_data')[0]['content']['data']['text/html']
        assert stream_text(outputs) == 'out'

        _, outputs = await kc.exec_ok("import sys; print('stdout'); print('stderr', file=sys.stderr)")
        assert stream_text(outputs, 'stdout') == 'stdout\n' and stream_text(outputs, 'stderr') == 'stderr\n'
        _, outputs = await kc.exec_ok('for i in range(5000): print(i)')
        assert stream_text(outputs) == ''.join(f'{i}\n' for i in range(5000))
        _, outputs = await kc.exec_ok("sys.stdout.write('GHOST')", silent=True)
        assert not any(m['msg_type'] in {'stream', 'execute_result', 'display_data'} for m in outputs)
        _, outputs = await kc.exec_ok("print('next cell')")
        assert 'next cell' in stream_text(outputs) and 'GHOST' not in stream_text(outputs)

        _, outputs = await kc.exec_ok("clear_output(wait=True)\nh = display('first', display_id=True)\nh.update('second')")
        assert iopub_msgs(outputs, 'clear_output')[0]['content']['wait']
        first = iopub_msgs(outputs, 'display_data')[0]['content']
        second = iopub_msgs(outputs, 'update_display_data')[0]['content']
        assert first['transient']['display_id'] == second['transient']['display_id']
        assert 'second' in second['data']['text/plain']
        code = "display({'text/latex': r'\\frac{1}{2}'}, raw=True, metadata={'foo': 'bar'}, transient={'display_id': 'xyz'})"
        _, outputs = await kc.exec_ok(code)
        content = iopub_msgs(outputs, 'display_data')[0]['content']
        assert content['metadata']['foo'] == 'bar' and content['transient']['display_id'] == 'xyz'
        assert 'text/latex' in content['data']
        _, outputs = await kc.exec_ok("get_ipython().display_pub.publish({'text/plain': 'buffer'}, buffers=[b'bytes'])")
        assert bytes(iopub_msgs(outputs, 'display_data')[0]['buffers'][0]) == b'bytes'

        for code in ("try: 1/0\nexcept: get_ipython().showtraceback()", "get_ipython().run_cell('1/0')"):
            _, outputs = await kc.exec_ok(code)
            assert 'ZeroDivisionError' in stream_text(outputs, 'stderr')

        code = r"""print('bad \udcff')
display({'text/plain': 'bad \udcff'}, raw=True)
print('after')"""
        _, outputs = await kc.exec_ok(code)
        text = stream_text(outputs)
        assert text.startswith('bad \ufffd') and text.endswith('after\n')
        assert 'bad \ufffd' in iopub_msgs(outputs, 'display_data')[0]['content']['data']['text/plain']

        reply, _ = await kc.exec_ok("get_ipython().set_next_input('first')\nget_ipython().set_next_input('second')")
        assert [p['text'] for p in reply['content']['payload'] if p['source'] == 'set_next_input'] == ['second']
        reply, _ = await kc.exec_ok("pm = get_ipython().payload_manager\npm.write_payload({'source': 'one'}, single=False)\npm.write_payload({'source': 'two'}, single=False)")
        assert [p['source'] for p in reply['content']['payload']] == ['one', 'two']
        reply, _ = await kc.exec_ok('print?')
        assert any(p['source'] == 'page' for p in reply['content']['payload'])
        await kc.exec_ok('get_ipython().display_page = True')
        reply, outputs = await kc.exec_ok('print?')
        assert not any(p['source'] == 'page' for p in reply['content']['payload'])
        assert iopub_msgs(outputs, 'display_data')


@pytest.mark.slow
async def test_inline_figure():
    pytest.importorskip('matplotlib')
    async with mini_kernel() as (_, kc):
        _, outputs = await kc.exec_ok('from PIL import Image\nImage.new("RGB", (10, 10), "red")')
        assert 'image/png' in iopub_msgs(outputs, 'execute_result')[0]['content']['data']
        _, outputs = await kc.exec_ok('import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9]);\nplt.gcf()', timeout=20)
        displays = iopub_msgs(outputs, 'display_data')
        assert any('image/png' in m['content']['data'] or 'image/svg+xml' in m['content']['data'] for m in displays)
