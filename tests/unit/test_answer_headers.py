"""知识库回答不得被强加与问题无关的结构化标题"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.documents import Document
import pytest

from app.core.agents.enterprise_assistant import AnswerGenerator, QueryType

INJECTED_HEADERS = ("## 配置和代码示例", "## 代码示例", "## 配置说明")


def _generator(answer: str) -> AnswerGenerator:
    llm = SimpleNamespace(generate_response=AsyncMock(return_value=answer))
    rag = SimpleNamespace(max_context_length=4000, similarity_threshold=0.5, prompt_template="{context}\n{question}")
    return AnswerGenerator(llm, config=SimpleNamespace(rag=rag))


def _md_docs():
    # 结构化 MD 文档，同时含代码与“config”字样，原逻辑会命中“## 配置和代码示例”
    meta = {"document_type": "markdown", "is_structured": True, "has_code": True, "title_hierarchy": ["部署"], "source": "guide.md"}
    return [
        Document(page_content="部署配置 config.yaml 示例\n```yaml\nkey: value\n```", metadata=dict(meta)),
        Document(page_content="平台 config 参数说明", metadata=dict(meta)),
    ]


@pytest.mark.parametrize("answer", ["AI-CloudOps 是一个智能运维平台，提供负载预测与根因分析。", "1. 打开控制台\n2. 选择集群"])
async def test_md_answer_is_not_prefixed_with_unrelated_header(answer):
    result = await _generator(answer).generate("介绍一下平台", _md_docs(), QueryType.CONCEPTUAL)
    text = result["answer"]
    assert not text.lstrip().startswith("#")
    assert not any(header in text for header in INJECTED_HEADERS)
    assert "智能运维平台" in text or "打开控制台" in text


async def test_md_answer_keeps_model_own_heading():
    answer = "## 部署步骤\n\n先准备 config.yaml"
    result = await _generator(answer).generate("如何部署", _md_docs(), QueryType.TUTORIAL)
    assert result["answer"].startswith("## 部署步骤")
    assert not any(header in result["answer"] for header in INJECTED_HEADERS)
