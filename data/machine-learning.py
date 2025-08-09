#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 机器学习模块 - 提供负载预测和机器学习模型功能
"""

import os
import json
import pandas as pd
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
import joblib
from datetime import datetime, timezone, timedelta
import matplotlib.pyplot as plt

# 基本设置
os.makedirs("models", exist_ok=True)

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))
MODEL_PATH = "models/time_qps_auto_scaling_model.pkl"
SCALER_PATH = "models/time_qps_auto_scaling_scaler.pkl"
METADATA_PATH = "models/time_qps_auto_scaling_model_metadata.json"
CSV_PATH = "data.csv"


def load_real_data():
    """加载真实数据"""
    print("正在加载真实数据...")

    try:
        # 尝试加载CSV文件
        if os.path.exists(CSV_PATH):
            df = pd.read_csv(CSV_PATH)

            # 确保时间戳列存在
            if "timestamp" not in df.columns:
                print("错误: 数据集缺少timestamp列")
                return None

            # 确保QPS和实例数列存在
            if "QPS" not in df.columns or "instances" not in df.columns:
                print("错误: 数据集缺少QPS或instances列")
                return None

            # 将时间戳转换为datetime格式
            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                try:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                except Exception:
                    print("警告: 无法解析时间戳字段，尝试使用默认格式...")

            # 检查数据是否有空值
            if df["QPS"].isnull().any() or df["instances"].isnull().any():
                print("警告: 数据包含空值，正在清理...")
                df = df.dropna(subset=["QPS", "instances"])

            # 基本数据验证
            if len(df) < 100:
                print(f"警告: 数据量较少 ({len(df)} 条)，可能导致模型性能不佳")

            # 检查并处理异常值
            qps_mean = df["QPS"].mean()
            qps_std = df["QPS"].std()
            outlier_mask = (df["QPS"] > qps_mean + 5 * qps_std) | (df["QPS"] < 0)
            if outlier_mask.any():
                print(
                    f"警告: 检测到 {outlier_mask.sum()} 个QPS异常值，将被限制在合理范围内"
                )
                df.loc[df["QPS"] < 0, "QPS"] = 0
                df.loc[df["QPS"] > qps_mean + 5 * qps_std, "QPS"] = (
                    qps_mean + 5 * qps_std
                )

            # 确保实例数是正整数
            if (df["instances"] < 1).any():
                print("警告: 发现实例数小于1，将设置为最小值1")
                df.loc[df["instances"] < 1, "instances"] = 1

            # 将实例数转为整数
            df["instances"] = df["instances"].round().astype(int)

            print(f"成功加载了 {len(df)} 条数据")
            return df
        else:
            print(f"错误: 数据文件 {CSV_PATH} 不存在")
            return None
    except Exception as e:
        print(f"加载数据时出错: {str(e)}")
        return None


def extract_features(df):
    """从数据集提取训练特征"""
    if df is None or len(df) == 0:
        print("错误: 无法从空数据集提取特征")
        return None, None

    try:
        print("正在提取特征...")

        # 提取时间特征
        df["hour"] = df["timestamp"].dt.hour
        df["day_of_week"] = df["timestamp"].dt.dayofweek  # 0是周一，6是周日
        df["month"] = df["timestamp"].dt.month
        df["day"] = df["timestamp"].dt.day
        df["is_weekend"] = df["day_of_week"].isin([5, 6]).astype(int)
        df["is_business_hour"] = (
            (df["hour"] >= 9) & (df["hour"] <= 17) & (df["is_weekend"] == 0)
        ).astype(int)

        # 创建周期性特征
        df["sin_time"] = np.sin(2 * np.pi * df["hour"] / 24)
        df["cos_time"] = np.cos(2 * np.pi * df["hour"] / 24)
        df["sin_day"] = np.sin(2 * np.pi * df["day_of_week"] / 7)
        df["cos_day"] = np.cos(2 * np.pi * df["day_of_week"] / 7)

        # 为每个时间点添加历史QPS数据
        df = df.sort_values("timestamp")

        # 添加滞后特征（前一小时，前一天，前一周）
        # 注意: 实际使用时，确保数据已按时间排序
        df["QPS_1h_ago"] = df["QPS"].shift(1)  # 假设数据点间隔为1小时
        df["QPS_1d_ago"] = df["QPS"].shift(24)  # 24小时前
        df["QPS_1w_ago"] = df["QPS"].shift(24 * 7)  # 一周前

        # 计算变化率
        df["QPS_change"] = df["QPS"].pct_change().fillna(0)

        # 计算移动平均
        df["QPS_avg_6h"] = df["QPS"].rolling(window=6).mean().fillna(df["QPS"])

        # 删除包含NaN的行
        df = df.dropna()

        # 选择特征和目标变量
        features = df[
            [
                "QPS",
                "sin_time",
                "cos_time",
                "sin_day",
                "cos_day",
                "is_business_hour",
                "is_weekend",
                "QPS_1h_ago",
                "QPS_1d_ago",
                "QPS_1w_ago",
                "QPS_change",
                "QPS_avg_6h",
            ]
        ]

        target = df["instances"]

        print(f"提取了 {len(features)} 条训练数据，包含 {len(features.columns)} 个特征")
        return features, target
    except Exception as e:
        print(f"提取特征时出错: {str(e)}")
        return None, None


def train_model():
    """训练和评估预测模型"""
    print("开始训练模型...")

    # 加载真实数据
    df = load_real_data()
    if df is None:
        print("错误: 无法加载数据，模型训练终止")
        return False

    # 提取特征
    features, target = extract_features(df)
    if features is None or target is None:
        print("错误: 特征提取失败，模型训练终止")
        return False

    # 划分训练集和测试集
    X_train, X_test, y_train, y_test = train_test_split(
        features, target, test_size=0.2, random_state=42
    )

    print(f"训练集: {X_train.shape[0]} 样本, 测试集: {X_test.shape[0]} 样本")

    # 标准化特征
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # 定义模型列表
    models = {
        "Ridge回归": Ridge(),
        "随机森林回归": RandomForestRegressor(random_state=42),
        "梯度提升回归": GradientBoostingRegressor(random_state=42),
    }

    # 定义参数网格
    param_grids = {
        "Ridge回归": {"alpha": [0.1, 1.0, 10.0]},
        "随机森林回归": {
            "n_estimators": [50, 100],
            "max_depth": [None, 10, 20],
            "min_samples_split": [2, 5],
        },
        "梯度提升回归": {
            "n_estimators": [50, 100],
            "learning_rate": [0.01, 0.1],
            "max_depth": [3, 5],
        },
    }

    best_model = None
    best_score = float("-inf")
    best_name = None

    # 模型训练与评估
    for name, model in models.items():
        print(f"\n训练模型: {name}")

        # 网格搜索最佳参数
        grid_search = GridSearchCV(
            model, param_grids[name], cv=5, scoring="neg_mean_squared_error", n_jobs=-1
        )
        grid_search.fit(X_train_scaled, y_train)

        # 获取最佳模型
        best_params = grid_search.best_params_
        model = grid_search.best_estimator_
        print(f"最佳参数: {best_params}")

        # 在测试集上评估
        y_pred = model.predict(X_test_scaled)

        # 计算性能指标
        mse = mean_squared_error(y_test, y_pred)
        rmse = np.sqrt(mse)
        mae = mean_absolute_error(y_test, y_pred)
        r2 = r2_score(y_test, y_pred)

        print("性能指标:")
        print(f"  MSE: {mse:.4f}")
        print(f"  RMSE: {rmse:.4f}")
        print(f"  MAE: {mae:.4f}")
        print(f"  R²: {r2:.4f}")

        # 更新最佳模型
        if r2 > best_score:
            best_model = model
            best_score = r2
            best_name = name

    if best_model is not None:
        print(f"\n选择最佳模型: {best_name} (R² = {best_score:.4f})")

        # 保存模型和标准化器
        joblib.dump(best_model, MODEL_PATH)
        joblib.dump(scaler, SCALER_PATH)
        print(f"模型已保存到 {MODEL_PATH}")
        print(f"标准化器已保存到 {SCALER_PATH}")

        # 保存模型元数据
        model_metadata = {
            "version": "v1.0.0",
            "author": "Bamboo",
            "created_at": datetime.now(BEIJING_TZ).isoformat(),
            "features": list(features.columns),
            "target": "instances",
            "algorithm": best_name,
            "performance": {"r2": best_score, "rmse": rmse, "mae": mae},
            "parameters": str(best_model.get_params()),
            "data_stats": {
                "n_samples": len(df),
                "mean_qps": float(df["QPS"].mean()),
                "std_qps": float(df["QPS"].std()),
                "min_qps": float(df["QPS"].min()),
                "max_qps": float(df["QPS"].max()),
                "mean_instances": float(df["instances"].mean()),
                "min_instances": int(df["instances"].min()),
                "max_instances": int(df["instances"].max()),
            },
        }

        with open(METADATA_PATH, "w") as f:
            json.dump(model_metadata, f, indent=2)
        print(f"模型元数据已保存到 {METADATA_PATH}")

        # 可视化实际值与预测值的对比
        y_pred_train = best_model.predict(X_train_scaled)
        y_pred_test = best_model.predict(X_test_scaled)

        plt.figure(figsize=(15, 10))

        # 训练集对比图
        plt.subplot(2, 2, 1)
        plt.scatter(y_train, y_pred_train, alpha=0.5)
        plt.plot([y_train.min(), y_train.max()], [y_train.min(), y_train.max()], "r--")
        plt.xlabel("Actual Instances")
        plt.ylabel("Predicted Instances")
        plt.title("Training Set: Actual vs Predicted")

        # 测试集对比图
        plt.subplot(2, 2, 2)
        plt.scatter(y_test, y_pred_test, alpha=0.5)
        plt.plot([y_test.min(), y_test.max()], [y_test.min(), y_test.max()], "r--")
        plt.xlabel("Actual Instances")
        plt.ylabel("Predicted Instances")
        plt.title("Test Set: Actual vs Predicted")

        # QPS 与实例数关系图
        plt.subplot(2, 2, 3)
        plt.scatter(df["QPS"], df["instances"], alpha=0.5)
        plt.xlabel("QPS")
        plt.ylabel("Instances")
        plt.title("QPS vs Instance Count")
        plt.grid(True)

        # 预测误差分布图
        plt.subplot(2, 2, 4)
        errors = y_test - y_pred_test
        plt.hist(errors, bins=20)
        plt.xlabel("Prediction Error")
        plt.ylabel("Frequency")
        plt.title("Prediction Error Distribution")

        plt.tight_layout()
        plt.savefig("models/prediction_results.png")
        print("模型评估结果已保存为图像")

        # 额外创建QPS和实例数的可视化
        plt.figure(figsize=(12, 6))

        # 选择一部分时间段的数据进行可视化
        time_series_df = df.sort_values("timestamp").reset_index(drop=True)
        sample_size = min(1000, len(time_series_df))
        sample_df = time_series_df.iloc[:sample_size]

        plt.plot(sample_df.index, sample_df["QPS"], "b-", label="QPS")
        plt.plot(
            sample_df.index, sample_df["instances"] * 10, "r-", label="Instances x 10"
        )
        plt.xlabel("Time Index")
        plt.ylabel("Value")
        plt.title("QPS and Instances Over Time")
        plt.legend()
        plt.grid(True)

        plt.savefig("models/qps_instances_visualization.png")
        print("QPS与实例数可视化已保存")

        return True
    else:
        print("错误: 未能找到合适的模型")
        return False


if __name__ == "__main__":
    # 训练模型
    success = train_model()
    if success:
        print("模型训练成功！")
    else:
        print("模型训练失败！")
