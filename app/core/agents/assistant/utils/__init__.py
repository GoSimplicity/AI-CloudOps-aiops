"""
工具模块
"""

from .helpers import (
    _build_context_with_history,
    _check_hallucination_advanced,
    _evaluate_doc_relevance_advanced,
    _filter_relevant_docs_advanced,
    _generate_fallback_answer,
    is_test_environment,
)
from .task_manager import TaskManager, create_safe_task, get_task_manager

__all__ = [
    "TaskManager",
    "create_safe_task",
    "get_task_manager",
    "is_test_environment",
    "_generate_fallback_answer",
    "_check_hallucination_advanced",
    "_evaluate_doc_relevance_advanced",
    "_build_context_with_history",
    "_filter_relevant_docs_advanced",
]
