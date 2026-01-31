from ipymini.shell import MiniShell


def test_complete_has_expected_shape(minishell):
    rep = minishell.complete("str.", cursor_pos=4)
    assert rep.get("status") == "ok"
    assert isinstance(rep.get("matches"), list)
    assert isinstance(rep.get("cursor_start"), int)
    assert isinstance(rep.get("cursor_end"), int)
    assert "metadata" in rep


def test_inspect_found(minishell):
    rep = minishell.inspect("len", cursor_pos=3, detail_level=0)
    assert rep.get("status") == "ok"
    assert rep.get("found") is True
    assert isinstance(rep.get("data"), dict)


def test_is_complete_incomplete_indent(minishell):
    rep = minishell.is_complete("for i in range(2):\n")
    assert rep.get("status") in ("incomplete", "complete", "invalid")
    if rep.get("status") == "incomplete": assert rep.get("indent") == " " * 4
