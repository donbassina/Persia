from pathlib import Path
from samokat_config import load_cfg

def test_defaults_load():
    cfg = load_cfg(Path(__file__).parent.parent, exit_on_error=False)
    assert cfg["UA"].startswith("Mozilla/")
