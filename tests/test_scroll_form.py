import os
import sys
import io
import json
import asyncio
import importlib.util
import pytest


# helper to load the Samokat-TP module
def load_module(monkeypatch):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Samokat-TP.py")
    spec = importlib.util.spec_from_file_location("stp", path)
    stp = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: type("R", (), {"status_code": 200})()
    )
    spec.loader.exec_module(stp)
    return stp


class DummyForm:
    def __init__(self, y):
        self.y = y

    async def bounding_box(self):
        return {"x": 0, "y": self.y, "width": 100, "height": 100}


class DummyPage:
    def __init__(self, height=9000, viewport=800, form_y=7000):
        self.height = height
        self.viewport = viewport
        self.scroll_y = 0
        self.form = DummyForm(form_y)

    async def query_selector(self, sel):
        if sel == "div.form-wrapper":
            return self.form
        return None

    async def evaluate(self, script):
        if script == "window.innerHeight":
            return self.viewport
        if script == "window.scrollY":
            return self.scroll_y
        if script == "document.body.scrollHeight":
            return self.height
        if script == "window.innerWidth - 4":
            return 100
        return None


def test_scroll_timeout(monkeypatch):
    stp = load_module(monkeypatch)
    stp.CFG.update(
        {
            "SCROLL_STEP": {
                "down1": [120, 350],
                "down2": [400, 800],
                "up": [80, 290],
                "fine": [300, 100, 40, 12],
            },
            "SCROLL_TO_FORM": {
                "TIMEOUT": 8,
                "BLOCK_PAUSE": [0.3, 0.9],
                "FINE_STEPS": [150, 60, 25, 10],
                "MAX_ITERS": 5,
            },
        }
    )

    page = DummyPage()
    calls = {"scroll": 0, "drag": 0}

    async def fake_human_scroll(px):
        calls["scroll"] += 1
        page.scroll_y = max(0, min(page.scroll_y + px, page.height - page.viewport))

    async def fake_drag_scroll(px):
        calls["drag"] += 1
        page.scroll_y = max(0, min(page.scroll_y + px, page.height - page.viewport))

    monkeypatch.setattr(stp, "human_scroll", fake_human_scroll)
    monkeypatch.setattr(stp, "drag_scroll", fake_drag_scroll)

    t = {"v": 0.0}

    async def fake_sleep(dt):
        t["v"] += dt

    class FakeLoop:
        def time(self):
            return t["v"]

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: FakeLoop())

    asyncio.run(stp.smooth_scroll_to_form(page, stp.RunContext()))
    assert t["v"] <= 8
    assert calls["scroll"] >= 1


def test_bad_scroll_config(tmp_path):
    from samokat_config import load_cfg, RunContext

    data = json.loads(open("config_defaults.json", encoding="utf-8").read())
    data["SCROLL_TO_FORM"]["FINE_STEPS"] = [1, 2]
    cfg_path = tmp_path / "config_defaults.json"
    cfg_path.write_text(json.dumps(data))

    with pytest.raises(SystemExit):
        load_cfg(tmp_path, ctx=RunContext())

