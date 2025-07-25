import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import importlib.util
import io
import requests
from utils import load_selectors


def test_load_selectors_default_equals_explicit(tmp_path, monkeypatch):
    sel_default = load_selectors()
    sel_explicit = load_selectors("default")
    assert sel_default == sel_explicit


def test_load_selectors_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_selectors("nonexistent_profile")


def test_profile_default_consistency(monkeypatch):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Samokat-TP.py")
    spec = importlib.util.spec_from_file_location("stp", path)
    stp = importlib.util.module_from_spec(spec)
    assert spec.loader

    class Resp:
        status_code = 200

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    spec.loader.exec_module(stp)

    async def dummy_run_browser(ctx):
        return None

    monkeypatch.setattr(stp, "run_browser", dummy_run_browser)
    import asyncio

    asyncio.run(stp.main(stp.ctx))
    assert stp.selectors == load_selectors("default")
