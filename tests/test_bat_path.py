import os


def test_run_loop_exists():
    assert os.path.isfile("run_loop.bat"), "run_loop.bat not found"
