import pytest

pytest.importorskip("playwright.async_api", reason="playwright not installed")
pytest.importorskip("python_ghost_cursor", reason="ghost-cursor not installed")
pytest.importorskip("watchdog.observers", reason="watchdog not installed")


def test_import_main():
    import importlib.util
    import sys
    import pathlib
    import io

    sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

    file = pathlib.Path(__file__).parent.parent / "Samokat-TP.py"
    spec = importlib.util.spec_from_file_location("SamokatTP", file)
    module = importlib.util.module_from_spec(spec)
    sys.modules["SamokatTP"] = module
    stdin = sys.stdin
    sys.stdin = io.StringIO("{}")
    argv_backup = sys.argv
    sys.argv = ["Samokat-TP.py", "--no-watch"]
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        # Мы игнорируем любые ошибки выполнения скрипта,
        # но не ошибки импорта или синтаксиса, которые
        # могут указывать на неполадки в окружении.
        if isinstance(e, (ImportError, SyntaxError)):
            raise
    finally:
        sys.argv = argv_backup
        sys.stdin = stdin
