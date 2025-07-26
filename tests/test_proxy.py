import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from proxy_utils import parse_proxy, probe_proxy, ProxyError


def test_parse_ok_http():
    p = parse_proxy("http://1.2.3.4:8080")
    assert p == {
        "scheme": "http",
        "host": "1.2.3.4",
        "port": 8080,
        "user": None,
        "password": None,
    }


def test_parse_ok_socks5():
    p = parse_proxy("socks5://user:pass@127.0.0.1:1080")
    assert p["scheme"] == "socks5"
    assert p["host"] == "127.0.0.1"
    assert p["port"] == 1080
    assert p["user"] == "user"
    assert p["password"] == "pass"


def test_parse_bad_scheme():
    with pytest.raises(ProxyError):
        parse_proxy("ftp://host:21")


def test_probe_proxy(monkeypatch):
    calls = {}

    class Resp:
        def __init__(self, status):
            self.status_code = status

    def fake_head(url, proxies=None, timeout=None):
        calls["called"] = True
        return Resp(200)

    monkeypatch.setattr("requests.head", fake_head)
    parsed = parse_proxy("http://1.2.3.4:8080")
    assert probe_proxy(parsed)
    assert calls


def test_probe_proxy_fail(monkeypatch):
    def fake_head(url, proxies=None, timeout=None):
        raise OSError("boom")

    monkeypatch.setattr("requests.head", fake_head)
    parsed = parse_proxy("http://1.2.3.4:8080")
    assert not probe_proxy(parsed)
