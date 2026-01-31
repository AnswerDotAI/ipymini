from ipymini.debug.flags import DebugFlags, envbool


def test_envbool_truthy_falsy(monkeypatch):
    for value in ["", "0", "false", "no"]:
        monkeypatch.setenv("IPYMINI_FLAG", value)
        assert envbool("IPYMINI_FLAG") is False
    for value in ["1", "true", "yes", "on"]:
        monkeypatch.setenv("IPYMINI_FLAG", value)
        assert envbool("IPYMINI_FLAG") is True


def test_debugflags_from_env(monkeypatch):
    monkeypatch.setenv("TEST_DEBUG", "1")
    monkeypatch.setenv("TEST_DEBUG_MSGS", "true")
    flags = DebugFlags.from_env("TEST")
    assert flags.enabled is True
    assert flags.trace_msgs is True
