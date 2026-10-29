#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps模型管理器 - 管理多种预测模型的加载和使用
"""

from datetime import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional
import warnings

import joblib
import numpy as np

# 禁用sklearn特征名称警告
warnings.filterwarnings("ignore", message="X does not have valid feature names")

from app.config.settings import config
from app.models import ModelInfo, PredictionType

logger = logging.getLogger("aiops.core.model_manager")


class ModelManager:
    """模型管理器"""

    def __init__(self):
        self.models = {}
        self.scalers = {}
        self.metadata = {}
        self.models_loaded = False
        self.model_paths = self._init_model_paths()

    def _init_model_paths(self) -> Dict[PredictionType, Dict[str, str]]:
        """初始化模型路径配置"""

        # 从配置文件获取模型路径
        model_paths_config = config.prediction.model_paths
        result = {}

        for pred_type in PredictionType:
            pred_type_key = pred_type.value.lower()
            if pred_type_key in model_paths_config:
                result[pred_type] = model_paths_config[pred_type_key]
            else:
                # 如果配置中没有，使用默认路径
                base_path = config.prediction.model_base_path
                result[pred_type] = {
                    "model": os.path.join(
                        base_path, f"{pred_type_key}_prediction_model.pkl"
                    ),
                    "scaler": os.path.join(
                        base_path, f"{pred_type_key}_prediction_scaler.pkl"
                    ),
                    "metadata": os.path.join(
                        base_path, f"{pred_type_key}_prediction_model_metadata.json"
                    ),
                }

        return result

    async def initialize(self):
        """初始化模型管理器"""
        await self.load_models()

    async def load_models(self) -> bool:
        """加载所有模型"""

        success_count = 0

        for pred_type, paths in self.model_paths.items():
            try:
                # 首先尝试加载专用模型
                if self._load_model_for_type(pred_type, paths):
                    success_count += 1
                    logger.info(f"成功加载{pred_type.value}预测模型")
                else:
                    # 如果专用模型不存在，使用默认QPS模型
                    if pred_type != PredictionType.QPS:
                        logger.warning(f"{pred_type.value}专用模型不存在，使用默认模型")
                        if self._use_default_model(pred_type):
                            success_count += 1

            except Exception as e:
                logger.error(f"加载{pred_type.value}模型失败: {str(e)}")
                # 尝试使用默认模型
                if self._use_default_model(pred_type):
                    success_count += 1

        self.models_loaded = success_count > 0

        if self.models_loaded:
            logger.info(f"模型管理器初始化完成，成功加载{success_count}个模型")
        else:
            logger.error("模型管理器初始化失败，无可用模型")

        return self.models_loaded

    def _load_model_for_type(
        self, pred_type: PredictionType, paths: Dict[str, str]
    ) -> bool:
        """加载特定类型的模型"""

        model_path = paths["model"]
        scaler_path = paths["scaler"]
        metadata_path = paths["metadata"]

        # 检查文件是否存在
        if not os.path.exists(model_path):
            return False

        try:
            # 加载模型
            model = joblib.load(model_path)
            self.models[pred_type] = model

            # 加载标准化器
            if os.path.exists(scaler_path):
                scaler = joblib.load(scaler_path)
                self.scalers[pred_type] = scaler
            else:
                logger.warning(f"{pred_type.value}标准化器不存在，将使用默认标准化")
                self.scalers[pred_type] = None

            # 加载元数据
            if os.path.exists(metadata_path):
                with open(metadata_path, "r") as f:
                    metadata = json.load(f)
                    self.metadata[pred_type] = metadata
            else:
                # 创建默认元数据
                self.metadata[pred_type] = self._create_default_metadata(pred_type)

            # 验证模型
            if self._validate_model(model, self.scalers[pred_type], pred_type):
                return True
            else:
                logger.error(f"{pred_type.value}模型验证失败")
                # 清理加载的内容
                self.models.pop(pred_type, None)
                self.scalers.pop(pred_type, None)
                self.metadata.pop(pred_type, None)
                return False

        except Exception as e:
            logger.error(f"加载{pred_type.value}模型时出错: {str(e)}")
            return False

    def _use_default_model(self, pred_type: PredictionType) -> bool:
        """使用默认模型（QPS模型）"""

        # 如果QPS模型已加载，复用它
        if PredictionType.QPS in self.models:
            self.models[pred_type] = self.models[PredictionType.QPS]
            self.scalers[pred_type] = self.scalers.get(PredictionType.QPS)

            # 复制并调整元数据
            if PredictionType.QPS in self.metadata:
                metadata = self.metadata[PredictionType.QPS].copy()
                metadata["adapted_for"] = pred_type.value
                self.metadata[pred_type] = metadata
            else:
                self.metadata[pred_type] = self._create_default_metadata(pred_type)

            return True

        # 尝试加载默认模型
        default_paths = self.model_paths[PredictionType.QPS]
        if self._load_model_for_type(pred_type, default_paths):
            self.metadata[pred_type]["adapted_from"] = "QPS model"
            return True

        return False

    def _validate_model(
        self, model: Any, scaler: Any, pred_type: PredictionType
    ) -> bool:
        """验证模型有效性"""

        try:
            # 创建测试特征
            test_features = self._create_test_features(pred_type)

            # 如果有标准化器，先标准化
            if scaler is not None:
                test_features = scaler.transform(test_features)

            # 执行预测
            prediction = model.predict(test_features)

            # 验证预测结果
            if prediction is None or len(prediction) == 0:
                return False

            # 检查预测值范围
            pred_value = prediction[0]
            if pred_type == PredictionType.QPS:
                # QPS应该是正数
                if pred_value < 0:
                    return False
            elif pred_type in [
                PredictionType.CPU,
                PredictionType.MEMORY,
                PredictionType.DISK,
            ]:
                # 百分比应该在0-100之间（允许一定的超出）
                if pred_value < -10 or pred_value > 110:
                    return False

            return True

        except Exception as e:
            logger.error(f"模型验证失败: {str(e)}")
            return False

    def _create_test_features(self, pred_type: PredictionType) -> np.ndarray:
        """创建测试特征"""

        # 根据类型创建相应的测试特征
        if pred_type == PredictionType.QPS:
            # QPS模型特征
            features = {
                "QPS": [100.0],
                "sin_time": [0.5],
                "cos_time": [0.8],
                "sin_day": [0.7],
                "cos_day": [0.7],
                "is_business_hour": [1],
                "is_weekend": [0],
                "is_holiday": [0],
                "QPS_1h_ago": [95.0],
                "QPS_1d_ago": [105.0],
                "QPS_1w_ago": [100.0],
                "QPS_change": [0.05],
                "QPS_avg_6h": [98.0],
                "QPS_std_6h": [2.0],
            }
        elif pred_type == PredictionType.CPU:
            # CPU模型特征
            features = {
                "CPU": [50.0],
                "sin_time": [0.5],
                "cos_time": [0.8],
                "sin_day": [0.7],
                "cos_day": [0.7],
                "is_business_hour": [1],
                "is_weekend": [0],
                "is_holiday": [0],
                "CPU_1h_ago": [48.0],
                "CPU_1d_ago": [52.0],
                "CPU_1w_ago": [50.0],
                "CPU_change": [0.04],
                "CPU_avg_6h": [49.0],
                "CPU_std_6h": [2.0],
                "CPU_max_6h": [55.0],
            }
        elif pred_type == PredictionType.MEMORY:
            # 内存模型特征
            features = {
                "MEMORY": [60.0],
                "sin_time": [0.5],
                "cos_time": [0.8],
                "sin_day": [0.7],
                "cos_day": [0.7],
                "is_business_hour": [1],
                "is_weekend": [0],
                "is_holiday": [0],
                "MEMORY_1h_ago": [58.0],
                "MEMORY_1d_ago": [62.0],
                "MEMORY_1w_ago": [60.0],
                "MEMORY_change": [0.03],
                "MEMORY_avg_6h": [59.0],
                "MEMORY_trend": [0.01],
                "MEMORY_min_6h": [55.0],
            }
        else:  # DISK
            # 磁盘模型特征
            features = {
                "DISK": [70.0],
                "sin_time": [0.5],
                "cos_time": [0.8],
                "sin_day": [0.7],
                "cos_day": [0.7],
                "is_business_hour": [1],
                "is_weekend": [0],
                "is_holiday": [0],
                "DISK_1h_ago": [69.5],
                "DISK_1d_ago": [69.0],
                "DISK_1w_ago": [65.0],
                "DISK_change": [0.01],
                "DISK_avg_24h": [69.0],
                "DISK_growth_rate": [0.005],
                "DISK_max_24h": [75.0],
            }

        # 转换为numpy数组
        import pandas as pd

        df = pd.DataFrame(features)
        return df.values

    def _create_default_metadata(self, pred_type: PredictionType) -> Dict[str, Any]:
        """创建默认元数据"""

        return {
            "model_name": f"{pred_type.value}_prediction_model",
            "model_version": None,
            "metadata_missing": True,
            "model_type": "unknown",
            "created_at": datetime.now().isoformat(),
            "features": self._get_default_features(pred_type),
            "target": pred_type.value,
            "algorithm": "default",
            "performance": {},
        }

    def _get_default_features(self, pred_type: PredictionType) -> List[str]:
        """获取默认特征列表"""

        base_features = [
            "sin_time",
            "cos_time",
            "sin_day",
            "cos_day",
            "is_business_hour",
            "is_weekend",
            "is_holiday",
        ]

        if pred_type == PredictionType.QPS:
            return base_features + [
                "QPS",
                "QPS_1h_ago",
                "QPS_1d_ago",
                "QPS_1w_ago",
                "QPS_change",
                "QPS_avg_6h",
            ]
        elif pred_type == PredictionType.CPU:
            return base_features + [
                "CPU",
                "CPU_1h_ago",
                "CPU_1d_ago",
                "CPU_1w_ago",
                "CPU_change",
                "CPU_avg_6h",
                "CPU_std_6h",
            ]
        elif pred_type == PredictionType.MEMORY:
            return base_features + [
                "MEMORY",
                "MEMORY_1h_ago",
                "MEMORY_1d_ago",
                "MEMORY_1w_ago",
                "MEMORY_change",
                "MEMORY_avg_6h",
                "MEMORY_trend",
            ]
        else:  # DISK
            return base_features + [
                "DISK",
                "DISK_1h_ago",
                "DISK_1d_ago",
                "DISK_1w_ago",
                "DISK_change",
                "DISK_avg_24h",
                "DISK_growth_rate",
            ]

    def get_model(self, pred_type: PredictionType) -> Optional[Any]:
        """获取指定类型的模型"""
        return self.models.get(pred_type)

    def get_scaler(self, pred_type: PredictionType) -> Optional[Any]:
        """获取指定类型的标准化器"""
        return self.scalers.get(pred_type)

    def get_metadata(self, pred_type: PredictionType) -> Dict[str, Any]:
        """获取指定类型的元数据"""
        return self.metadata.get(pred_type, {})

    async def get_models_info(self) -> List[ModelInfo]:
        """获取所有模型信息"""

        models_info = []

        for pred_type in PredictionType:
            if pred_type in self.models:
                metadata = self.metadata.get(pred_type, {})

                info = ModelInfo(
                    model_name=metadata.get("model_name", f"{pred_type.value}_model"),
                    model_version=metadata.get("model_version", "1.0"),
                    model_type=metadata.get("algorithm", "unknown"),
                    supported_prediction_types=[pred_type],
                    training_data_size=metadata.get("data_stats", {}).get("n_samples"),
                    last_trained=metadata.get("created_at"),
                    accuracy_metrics=metadata.get("performance", {}),
                    feature_importance=metadata.get("feature_importance", {}),
                )

                models_info.append(info)

        return models_info

    async def get_detailed_info(self) -> Dict[str, Any]:
        """获取详细的模型信息"""

        info = {
            "models": [],
            "total_models": len(self.models),
            "models_loaded": self.models_loaded,
            "supported_types": [t.value for t in self.models.keys()],
        }

        for pred_type, model in self.models.items():
            metadata = self.metadata.get(pred_type, {})

            model_info = {
                "type": pred_type.value,
                "name": metadata.get("model_name", "unknown"),
                "version": metadata.get("model_version", "1.0"),
                "algorithm": metadata.get("algorithm", "unknown"),
                "features": metadata.get("features", []),
                "performance": metadata.get("performance", {}),
                "created_at": metadata.get("created_at"),
                "has_scaler": pred_type in self.scalers
                and self.scalers[pred_type] is not None,
            }

            # 添加模型特定信息
            if hasattr(model, "get_params"):
                model_info["parameters"] = str(model.get_params())

            info["models"].append(model_info)

        return info

    async def reload_model(self, pred_type: PredictionType) -> bool:
        """重新加载特定类型的模型"""

        try:
            paths = self.model_paths.get(pred_type)
            if not paths:
                logger.error(f"未找到{pred_type.value}的模型路径配置")
                return False

            # 清理旧模型
            self.models.pop(pred_type, None)
            self.scalers.pop(pred_type, None)
            self.metadata.pop(pred_type, None)

            # 重新加载
            success = self._load_model_for_type(pred_type, paths)

            if not success:
                # 尝试使用默认模型
                success = self._use_default_model(pred_type)

            if success:
                logger.info(f"成功重新加载{pred_type.value}模型")
            else:
                logger.error(f"重新加载{pred_type.value}模型失败")

            return success

        except Exception as e:
            logger.error(f"重新加载模型失败: {str(e)}")
            return False

    async def is_healthy(self) -> bool:
        """健康检查"""
        return self.models_loaded and len(self.models) > 0

    def save_model(
        self,
        model: Any,
        scaler: Any,
        metadata: Dict[str, Any],
        pred_type: PredictionType,
    ) -> bool:
        """保存模型（用于模型更新）"""

        try:
            paths = self.model_paths[pred_type]

            # 保存模型
            joblib.dump(model, paths["model"])

            # 保存标准化器
            if scaler is not None:
                joblib.dump(scaler, paths["scaler"])

            # 保存元数据
            with open(paths["metadata"], "w") as f:
                json.dump(metadata, f, indent=2)

            logger.info(f"成功保存{pred_type.value}模型")

            # 更新内存中的模型
            self.models[pred_type] = model
            self.scalers[pred_type] = scaler
            self.metadata[pred_type] = metadata

            return True

        except Exception as e:
            logger.error(f"保存模型失败: {str(e)}")
            return False
