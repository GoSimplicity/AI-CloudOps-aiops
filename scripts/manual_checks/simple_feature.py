#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""简单特征名称测试脚本"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

import asyncio
from pathlib import Path
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 捕获sklearn警告
sklearn_warnings = []


def capture_sklearn_warnings(
    message, category=None, filename="", lineno=-1, file=None, line=None
):
    if "feature names" in str(message).lower():
        sklearn_warnings.append(str(message))


warnings.showwarning = capture_sklearn_warnings


async def test_direct_prediction():
    """直接测试预测功能，避免pydantic问题"""
    print("测试特征名称修复...")

    try:
        from app.core.prediction.feature_extractor import FeatureExtractor
        from app.core.prediction.model_manager import ModelManager
        from app.core.prediction.unified_predictor import UnifiedPredictor
        from app.models.predict_models import PredictionType

        # 初始化组件
        model_manager = ModelManager()
        await model_manager.initialize()

        feature_extractor = FeatureExtractor()

        predictor = UnifiedPredictor(model_manager, feature_extractor)
        await predictor.initialize()

        # 测试一次预测
        historical_data = []  # 使用空历史数据

        predictions = await predictor.predict(
            prediction_type=PredictionType.QPS,
            current_value=100.0,
            historical_data=historical_data,
            prediction_hours=3,
            granularity=PredictionType.QPS,
        )

        if predictions:
            print(f"✅ 预测成功，生成 {len(predictions)} 个预测点")
        else:
            print("❌ 预测失败")

        # 检查是否有sklearn警告
        if sklearn_warnings:
            print(f"⚠️  检测到 {len(sklearn_warnings)} 个sklearn特征名称警告:")
            for warning in sklearn_warnings:
                print(f"    {warning}")
        else:
            print("🎉 没有sklearn特征名称警告!")

    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")


if __name__ == "__main__":
    asyncio.run(test_direct_prediction())
