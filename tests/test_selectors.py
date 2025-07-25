import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from utils import load_selectors


def test_load_selectors_default_equals_explicit(tmp_path, monkeypatch):
    sel_default = load_selectors()
    sel_explicit = load_selectors("default")
    assert sel_default == sel_explicit


def test_load_selectors_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_selectors("nonexistent_profile")
