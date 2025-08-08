"""
工具模块
"""

from .task_manager import TaskManager, create_safe_task, get_task_manager
from .helpers import (
    is_test_environment,
    _generate_fallback_answer,
    _check_hallucination_advanced,
    _evaluate_doc_relevance_advanced,
    _build_context_with_history,
    _filter_relevant_docs_advanced,
)

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
