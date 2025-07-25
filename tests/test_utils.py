import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import RunContext, version_check


def test_clone_deepcopy():
    ctx = RunContext(cli_overrides={"a": "1"})
    clone = ctx.clone()
    assert clone == ctx
    clone.cli_overrides["a"] = "2"
    assert ctx.cli_overrides["a"] == "1"


def test_version_check(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr("importlib.metadata.version", lambda _: "1.5")
        version_check({"pkg": ("1.0", "2.0")})


def test_version_check_fail(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr("importlib.metadata.version", lambda _: "0.5")
        with pytest.raises(SystemExit):
            version_check({"pkg": ("1.0", "2.0")})
