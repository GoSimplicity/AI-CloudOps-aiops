import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario", ["missing_execute", "existing_result", "remote_config"])
def test_kind_runner_rejects_unsafe_inputs_before_docker_or_cluster_access(tmp_path, scenario):
    marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\n: > "$CLOUDOPS_KIND_MARKER"\nexit 99\n')
    docker.chmod(0o700)
    config = tmp_path / "config"
    config.write_text(
        json.dumps(
            {
                "current-context": "kind-cloudops-release",
                "contexts": [{"name": "kind-cloudops-release", "context": {"cluster": "owned", "user": "owned"}}],
                "clusters": [{"name": "owned", "cluster": {"server": "https://example.invalid:6443"}}],
                "users": [{"name": "owned", "user": {}}],
            }
        )
    )
    root = Path(__file__).resolve().parents[2]
    args = [sys.executable, str(root / "scripts/integration/autofix_kind_smoke.py"), "--kubeconfig", str(config)]
    if scenario != "missing_execute":
        args.append("--execute")
    if scenario == "existing_result":
        result = tmp_path / "existing.json"
        result.write_text("preserve")
        args.extend(["--result", str(result)])
    process = subprocess.run(args, env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "CLOUDOPS_KIND_MARKER": str(marker)}, capture_output=True, text=True, timeout=10)
    assert process.returncode != 0
    assert not marker.exists()
    if scenario == "remote_config":
        result = json.loads(process.stdout)
        assert result["config_guard"] is False and result["failure_phase"] == "configuration"
    if scenario == "existing_result":
        assert (tmp_path / "existing.json").read_text() == "preserve"
