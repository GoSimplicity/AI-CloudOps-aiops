#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MCP 工具（calculator_tool）
"""

import math
from typing import Any, Dict

from ..mcp_server import BaseTool


class CalculatorTool(BaseTool):
    """基础数学计算器工具"""

    def __init__(self):
        super().__init__(
            name="calculate", description="执行基础数学计算，支持加、减、乘、除、幂运算"
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如 '2 + 3 * 4', 'sqrt(16)', 'pow(2, 10)'",
                },
                "precision": {
                    "type": "integer",
                    "description": "结果精度（小数位数），默认为2",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 2,
                },
            },
            "required": ["expression"],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        try:
            expression = parameters["expression"]
            precision = parameters.get("precision", 2)

            # 安全的环境变量
            safe_dict = {
                "abs": abs,
                "round": round,
                "min": min,
                "max": max,
                "sum": sum,
                "pow": pow,
                "sqrt": math.sqrt,
                "sin": math.sin,
                "cos": math.cos,
                "tan": math.tan,
                "asin": math.asin,
                "acos": math.acos,
                "atan": math.atan,
                "sinh": math.sinh,
                "cosh": math.cosh,
                "tanh": math.tanh,
                "log": math.log,
                "log10": math.log10,
                "log2": math.log2,
                "exp": math.exp,
                "ceil": math.ceil,
                "floor": math.floor,
                "pi": math.pi,
                "e": math.e,
                "tau": math.tau,
                "inf": math.inf,
                "nan": math.nan,
            }

            # 不允许的字符
            forbidden_chars = [
                "__",
                "import",
                "exec",
                "eval",
                "open",
                "file",
                "input",
                "raw_input",
            ]
            for char in forbidden_chars:
                if char in expression:
                    raise ValueError(f"不安全的表达式: {char}")

            # 计算表达式
            result = eval(expression, {"__builtins__": {}}, safe_dict)

            # 格式化结果
            if isinstance(result, float):
                formatted_result = round(result, precision)
            else:
                formatted_result = result

            return {
                "expression": expression,
                "result": formatted_result,
                "raw_result": result,
                "type": type(result).__name__,
                "precision": precision,
            }

        except Exception as e:
            raise RuntimeError(f"计算失败: {str(e)}") from e


class StatisticsTool(BaseTool):
    """统计计算工具"""

    def __init__(self):
        super().__init__(
            name="calculate_statistics",
            description="计算数字列表的统计信息：平均值、中位数、标准差等",
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "numbers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "数字列表",
                },
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "mean",
                            "median",
                            "mode",
                            "std",
                            "variance",
                            "min",
                            "max",
                            "sum",
                            "count",
                        ],
                    },
                    "description": "要计算的统计操作，默认为['mean', 'median', 'std', 'min', 'max']",
                    "default": ["mean", "median", "std", "min", "max"],
                },
            },
            "required": ["numbers"],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        try:
            numbers = parameters["numbers"]
            operations = parameters.get(
                "operations", ["mean", "median", "std", "min", "max"]
            )

            if not numbers:
                raise ValueError("数字列表不能为空")

            result = {}

            if "mean" in operations:
                result["mean"] = round(sum(numbers) / len(numbers), 4)

            if "median" in operations:
                sorted_nums = sorted(numbers)
                n = len(sorted_nums)
                if n % 2 == 0:
                    median = (sorted_nums[n // 2 - 1] + sorted_nums[n // 2]) / 2
                else:
                    median = sorted_nums[n // 2]
                result["median"] = round(median, 4)

            if "mode" in operations:
                from collections import Counter

                counter = Counter(numbers)
                max_count = max(counter.values())
                modes = [k for k, v in counter.items() if v == max_count]
                result["mode"] = modes if len(modes) > 1 else modes[0]

            if "std" in operations or "variance" in operations:
                mean = sum(numbers) / len(numbers)
                variance = sum((x - mean) ** 2 for x in numbers) / len(numbers)
                if "variance" in operations:
                    result["variance"] = round(variance, 4)
                if "std" in operations:
                    result["std"] = round(math.sqrt(variance), 4)

            if "min" in operations:
                result["min"] = min(numbers)

            if "max" in operations:
                result["max"] = max(numbers)

            if "sum" in operations:
                result["sum"] = sum(numbers)

            if "count" in operations:
                result["count"] = len(numbers)

            return {"numbers": numbers, "statistics": result, "operations": operations}

        except Exception as e:
            raise RuntimeError(f"统计计算失败: {str(e)}") from e


class UnitConverterTool(BaseTool):
    """单位转换工具"""

    def __init__(self):
        super().__init__(
            name="convert_units",
            description="单位转换工具，支持长度、重量、温度、存储单位等转换",
        )

    def get_parameters(self) -> Dict[str, Any]:
        """获取工具参数定义"""
        return {
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "要转换的数值"},
                "from_unit": {
                    "type": "string",
                    "description": "原始单位，例如 'm', 'kg', 'c', 'mb'",
                },
                "to_unit": {
                    "type": "string",
                    "description": "目标单位，例如 'ft', 'lb', 'f', 'gb'",
                },
                "category": {
                    "type": "string",
                    "description": "单位类别，可选值：'length', 'weight', 'temperature', 'storage', 'time'",
                    "enum": ["length", "weight", "temperature", "storage", "time"],
                },
            },
            "required": ["value", "from_unit", "to_unit", "category"],
        }

    async def execute(self, parameters: Dict[str, Any]) -> Any:
        """执行工具"""
        try:
            value = parameters["value"]
            from_unit = parameters["from_unit"].lower()
            to_unit = parameters["to_unit"].lower()
            category = parameters["category"]

            # 转换因子
            conversion_factors = {
                "length": {
                    "m": {
                        "m": 1,
                        "cm": 100,
                        "mm": 1000,
                        "km": 0.001,
                        "ft": 3.28084,
                        "in": 39.3701,
                        "yd": 1.09361,
                    },
                    "cm": {
                        "m": 0.01,
                        "cm": 1,
                        "mm": 10,
                        "km": 0.00001,
                        "ft": 0.0328084,
                        "in": 0.393701,
                        "yd": 0.0109361,
                    },
                    "ft": {
                        "m": 0.3048,
                        "cm": 30.48,
                        "mm": 304.8,
                        "km": 0.0003048,
                        "ft": 1,
                        "in": 12,
                        "yd": 0.333333,
                    },
                },
                "weight": {
                    "kg": {
                        "kg": 1,
                        "g": 1000,
                        "mg": 1000000,
                        "lb": 2.20462,
                        "oz": 35.274,
                    },
                    "g": {
                        "kg": 0.001,
                        "g": 1,
                        "mg": 1000,
                        "lb": 0.00220462,
                        "oz": 0.035274,
                    },
                    "lb": {
                        "kg": 0.453592,
                        "g": 453.592,
                        "mg": 453592,
                        "lb": 1,
                        "oz": 16,
                    },
                },
                "storage": {
                    "b": {
                        "b": 1,
                        "kb": 0.0009765625,
                        "mb": 0.000000953674,
                        "gb": 0.000000000931323,
                        "tb": 0.000000000000909495,
                    },
                    "kb": {
                        "b": 1024,
                        "kb": 1,
                        "mb": 0.0009765625,
                        "gb": 0.000000953674,
                        "tb": 0.000000000931323,
                    },
                    "mb": {
                        "b": 1048576,
                        "kb": 1024,
                        "mb": 1,
                        "gb": 0.0009765625,
                        "tb": 0.000000953674,
                    },
                    "gb": {
                        "b": 1073741824,
                        "kb": 1048576,
                        "mb": 1024,
                        "gb": 1,
                        "tb": 0.0009765625,
                    },
                },
                "time": {
                    "s": {
                        "s": 1,
                        "min": 1 / 60,
                        "h": 1 / 3600,
                        "day": 1 / 86400,
                        "ms": 1000,
                        "us": 1000000,
                    },
                    "min": {
                        "s": 60,
                        "min": 1,
                        "h": 1 / 60,
                        "day": 1 / 1440,
                        "ms": 60000,
                        "us": 60000000,
                    },
                    "h": {
                        "s": 3600,
                        "min": 60,
                        "h": 1,
                        "day": 1 / 24,
                        "ms": 3600000,
                        "us": 3600000000,
                    },
                },
            }

            if category == "temperature":
                if from_unit == to_unit:
                    result = value
                elif from_unit == "c" and to_unit == "f":
                    result = (value * 9 / 5) + 32
                elif from_unit == "f" and to_unit == "c":
                    result = (value - 32) * 5 / 9
                elif from_unit == "c" and to_unit == "k":
                    result = value + 273.15
                elif from_unit == "k" and to_unit == "c":
                    result = value - 273.15
                elif from_unit == "f" and to_unit == "k":
                    result = (value - 32) * 5 / 9 + 273.15
                elif from_unit == "k" and to_unit == "f":
                    result = (value - 273.15) * 9 / 5 + 32
                else:
                    raise ValueError(f"不支持的温度单位转换: {from_unit} -> {to_unit}")

            elif category in conversion_factors:
                if (
                    from_unit not in conversion_factors[category]
                    or to_unit not in conversion_factors[category][from_unit]
                ):
                    raise ValueError(f"不支持的单位: {from_unit} 或 {to_unit}")

                result = value * conversion_factors[category][from_unit][to_unit]

            else:
                raise ValueError(f"不支持的类别: {category}")

            return {
                "original_value": value,
                "from_unit": from_unit.upper(),
                "to_unit": to_unit.upper(),
                "converted_value": round(result, 6),
                "category": category,
            }

        except Exception as e:
            raise RuntimeError(f"单位转换失败: {str(e)}") from e
