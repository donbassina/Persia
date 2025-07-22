from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))
from samokat_config import load_cfg

def test_defaults_load():
    cfg = load_cfg(Path(__file__).parent.parent, exit_on_error=False)
    assert cfg["UA"].startswith("Mozilla/")
