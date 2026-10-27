import asyncio, io, os, subprocess, sys
import pytest
from ipymini.shell import MiniShell
from ipymini.term import MiniStream
from ipymini.debug.cells import debug_cell_filename


def test_standalone_shell_capture_and_streams(tmp_path):
    from ..kernel_utils import build_env
    ipdir = tmp_path/'ipython'
    ipdir.mkdir()
    for use_jedi in ('0', '1'):
        env = build_env(dict(IPYTHONDIR=str(ipdir), IPYMINI_USE_JEDI=use_jedi, KERNMINI_CELL_NAME=str(tmp_path/'cell.py')))
        subprocess.run([sys.executable, __file__], input='Ada\n', text=True, check=True, env=env)


async def _story():
    read_input = input
    shell = MiniShell(request_input=lambda prompt, password: read_input(prompt))
    assert shell.ipy.Completer.use_jedi == (os.environ['IPYMINI_USE_JEDI'] == '1')
    assert debug_cell_filename('print(1)') == os.environ['KERNMINI_CELL_NAME']
    with shell.execution_context(allow_stdin=True, silent=False):
        result = await shell.execute("from IPython.display import display\nname = input('Name: ')\nprint(name)\ndisplay({'a': 1})\n1+1")
    assert result['error'] is None and result['result']['text/plain'] == '2'
    assert 'Ada' in ''.join(e['text'] for e in result['streams']) and result['display']
    with shell.execution_context(allow_stdin=False, silent=False): failed = await shell.execute('1/0', user_expressions={'a': 'name'})
    assert failed['error']['ename'] == 'ZeroDivisionError' and failed['user_expressions'] == {}
    with shell.execution_context(allow_stdin=False, silent=True):
        silent = await shell.execute('x = 1', silent=True, store_history=False, user_expressions='{"a": "x+1"}')
    assert silent['user_expressions']['a']['data']['text/plain'] == '2'

    child = MiniShell(request_input=shell.request_input, use_singleton=False)
    with shell.execution_context(allow_stdin=False, silent=False):
        await shell.execute("import sys\nprint('outer-before')\nprint('err', file=sys.stderr)")
        with child.execution_context(allow_stdin=False, silent=False): inner = await child.execute("print('inner')")
        print('outer-after')
        outer = shell.capture.snapshot()
    assert [e['name'] for e in outer['streams']] == ['stdout', 'stderr', 'stdout']
    assert 'outer-after' in outer['streams'][-1]['text']
    assert 'inner' in inner['streams'][0]['text'] and 'inner' not in ''.join(e['text'] for e in outer['streams'])

    live = io.BytesIO()
    stream = MiniStream('stdout', None, sink=lambda name, text: live.write(text.encode('utf-8')))
    stream.write(b'alpha\nbeta\n')
    assert live.getvalue() == b'alpha\nbeta\n'
    stream.writelines(['gamma', 'delta'])
    assert live.getvalue() == b'alpha\nbeta\n'
    stream.flush()
    assert live.getvalue() == b'alpha\nbeta\ngammadelta'
    with pytest.raises(UnicodeEncodeError): stream.write('\udcff\n')
    stream.write('good\n')
    assert live.getvalue().endswith(b'good\n')


if __name__ == '__main__': asyncio.run(_story())
