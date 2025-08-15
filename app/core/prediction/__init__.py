#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 预测服务（__init__）
"""

from .model_loader import ModelLoader
from .predictor import PredictionService

__all__ = ["PredictionService", "ModelLoader"]
