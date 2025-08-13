#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
from __future__ import annotations

from typing import Dict, List, Tuple


class TopologyGraph:
    """简单的有向图表示拓扑关系。"""

    def __init__(self):
        self.nodes: Dict[str, Dict] = {}
        self.edges: List[Tuple[str, str, str]] = []  # (from, to, relation)
        self._out: Dict[str, List[str]] = {}
        self._in: Dict[str, List[str]] = {}

    def add_node(self, key: str, attrs: Dict):
        self.nodes[key] = attrs

    def add_edge(self, src: str, dst: str, relation: str):
        self.edges.append((src, dst, relation))
        self._out.setdefault(src, []).append(dst)
        self._in.setdefault(dst, []).append(src)

    def to_dict(self) -> Dict:
        return {"nodes": self.nodes, "edges": self.edges}

    def neighbors(self, node: str, direction: str = "out") -> List[str]:
        if direction == "in":
            return list(self._in.get(node, []))
        return list(self._out.get(node, []))

    def reachable(
        self, sources: List[str], max_hops: int = 1, direction: str = "out"
    ) -> List[str]:
        """从 sources 出发在给定方向(BFS)内可达的节点集合（不含源）。"""
        visited = set(sources)
        frontier = list(sources)
        hops = 0
        result: List[str] = []
        while frontier and hops < max_hops:
            next_frontier: List[str] = []
            for u in frontier:
                for v in self.neighbors(u, direction=direction):
                    if v not in visited:
                        visited.add(v)
                        result.append(v)
                        next_frontier.append(v)
            frontier = next_frontier
            hops += 1
        return result


def _matches_selector(pod: Dict, selector: Dict[str, str]) -> bool:
    if not selector:
        return False
    labels = ((pod or {}).get("metadata", {}) or {}).get("labels", {}) or {}
    for k, v in (selector or {}).items():
        if labels.get(k) != v:
            return False
    return True


def build_topology_from_state(state_snapshot: Dict) -> TopologyGraph:
    """依据状态快照推导基础拓扑：Service -> Pod，Deployment -> Pod。"""
    g = TopologyGraph()
    ns = state_snapshot.get("namespace")

    pods = state_snapshot.get("pods", []) or []
    svcs = state_snapshot.get("services", []) or []
    deps = state_snapshot.get("deployments", []) or []

    # 节点：Pod
    for pod in pods:
        name = ((pod or {}).get("metadata", {}) or {}).get("name")
        if name:
            g.add_node(f"pod:{ns}/{name}", {"type": "pod"})

    # 节点：Service 与连边：Service -> Pod（基于 selector 匹配）
    for svc in svcs:
        sname = ((svc or {}).get("metadata", {}) or {}).get("name")
        selector = ((svc or {}).get("spec", {}) or {}).get("selector", {}) or {}
        if sname:
            skey = f"svc:{ns}/{sname}"
            g.add_node(skey, {"type": "service"})
            if selector:
                for pod in pods:
                    pname = ((pod or {}).get("metadata", {}) or {}).get("name")
                    if pname and _matches_selector(pod, selector):
                        g.add_edge(skey, f"pod:{ns}/{pname}", "selects")

    # 节点：Deployment 与连边：Deployment -> Pod（基于 matchLabels 匹配）
    for dep in deps:
        dname = ((dep or {}).get("metadata", {}) or {}).get("name")
        sel = (((dep or {}).get("spec", {}) or {}).get("selector", {}) or {}).get(
            "matchLabels", {}
        ) or {}
        if dname:
            dkey = f"deploy:{ns}/{dname}"
            g.add_node(dkey, {"type": "deployment"})
            if sel:
                for pod in pods:
                    pname = ((pod or {}).get("metadata", {}) or {}).get("name")
                    if pname and _matches_selector(pod, sel):
                        g.add_edge(dkey, f"pod:{ns}/{pname}", "manages")

    return g
