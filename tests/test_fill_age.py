import os
import sys
import importlib.util
import io
import asyncio

import requests


def load_module(monkeypatch):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Samokat-TP.py")
    spec = importlib.util.spec_from_file_location("stp", path)
    stp = importlib.util.module_from_spec(spec)

    class Resp:
        status_code = 200

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    spec.loader.exec_module(stp)
    return stp


def test_fill_age_selector(monkeypatch):
    stp = load_module(monkeypatch)

    stp.selectors = {"form": {"age": "input[name='user_age']"}}

    class DummyElement:
        def __init__(self):
            self.clicked = False
            self.filled = False

        async def click(self):
            self.clicked = True

        async def fill(self, _):
            self.filled = True

        async def input_value(self):
            return "18"

    class DummyPage:
        def __init__(self):
            self.used_selector = None
            self.elem = DummyElement()

        def locator(self, selector):
            self.used_selector = selector
            return self.elem

        async def wait_for_timeout(self, ms):
            pass

    async def dummy_move_cursor(page, el, ctx):
        pass

    async def dummy_human_type(page, selector, text, ctx):
        pass

    monkeypatch.setattr(stp, "human_move_cursor", dummy_move_cursor)
    monkeypatch.setattr(stp, "human_type", dummy_human_type)

    ctx = stp.RunContext()
    page = DummyPage()
    asyncio.run(stp.fill_age(page, "18", ctx))
    assert page.used_selector == stp.selectors["form"]["age"]
