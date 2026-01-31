import comm

from ipymini.shell import comm_context


def test_comm_publish_calls_sender(minishell):
    seen = []
    parent = {"header": {"msg_id": "p"}}

    def sender(msg_type, parent_msg, **kwargs): seen.append((msg_type, parent_msg, kwargs))

    with comm_context(sender, parent):
        c = comm.create_comm(target_name="demo", primary=False)
        c.open(data={"a": 1})
        c.send(data={"b": 2})
        c.close(data={"c": 3})

    assert [m for (m, _p, _k) in seen][:3] == ["comm_open", "comm_msg", "comm_close"]
    for msg_type, parent_msg, kw in seen:
        assert parent_msg == parent
        content = kw.get("content") or {}
        assert content.get("comm_id")  # always present
        assert "data" in content


def test_comm_publish_no_sender_is_noop():
    called = {"n": 0}
    def sender(*args, **kwargs): called["n"] += 1

    parent = {"header": {"msg_id": "p2"}}
    c = comm.create_comm(target_name="demo2")
    # Without comm_context, publish_msg should do nothing.
    c.open(data={"x": 1})
    assert called["n"] == 0

    with comm_context(sender, parent): c.send(data={"y": 2})
    assert called["n"] == 1
