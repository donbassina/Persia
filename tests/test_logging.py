import io
import sys
import json
import importlib.util
import asyncio


def _run_script(monkeypatch, tmp_path, ok=True):
    data = {"user_phone": "79990001234"}
    if not ok:
        data["headless"] = "not_bool"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: type("R", (), {"status_code": 200})()
    )
    spec = importlib.util.spec_from_file_location("stp", "Samokat-TP.py")
    stp = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(stp)
    except SystemExit:
        return stp.ctx.log_file

    async def dummy_run_browser(ctx):
        return None

    if hasattr(stp, "run_browser"):
        monkeypatch.setattr(stp, "run_browser", dummy_run_browser)
    try:
        asyncio.run(stp.main(stp.ctx))
    except SystemExit:
        pass
    return stp.ctx.log_file


def test_result_success(monkeypatch, tmp_path):
    lf = _run_script(monkeypatch, tmp_path, ok=True)
    assert "RESULT: SUCCESS" in open(lf, encoding="utf-8").read()


def test_result_error(monkeypatch, tmp_path):
    lf = _run_script(monkeypatch, tmp_path, ok=False)
    assert "RESULT: ERROR" in open(lf, encoding="utf-8").read()
