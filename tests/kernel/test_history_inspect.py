from ..aclient import *

async def test_history_and_inspect_features():
    async with mini_kernel() as (_, kc):
        reply = await kc.cmd.complete(code="pri", cursor_pos=3, timeout=10)
        matches = set(reply["content"].get("matches", []))
        assert "print" in matches

        reply = await kc.cmd.complete(code="from sys imp", cursor_pos=12, timeout=10)
        matches = set(reply["content"].get("matches", []))
        assert "import " in matches

        reply = await kc.cmd.is_complete(code="print('hello, world')", timeout=10)
        assert reply["content"]["status"] == "complete"

        reply = await kc.cmd.is_complete(code="print('''hello", timeout=10)
        assert reply["content"]["status"] == "incomplete"

        reply = await kc.cmd.is_complete(code="import = 7q", timeout=10)
        assert reply["content"]["status"] == "invalid"

        reply = await kc.cmd.inspect(code="open", cursor_pos=4, detail_level=0, timeout=10)
        content = reply["content"]
        assert content["status"] == "ok"
        assert content["found"]
        assert "text/plain" in content.get("data", {})

        reply, _ = await kc.exec_drain("1+1")
        assert reply["content"]["status"] == "ok"

        reply, _ = await kc.exec_drain("2+2")
        assert reply["content"]["status"] == "ok"

        reply = await kc.cmd.history(hist_access_type="tail", n=2, output=False, raw=True, timeout=10)
        assert reply["content"]["status"] == "ok"
        assert reply["content"]["history"]

        reply = await kc.cmd.history(hist_access_type="search", pattern="1?2*", output=False, raw=True, timeout=10)
        assert reply["content"]["status"] == "ok"

        for code in ("1+1", "1+2", "1+3", "1+1"):
            reply, _ = await kc.exec_drain(code)
            assert reply["content"]["status"] == "ok"

        reply = await kc.cmd.history(hist_access_type="search", pattern="1+*", output=False, raw=True, unique=True, timeout=10)
        assert reply["content"]["status"] == "ok"
        assert len(reply["content"]["history"]) >= 1

        reply = await kc.cmd.history(hist_access_type="search", pattern="1+*", output=False, raw=True, n=3, timeout=10)
        assert reply["content"]["status"] == "ok"
        assert len(reply["content"]["history"]) == 3
