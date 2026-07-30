"""cassis-cli targets Python >=3.10 (see pyproject `requires-python`).

Guards against 3.14-only syntax slipping in — notably black (run from the repo
root, which targets a newer Python) stripping the parentheses off a
multi-exception `except (A, B):`, which is a SyntaxError before 3.14.
"""

import ast
import pathlib

_PKG = pathlib.Path(__file__).resolve().parents[1] / "cassis_cli"


def test_all_modules_parse_under_python_310():
    modules = sorted(_PKG.glob("*.py"))
    assert modules, "no cassis_cli modules found"
    for module in modules:
        ast.parse(module.read_text(encoding="utf-8"), filename=str(module), feature_version=(3, 10))
