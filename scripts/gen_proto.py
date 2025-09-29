#!/usr/bin/env python3
"""
生成 Protocol Buffer 代码的 Python 脚本
"""

import os
import subprocess
import sys
from pathlib import Path

def run_command(cmd, cwd=None):
    """运行命令并检查结果"""
    print(f"运行命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"错误: {result.stderr}")
        sys.exit(1)
    print(f"成功: {result.stdout}")
    return result.stdout

def main():
    # 获取项目根目录
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    proto_dir = project_root / "proto"
    
    print(f"项目根目录: {project_root}")
    print(f"Proto 目录: {proto_dir}")
    
    # 检查 proto 文件是否存在
    proto_file = proto_dir / "aiops" / "v1" / "aiops_core.proto"
    if not proto_file.exists():
        print(f"错误: Proto 文件不存在: {proto_file}")
        sys.exit(1)
    
    # 检查依赖
    try:
        import grpc_tools
        print("grpc-tools 已安装")
    except ImportError:
        print("错误: grpc-tools 未安装，请运行: pip install grpcio-tools")
        sys.exit(1)
    
    # 创建输出目录
    proto_output_dir = project_root / "proto"
    proto_output_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成 Python gRPC 代码
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={proto_dir}",
        f"--python_out={project_root}",
        f"--grpc_python_out={project_root}",
        str(proto_file)
    ]
    
    run_command(cmd, cwd=project_root)
    
    # 验证生成的文件
    generated_files = [
        proto_output_dir / "aiops" / "v1" / "aiops_core_pb2.py",
        proto_output_dir / "aiops" / "v1" / "aiops_core_pb2_grpc.py"
    ]
    
    print("\n验证生成的文件:")
    for file_path in generated_files:
        if file_path.exists():
            print(f"✅ {file_path}")
        else:
            print(f"❌ {file_path} - 文件未生成")
            sys.exit(1)
    
    # 创建 __init__.py 文件
    init_files = [
        proto_output_dir / "__init__.py",
        proto_output_dir / "aiops" / "__init__.py", 
        proto_output_dir / "aiops" / "v1" / "__init__.py"
    ]
    
    for init_file in init_files:
        init_file.parent.mkdir(parents=True, exist_ok=True)
        if not init_file.exists():
            init_file.write_text("# Auto-generated __init__.py\n")
            print(f"创建: {init_file}")
    
    print("\n🎉 Protocol Buffer 代码生成完成!")

if __name__ == "__main__":
    main()
