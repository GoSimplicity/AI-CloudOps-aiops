"""Bounded arithmetic evaluator; never execute model-generated Python."""

import ast
import math
import operator

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}


def evaluate(expression, functions):
    if not isinstance(expression, str) or len(expression) > 512:
        raise ValueError("Expression must be at most 512 characters")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("Expression is too complex")

    def checked(value):
        if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Result must be a finite number within 1e100")
        return value

    def power(left, right):
        if abs(right) > 1000:
            raise ValueError("Exponent is too large")
        return checked(math.pow(left, right))

    def visit(node):
        if isinstance(node, ast.Constant):
            return checked(node.value)
        if isinstance(node, ast.Name) and node.id in functions and not callable(functions[node.id]):
            return checked(functions[node.id])
        if isinstance(node, (ast.List, ast.Tuple)):
            return [visit(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return checked(visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1))
        if isinstance(node, ast.BinOp):
            left, right = checked(visit(node.left)), checked(visit(node.right))
            if isinstance(node.op, ast.Pow):
                return power(left, right)
            if type(node.op) in _OPERATORS:
                return checked(_OPERATORS[type(node.op)](left, right))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            function = power if name == "pow" else functions.get(name)
            if callable(function):
                return checked(function(*(visit(arg) for arg in node.args)))
        raise ValueError("Unsupported expression")

    return checked(visit(tree.body))
