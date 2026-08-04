"""Regression tests from the Solveit gateway era, driven through ConKernelClient.

The uncollected-messages test pins the IOPub stall bug: fire-and-forget executes (the
live `xpush` pattern) must not stop the kernel publishing. The gateway-notebook test runs
the real Solveit gateway notebook (copied into `meta/`, not in git) inside the kernel:
code that itself imports zmq and jupyter_client, the self-hosting scenario that
historically hung. Both assert that every tracked request reaches iopub idle, and the
gateway run additionally that no cell errors (hide/export cells are skipped on load).
"""
import asyncio, json
from pathlib import Path

import pytest

from ..aclient import *


@pytest.fixture(scope="module")
async def kc():
    "One shared kernel for this module; each test drains its own requests."
    async with mini_kernel() as (_, kc): yield kc


async def test_multiple_payloads(kc):
    code = """pm = get_ipython().payload_manager
pm.write_payload(dict(source='testing', foo='bar'), single=False)
pm.write_payload(dict(source='testing', bar='baz'), single=False)
"""
    r = await kc.reply(code)
    assert len(r["content"]["payload"]) == 2, r["content"]
    await aflush(kc)


async def test_uncollected_messages_dont_stall_iopub(kc):
    "5 uncollected fire-and-forget sends per tracked cell, 20 cells: every tracked cell must reach idle."
    await aflush(kc)
    tracked = []
    for n in range(20):
        for _ in range(5): kc.execute(f"__msg_id = 'cell_{n}'")
        tracked.append(kc.execute(f"cell_{n}_result = {n}"))
    await collect_iopub(kc, tracked, timeout=30)
    await aflush(kc, timeout=0.5)


@pytest.mark.slow
@pytest.mark.timeout(180)
async def test_gateway_notebook_cells(kc):
    "Run every code cell of the real gateway notebook in the kernel, with uncollected sends interleaved."
    pp = Path(__file__).resolve().parents[2]
    nb = json.loads((pp/"meta"/"00_gateway.ipynb").read_text())
    cells = [s for c in nb["cells"] if c["cell_type"] == "code"
        and (s := "".join(c["source"]).strip()) and not s.startswith(("#|hide", "#| hide"))]

    await aflush(kc)
    tracked = {}
    for code in cells:
        for _ in range(5): kc.execute("__msg_id = 'x'", stop_on_error=False)
        mid = kc.execute(code, allow_stdin=False, stop_on_error=False)
        tracked[mid] = code
        await asyncio.sleep(0.01)
    outputs = await collect_iopub(kc, list(tracked), timeout=120)

    errs = [(tracked[mid][:60], m["content"].get("ename")) for mid, msgs in outputs.items()
        for m in msgs if m["msg_type"] == "error"]
    assert not errs, f'{len(errs)} gateway cells errored: {errs}'
    await aflush(kc, timeout=0.5)
