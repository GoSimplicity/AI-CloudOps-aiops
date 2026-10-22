import math

import pytest

from app.utils.safe_math import evaluate


@pytest.mark.parametrize("expression, expected", [("2 + 3 * 4", 14), ("sqrt(16)", 4), ("pow(2, 10)", 1024), ("sum([1, 2, 3])", 6), ("-2 ** 3", -8)])
def test_arithmetic(expression, expected):
    assert evaluate(expression, {"sqrt": math.sqrt, "sum": sum}) == expected


@pytest.mark.parametrize("expression", ["9 ** 999999999", "pow(9, 999999999)", "'a' * 999999999", "(1).__class__", "[1] * 999999999", "[x for x in [1]]", "1e309", "True", "__import__('os')", "2+" * 300 + "1"])
def test_rejects_code_and_unbounded_work(expression):
    with pytest.raises((ValueError, TypeError)):
        evaluate(expression, {})
