import ast
import pathlib

source_path = pathlib.Path(__file__).resolve().parent.parent / "Samokat-TP.py"
source = source_path.read_text(encoding="utf-8")
module_ast = ast.parse(source)

normalize_func = None
error_assign = None
for node in module_ast.body:
    if isinstance(node, ast.FunctionDef) and node.name == "normalize_phone":
        normalize_func = node
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "ERROR_RU":
                error_assign = node

module_dict = {}
if normalize_func is not None:
    exec(
        compile(
            ast.Module(body=[normalize_func], type_ignores=[]),
            filename="<ast>",
            mode="exec",
        ),
        module_dict,
    )
if error_assign is not None:
    exec(
        compile(
            ast.Module(body=[error_assign], type_ignores=[]),
            filename="<ast>",
            mode="exec",
        ),
        module_dict,
    )

np = module_dict["normalize_phone"]
ERROR_RU = module_dict["ERROR_RU"]


def test_normalize_phone_basic():
    assert np("+7 (999) 123-45-67") == "9991234567"
    assert np("89991234567") == "9991234567"
    assert np("12345") == "12345"
    assert np("") == ""


def test_error_ru_duplicate_phone():
    assert ERROR_RU["duplicate_phone"] == "Одновременный запуск для этого телефона"
