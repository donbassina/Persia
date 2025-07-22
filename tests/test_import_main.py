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
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        # Разрешаем ошибку исполнения, но не ImportError или SyntaxError
        if isinstance(e, (ImportError, SyntaxError)):
            raise
    finally:
        sys.stdin = stdin
