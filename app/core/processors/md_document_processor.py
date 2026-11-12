#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: MD文档专用处理器
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# 延迟导入元数据增强器
try:
    from .md_metadata_enhancer import EnhancedMetadata, MDMetadataEnhancer

    METADATA_ENHANCER_AVAILABLE = True
except ImportError:
    METADATA_ENHANCER_AVAILABLE = False

logger = logging.getLogger("aiops.md_processor")


class MDElementType(Enum):
    """MD文档元素类型"""

    TITLE = "title"
    PARAGRAPH = "paragraph"
    CODE_BLOCK = "code_block"
    LIST = "list"
    TABLE = "table"
    QUOTE = "quote"
    LINK = "link"
    IMAGE = "image"


@dataclass
class MDElement:
    """MD文档元素"""

    element_type: MDElementType
    content: str
    level: int = 0  # 标题级别或嵌套级别
    language: Optional[str] = None  # 代码块语言
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_line: int = 0
    end_line: int = 0


@dataclass
class MDChunk:
    """MD文档块"""

    chunk_id: str
    content: str
    title_hierarchy: List[str] = field(default_factory=list)  # 标题层级路径
    elements: List[MDElement] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    semantic_weight: float = 1.0  # 语义权重
    structural_weight: float = 1.0  # 结构权重


class MDDocumentProcessor:
    """MD文档专用处理器"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

        # 分块配置
        self.max_chunk_size = self.config.get("max_chunk_size", 800)
        self.chunk_overlap = self.config.get("chunk_overlap", 100)
        self.preserve_structure = self.config.get("preserve_structure", True)
        self.enable_metadata_enhancement = self.config.get(
            "enable_metadata_enhancement", True
        )

        # 性能优化配置
        self.enable_parallel_processing = self.config.get(
            "enable_parallel_processing", True
        )
        self.max_workers = self.config.get("max_workers", 2)
        self.chunk_cache_size = self.config.get("chunk_cache_size", 100)
        self.enable_caching = self.config.get("enable_caching", True)

        # 缓存管理
        self._chunk_cache = {} if self.enable_caching else None
        self._pattern_cache = {}

        # 线程池（仅在需要时创建）
        self._executor = None
        if self.enable_parallel_processing:
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers)

        # 统计信息
        self.stats = {
            "documents_processed": 0,
            "chunks_generated": 0,
            "processing_time_total": 0.0,
            "average_processing_time": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

        # 权重配置
        self.title_weights = {1: 1.5, 2: 1.3, 3: 1.2, 4: 1.1, 5: 1.05, 6: 1.0}
        self.element_weights = {
            MDElementType.TITLE: 1.4,
            MDElementType.CODE_BLOCK: 1.2,
            MDElementType.LIST: 1.1,
            MDElementType.TABLE: 1.3,
            MDElementType.QUOTE: 1.0,
            MDElementType.PARAGRAPH: 1.0,
        }

        # 元数据增强器
        self.metadata_enhancer = None
        if METADATA_ENHANCER_AVAILABLE and self.enable_metadata_enhancement:
            self.metadata_enhancer = MDMetadataEnhancer()
            logger.info("MD元数据增强器已启用")

        # 编译正则表达式
        self._compile_patterns()

        # 额外的快速匹配模式
        self._compile_fast_patterns()

    def _compile_patterns(self):
        """编译常用的正则表达式"""
        self.patterns = {
            "title": re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE),
            "code_block": re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL),
            "inline_code": re.compile(r"`([^`]+)`"),
            "list_item": re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.+)$", re.MULTILINE),
            "table_row": re.compile(r"\|(.+)\|"),
            "quote": re.compile(r"^>\s*(.+)$", re.MULTILINE),
            "link": re.compile(r"\[([^\]]+)\]\(([^)]+)\)"),
            "image": re.compile(r"!\[([^\]]*)\]\(([^)]+)\)"),
            "bold": re.compile(r"\*\*([^*]+)\*\*"),
            "italic": re.compile(r"\*([^*]+)\*"),
        }

    def _compile_fast_patterns(self):
        """编译快速匹配模式"""
        self.fast_patterns = {
            "has_headers": re.compile(r"^#{1,6}\s+", re.MULTILINE),
            "has_code_blocks": re.compile(r"```"),
            "has_lists": re.compile(r"^(\s*)([-*+]|\d+\.)\s+", re.MULTILINE),
        }

    def parse_document(
        self, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[MDChunk]:
        """解析MD文档为结构化块（带缓存和性能优化）"""
        metadata = metadata or {}
        start_time = time.time()

        # 检查缓存
        if self.enable_caching and self._chunk_cache is not None:
            cache_key = self._get_cache_key(content, str(metadata))
            if cache_key in self._chunk_cache:
                self.stats["cache_hits"] += 1
                logger.debug(f"使用缓存的解析结果: {cache_key[:16]}...")
                return self._chunk_cache[cache_key]
            else:
                self.stats["cache_misses"] += 1

        # 预处理文档
        content = self._preprocess_content(content)

        # 评估文档复杂度并选择处理策略
        complexity = self._assess_document_complexity(content)

        if (
            complexity == "complex"
            and self.enable_parallel_processing
            and self._executor
        ):
            chunks = self._process_document_parallel(content, metadata)
        else:
            chunks = self._process_document_sequential(content, metadata)

        # 更新统计信息
        processing_time = time.time() - start_time
        self._update_stats(len(chunks), processing_time)

        # 缓存结果（如果启用）
        if self.enable_caching and self._chunk_cache is not None:
            cache_key = self._get_cache_key(content, str(metadata))

            # 限制缓存大小
            if len(self._chunk_cache) >= self.chunk_cache_size:
                # 简单的LRU：移除最老的一半条目
                keys_to_remove = list(self._chunk_cache.keys())[
                    : self.chunk_cache_size // 2
                ]
                for key in keys_to_remove:
                    del self._chunk_cache[key]

            self._chunk_cache[cache_key] = chunks

        logger.info(
            f"MD文档解析完成，生成 {len(chunks)} 个块，"
            f"复杂度: {complexity}，耗时: {processing_time:.3f}秒"
        )
        return chunks

    def _preprocess_content(self, content: str) -> str:
        """预处理文档内容"""
        # 标准化换行符
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        # 移除过多的空行
        content = re.sub(r"\n{3,}", "\n\n", content)

        # 标准化标题格式
        content = re.sub(
            r"^(#{1,6})\s*([^#\n]+)\s*#+?\s*$", r"\1 \2", content, flags=re.MULTILINE
        )

        return content.strip()

    def _parse_elements(self, content: str) -> List[MDElement]:
        """解析文档元素"""
        elements = []
        lines = content.split("\n")
        i = 0

        while i < len(lines):
            element, consumed_lines = self._parse_line(lines, i)

            if element:
                element.start_line = i
                element.end_line = i + consumed_lines
                elements.append(element)

            i += max(1, consumed_lines)

        return elements

    def _parse_line(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析单行或多行元素"""
        line = lines[start_idx].strip()

        if not line:
            return None, 1

        # 标题
        title_match = self.patterns["title"].match(lines[start_idx])
        if title_match:
            level = len(title_match.group(1))
            title = title_match.group(2).strip()
            return (
                MDElement(
                    element_type=MDElementType.TITLE,
                    content=title,
                    level=level,
                    metadata={"raw_line": lines[start_idx]},
                ),
                1,
            )

        # 代码块
        if line.startswith("```"):
            return self._parse_code_block(lines, start_idx)

        # 列表项
        list_match = self.patterns["list_item"].match(lines[start_idx])
        if list_match:
            return self._parse_list_item(lines, start_idx)

        # 引用
        if line.startswith(">"):
            return self._parse_quote(lines, start_idx)

        # 表格
        if "|" in line and start_idx < len(lines) - 1:
            next_line = lines[start_idx + 1].strip()
            if "|" in next_line and re.match(r"^[\|\-\s:]+$", next_line):
                return self._parse_table(lines, start_idx)

        # 普通段落
        return self._parse_paragraph(lines, start_idx)

    def _parse_code_block(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析代码块"""
        start_line = lines[start_idx]
        language_match = re.match(r"```(\w+)?", start_line)
        language = (
            language_match.group(1)
            if language_match and language_match.group(1)
            else None
        )

        code_lines = []
        i = start_idx + 1

        while i < len(lines):
            if lines[i].strip() == "```":
                break
            code_lines.append(lines[i])
            i += 1

        if i >= len(lines):
            # 未闭合的代码块
            return None, 1

        content = "\n".join(code_lines)
        return (
            MDElement(
                element_type=MDElementType.CODE_BLOCK,
                content=content,
                language=language,
                metadata={"raw_content": content, "language": language},
            ),
            i - start_idx + 1,
        )

    def _parse_list_item(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析列表项"""
        list_lines = []
        i = start_idx
        base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())

        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            current_indent = len(line) - len(line.lstrip())

            # 检查是否是列表项或延续
            if self.patterns["list_item"].match(line) or (
                current_indent > base_indent and line.strip()
            ):
                list_lines.append(line)
                i += 1
            else:
                break

        content = "\n".join(list_lines)
        return (
            MDElement(
                element_type=MDElementType.LIST,
                content=content,
                metadata={
                    "item_count": len(
                        [line for line in list_lines if self.patterns["list_item"].match(line)]
                    )
                },
            ),
            i - start_idx,
        )

    def _parse_quote(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析引用"""
        quote_lines = []
        i = start_idx

        while i < len(lines):
            line = lines[i]
            if line.strip().startswith(">"):
                quote_lines.append(line.strip()[1:].strip())
                i += 1
            elif not line.strip():
                i += 1
            else:
                break

        content = "\n".join(quote_lines)
        return (
            MDElement(element_type=MDElementType.QUOTE, content=content),
            i - start_idx,
        )

    def _parse_table(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析表格"""
        table_lines = []
        i = start_idx

        while i < len(lines):
            line = lines[i].strip()
            if "|" in line:
                table_lines.append(line)
                i += 1
            elif not line:
                i += 1
            else:
                break

        if len(table_lines) < 2:
            return None, 1

        content = "\n".join(table_lines)

        # 解析表格结构
        headers = [h.strip() for h in table_lines[0].split("|")[1:-1]]
        rows = []
        for line in table_lines[2:]:  # 跳过分隔符行
            if "|" in line:
                row = [cell.strip() for cell in line.split("|")[1:-1]]
                rows.append(row)

        return (
            MDElement(
                element_type=MDElementType.TABLE,
                content=content,
                metadata={
                    "headers": headers,
                    "rows": rows,
                    "column_count": len(headers),
                    "row_count": len(rows),
                },
            ),
            i - start_idx,
        )

    def _parse_paragraph(
        self, lines: List[str], start_idx: int
    ) -> Tuple[Optional[MDElement], int]:
        """解析段落"""
        para_lines = []
        i = start_idx

        while i < len(lines):
            line = lines[i].strip()

            if not line:
                break

            # 检查是否是其他元素的开始
            if (
                line.startswith("#")
                or line.startswith("```")
                or line.startswith(">")
                or self.patterns["list_item"].match(lines[i])
                or ("|" in line and i + 1 < len(lines) and "|" in lines[i + 1])
            ):
                break

            para_lines.append(line)
            i += 1

        if not para_lines:
            return None, 1

        content = " ".join(para_lines)

        # 提取段落中的特殊元素
        links = self.patterns["link"].findall(content)
        images = self.patterns["image"].findall(content)
        inline_codes = self.patterns["inline_code"].findall(content)

        return (
            MDElement(
                element_type=MDElementType.PARAGRAPH,
                content=content,
                metadata={
                    "links": links,
                    "images": images,
                    "inline_codes": inline_codes,
                    "word_count": len(content.split()),
                },
            ),
            i - start_idx,
        )

    def _build_title_hierarchy(self, elements: List[MDElement]) -> Dict[int, List[str]]:
        """构建标题层级映射"""
        hierarchy = {}
        title_stack = [None] * 7  # 支持1-6级标题

        line_num = 0
        for element in elements:
            if element.element_type == MDElementType.TITLE:
                level = element.level
                title_stack[level] = element.content

                # 清除更深层级的标题
                for i in range(level + 1, 7):
                    title_stack[i] = None

                # 构建当前路径
                path = [t for t in title_stack[1 : level + 1] if t is not None]
                hierarchy[line_num] = path

            line_num = element.end_line

        return hierarchy

    def _intelligent_chunking(
        self,
        elements: List[MDElement],
        title_hierarchy: Dict[int, List[str]],
        metadata: Dict[str, Any],
    ) -> List[MDChunk]:
        """智能分块策略"""
        chunks = []
        current_chunk_elements = []
        current_size = 0
        current_title_path = []

        for element in elements:
            element_size = len(element.content)

            # 如果是标题，考虑是否开始新块
            if element.element_type == MDElementType.TITLE:
                # 如果当前块有内容且达到最小大小，创建新块
                if current_chunk_elements and current_size > self.max_chunk_size // 2:
                    chunks.append(
                        self._create_chunk(
                            current_chunk_elements, current_title_path, metadata
                        )
                    )
                    current_chunk_elements = []
                    current_size = 0

                # 更新标题路径
                line_key = max(
                    [k for k in title_hierarchy.keys() if k <= element.start_line],
                    default=0,
                )
                current_title_path = title_hierarchy.get(line_key, [])

            # 检查是否需要分块
            if (
                current_size + element_size > self.max_chunk_size
                and current_chunk_elements
            ):
                # 尝试在合适的位置分割
                split_point = self._find_optimal_split_point(
                    current_chunk_elements, element
                )

                if split_point > 0:
                    # 创建当前块
                    chunk_elements = current_chunk_elements[:split_point]
                    chunks.append(
                        self._create_chunk(chunk_elements, current_title_path, metadata)
                    )

                    # 保留重叠内容
                    overlap_elements = current_chunk_elements[max(0, split_point - 1) :]
                    current_chunk_elements = overlap_elements
                    current_size = sum(len(e.content) for e in overlap_elements)

            current_chunk_elements.append(element)
            current_size += element_size

        # 处理最后一个块
        if current_chunk_elements:
            chunks.append(
                self._create_chunk(current_chunk_elements, current_title_path, metadata)
            )

        return chunks

    def _find_optimal_split_point(
        self, current_elements: List[MDElement], new_element: MDElement
    ) -> int:
        """找到最佳分割点"""
        # 优先在段落结束处分割
        for i in range(len(current_elements) - 1, -1, -1):
            element = current_elements[i]
            if element.element_type == MDElementType.PARAGRAPH:
                return i + 1

        # 其次在列表或引用结束处分割
        for i in range(len(current_elements) - 1, -1, -1):
            element = current_elements[i]
            if element.element_type in [MDElementType.LIST, MDElementType.QUOTE]:
                return i + 1

        # 最后在代码块结束处分割
        for i in range(len(current_elements) - 1, -1, -1):
            element = current_elements[i]
            if element.element_type == MDElementType.CODE_BLOCK:
                return i + 1

        # 默认从中间分割
        return len(current_elements) // 2

    def _create_chunk(
        self,
        elements: List[MDElement],
        title_hierarchy: List[str],
        metadata: Dict[str, Any],
    ) -> MDChunk:
        """创建文档块"""
        # 组合内容
        content_parts = []

        # 添加标题上下文
        if title_hierarchy:
            context = " > ".join(title_hierarchy)
            content_parts.append(f"[上下文: {context}]")

        # 添加元素内容
        for element in elements:
            if element.element_type == MDElementType.TITLE:
                content_parts.append(f"{'#' * element.level} {element.content}")
            elif element.element_type == MDElementType.CODE_BLOCK:
                lang = element.language or ""
                content_parts.append(f"```{lang}\n{element.content}\n```")
            else:
                content_parts.append(element.content)

        content = "\n\n".join(content_parts)

        # 生成块ID
        chunk_id = hashlib.md5(content.encode()).hexdigest()

        # 计算初始权重
        semantic_weight = self._calculate_semantic_weight(elements)
        structural_weight = self._calculate_structural_weight(elements, title_hierarchy)

        # 构建元数据
        chunk_metadata = {
            **metadata,
            "element_types": [e.element_type.value for e in elements],
            "element_count": len(elements),
            "title_hierarchy": title_hierarchy,
            "has_code": any(
                e.element_type == MDElementType.CODE_BLOCK for e in elements
            ),
            "has_table": any(e.element_type == MDElementType.TABLE for e in elements),
            "has_list": any(e.element_type == MDElementType.LIST for e in elements),
            "languages": list(set(e.language for e in elements if e.language)),
        }

        return MDChunk(
            chunk_id=chunk_id,
            content=content,
            title_hierarchy=title_hierarchy,
            elements=elements,
            metadata=chunk_metadata,
            semantic_weight=semantic_weight,
            structural_weight=structural_weight,
        )

    def _calculate_semantic_weight(self, elements: List[MDElement]) -> float:
        """计算语义权重"""
        total_weight = 0.0
        total_length = 0

        for element in elements:
            element_weight = self.element_weights.get(element.element_type, 1.0)

            # 标题额外权重
            if element.element_type == MDElementType.TITLE:
                element_weight *= self.title_weights.get(element.level, 1.0)

            # 代码块根据语言调整权重
            elif element.element_type == MDElementType.CODE_BLOCK:
                important_languages = {"python", "yaml", "bash", "sql", "dockerfile"}
                if element.language and element.language.lower() in important_languages:
                    element_weight *= 1.2

            length = len(element.content)
            total_weight += element_weight * length
            total_length += length

        return total_weight / max(total_length, 1)

    def _calculate_structural_weight(
        self, elements: List[MDElement], title_hierarchy: List[str]
    ) -> float:
        """计算结构权重"""
        # 基于标题层级深度
        depth_weight = 1.0
        if title_hierarchy:
            depth = len(title_hierarchy)
            depth_weight = max(0.8, 1.2 - depth * 0.1)  # 越深层级权重越低

        # 基于内容类型多样性
        element_types = set(e.element_type for e in elements)
        diversity_weight = 1.0 + len(element_types) * 0.05

        # 基于特殊内容
        special_weight = 1.0
        for element in elements:
            if element.element_type == MDElementType.TABLE:
                special_weight += 0.1
            elif element.element_type == MDElementType.CODE_BLOCK:
                special_weight += 0.05

        return depth_weight * diversity_weight * special_weight

    def _calculate_weights_and_metadata(self, chunks: List[MDChunk]):
        """计算并标准化权重和元数据"""
        if not chunks:
            return

        # 提前计算所有权重，避免重复列表生成
        semantic_weights = [chunk.semantic_weight for chunk in chunks]
        structural_weights = [chunk.structural_weight for chunk in chunks]

        # 标准化语义权重
        if semantic_weights:
            min_semantic, max_semantic = min(semantic_weights), max(semantic_weights)
            range_semantic = max_semantic - min_semantic

            if range_semantic > 0:
                for chunk in chunks:
                    chunk.semantic_weight = 0.5 + 0.5 * (
                        (chunk.semantic_weight - min_semantic) / range_semantic
                    )

        # 标准化结构权重
        if structural_weights:
            min_structural, max_structural = (
                min(structural_weights),
                max(structural_weights),
            )
            range_structural = max_structural - min_structural

            if range_structural > 0:
                for chunk in chunks:
                    chunk.structural_weight = 0.5 + 0.5 * (
                        (chunk.structural_weight - min_structural) / range_structural
                    )

        # 添加全局元数据
        for i, chunk in enumerate(chunks):
            chunk.metadata.update(
                {
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "relative_position": i / max(len(chunks) - 1, 1),
                }
            )

    def _enhance_chunks_metadata(
        self,
        chunks: List[MDChunk],
        original_content: str,
        base_metadata: Dict[str, Any],
    ):
        """使用元数据增强器增强块的元数据"""
        if not self.metadata_enhancer:
            return

        logger.debug(f"开始增强 {len(chunks)} 个块的元数据")

        # 为整个文档生成增强元数据
        self.metadata_enhancer.enhance_metadata(original_content, base_metadata)

        # 为每个块增强元数据
        for i, chunk in enumerate(chunks):
            try:
                # 基础元数据
                chunk_basic_metadata = {
                    **chunk.metadata,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                }

                # 块级增强元数据
                chunk_enhanced = self.metadata_enhancer.enhance_metadata(
                    chunk.content, chunk_basic_metadata
                )

                # 合并增强元数据到块元数据中
                enhancement_dict = self.metadata_enhancer.to_dict(chunk_enhanced)

                # 选择性添加重要的增强元数据
                chunk.metadata.update(
                    {
                        # 语义标签
                        "semantic_tags": enhancement_dict.get("semantic_tags", []),
                        "technical_concepts": enhancement_dict.get(
                            "technical_concepts", []
                        ),
                        "content_complexity": enhancement_dict.get(
                            "content_complexity", "simple"
                        ),
                        "technical_domains": enhancement_dict.get(
                            "technical_domains", []
                        ),
                        # 内容模式
                        "content_patterns": enhancement_dict.get(
                            "content_patterns", []
                        ),
                        # 质量指标
                        "readability_score": enhancement_dict.get(
                            "readability_score", 0.0
                        ),
                        "technical_depth": enhancement_dict.get("technical_depth", 0.0),
                        "completeness_score": enhancement_dict.get(
                            "completeness_score", 0.0
                        ),
                        # 关键信息
                        "key_commands": enhancement_dict.get("key_commands", []),
                        "configuration_files": enhancement_dict.get(
                            "configuration_files", []
                        ),
                        "api_endpoints": enhancement_dict.get("api_endpoints", []),
                        "error_patterns": enhancement_dict.get("error_patterns", []),
                        # 统计信息
                        "word_count": enhancement_dict.get("word_count", 0),
                        "enhanced_metadata_version": "1.0",
                    }
                )

                # 根据增强元数据调整权重
                self._adjust_weights_from_enhancement(chunk, chunk_enhanced)

            except Exception as e:
                logger.warning("块 %s 元数据增强失败，异常类型: %s", i, type(e).__name__)

        logger.debug("块元数据增强完成")

    def _adjust_weights_from_enhancement(
        self, chunk: MDChunk, enhanced: "EnhancedMetadata"
    ):
        """根据增强元数据调整权重"""
        try:
            # 基于技术深度调整语义权重
            if enhanced.technical_depth > 0.7:
                chunk.semantic_weight *= 1.2
            elif enhanced.technical_depth > 0.5:
                chunk.semantic_weight *= 1.1

            # 基于完整性调整结构权重
            if enhanced.completeness_score > 0.8:
                chunk.structural_weight *= 1.15
            elif enhanced.completeness_score > 0.6:
                chunk.structural_weight *= 1.05

            # 基于内容复杂度调整权重
            complexity_multipliers = {
                "simple": 1.0,
                "moderate": 1.05,
                "complex": 1.1,
                "expert": 1.15,
            }

            complexity = (
                enhanced.content_complexity.value
                if hasattr(enhanced.content_complexity, "value")
                else str(enhanced.content_complexity)
            )
            multiplier = complexity_multipliers.get(complexity, 1.0)
            chunk.semantic_weight *= multiplier

            # 确保权重在合理范围内
            chunk.semantic_weight = max(0.5, min(chunk.semantic_weight, 2.0))
            chunk.structural_weight = max(0.5, min(chunk.structural_weight, 2.0))

        except Exception as e:
            logger.warning("权重调整失败，异常类型: %s", type(e).__name__)

    def _get_cache_key(self, content: str, metadata_str: str) -> str:
        """生成缓存键"""
        content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        metadata_hash = hashlib.md5(metadata_str.encode()).hexdigest()[:8]
        return f"{content_hash}_{metadata_hash}"

    def clear_cache(self):
        """清理缓存"""
        if self._chunk_cache:
            self._chunk_cache.clear()
        if self._pattern_cache:
            self._pattern_cache.clear()
        logger.info("MD文档处理器缓存已清理")

    def _assess_document_complexity(self, content: str) -> str:
        """快速评估文档复杂度"""
        # 快速指标检查
        char_count = len(content)
        if char_count < 1000:
            return "simple"

        # 检查结构复杂度
        has_headers = bool(self.fast_patterns["has_headers"].search(content))
        has_code_blocks = bool(self.fast_patterns["has_code_blocks"].search(content))
        has_lists = bool(self.fast_patterns["has_lists"].search(content))

        complexity_score = sum([has_headers, has_code_blocks, has_lists])

        if char_count > 5000 or complexity_score >= 2:
            return "complex"
        elif char_count > 2000 or complexity_score >= 1:
            return "moderate"
        else:
            return "simple"

    def _process_document_sequential(
        self, content: str, metadata: Dict[str, Any]
    ) -> List[MDChunk]:
        """顺序处理文档"""
        # 解析文档结构
        elements = self._parse_elements(content)

        # 构建标题层级
        title_hierarchy = self._build_title_hierarchy(elements)

        # 智能分块
        chunks = self._intelligent_chunking(elements, title_hierarchy, metadata)

        # 计算权重和元数据
        self._calculate_weights_and_metadata(chunks)

        # 如果启用了元数据增强器，进行增强处理
        if self.metadata_enhancer:
            self._enhance_chunks_metadata(chunks, content, metadata)

        return chunks

    def _process_document_parallel(
        self, content: str, metadata: Dict[str, Any]
    ) -> List[MDChunk]:
        """并行处理文档"""
        # 解析文档结构
        elements = self._parse_elements(content)

        # 构建标题层级
        title_hierarchy = self._build_title_hierarchy(elements)

        # 智能分块
        chunks = self._intelligent_chunking(elements, title_hierarchy, metadata)

        # 并行计算权重和元数据
        futures = []
        chunk_batches = self._split_chunks_for_parallel(chunks)

        for batch in chunk_batches:
            future = self._executor.submit(self._process_chunk_batch, batch)
            futures.append(future)

        # 等待所有批次完成
        for future in futures:
            future.result()

        # 计算全局权重和元数据
        self._calculate_weights_and_metadata(chunks)

        # 如果启用了元数据增强器，进行增强处理
        if self.metadata_enhancer:
            self._enhance_chunks_metadata(chunks, content, metadata)

        return chunks

    def _split_chunks_for_parallel(self, chunks: List[MDChunk]) -> List[List[MDChunk]]:
        """将块分割为并行处理的批次"""
        if len(chunks) <= self.max_workers:
            return [chunks]

        batch_size = max(1, len(chunks) // self.max_workers)
        batches = []

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            batches.append(batch)

        return batches

    def _process_chunk_batch(self, chunks: List[MDChunk]):
        """处理一个批次的块"""
        for chunk in chunks:
            # 预计算一些可以并行处理的元数据
            chunk.metadata["word_count"] = len(chunk.content.split())
            chunk.metadata["char_count"] = len(chunk.content)
            chunk.metadata["line_count"] = chunk.content.count("\n") + 1

    def _update_stats(self, chunk_count: int, processing_time: float):
        """更新统计信息"""
        self.stats["documents_processed"] += 1
        self.stats["chunks_generated"] += chunk_count
        self.stats["processing_time_total"] += processing_time

        # 计算平均处理时间
        if self.stats["documents_processed"] > 0:
            self.stats["average_processing_time"] = (
                self.stats["processing_time_total"] / self.stats["documents_processed"]
            )

    def get_stats(self) -> Dict[str, Any]:
        """获取处理统计信息"""
        stats = self.stats.copy()
        stats.update(
            {
                "cache_size": len(self._chunk_cache) if self._chunk_cache else 0,
                "max_cache_size": self.chunk_cache_size,
                "parallel_processing_enabled": self.enable_parallel_processing,
                "max_workers": self.max_workers,
            }
        )
        return stats

    def __del__(self):
        """析构函数，清理资源"""
        if hasattr(self, "_executor") and self._executor:
            self._executor.shutdown(wait=False)


class MDEnhancedQueryProcessor:
    """MD文档增强查询处理器"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

        # MD特定的查询模式
        self.md_patterns = {
            "code_query": re.compile(r"代码|代码示例|implementation|code|script|命令"),
            "config_query": re.compile(r"配置|设置|配置文件|config|setting|参数"),
            "tutorial_query": re.compile(
                r"教程|指南|步骤|how\s+to|guide|tutorial|安装|部署"
            ),
            "troubleshoot_query": re.compile(
                r"问题|错误|故障|排查|debug|error|fail|issue"
            ),
            "concept_query": re.compile(
                r"什么是|概念|原理|principle|concept|定义|架构"
            ),
        }

        # 技术领域映射
        self.domain_keywords = {
            "kubernetes": [
                "k8s",
                "kubernetes",
                "pod",
                "deployment",
                "service",
                "kubectl",
            ],
            "docker": ["docker", "container", "image", "dockerfile"],
            "monitoring": ["prometheus", "metric", "alert", "monitor"],
            "networking": ["network", "ip", "port", "proxy", "ingress"],
            "security": ["security", "auth", "rbac", "certificate", "tls"],
        }

    def enhance_query_for_md(
        self, query: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """为MD文档增强查询"""
        enhanced_info = {
            "original_query": query,
            "query_type": "general",
            "md_focus": [],
            "structure_preference": [],
            "enhanced_queries": [query],
        }

        query_lower = query.lower()

        # 检测查询类型和MD焦点
        for pattern_name, pattern in self.md_patterns.items():
            if pattern.search(query_lower):
                enhanced_info["query_type"] = pattern_name

                if pattern_name == "code_query":
                    enhanced_info["md_focus"].append("code_block")
                    enhanced_info["enhanced_queries"].extend(
                        [f"{query} 代码示例", f"{query} 实现方法", f"如何实现 {query}"]
                    )
                elif pattern_name == "config_query":
                    enhanced_info["md_focus"].extend(["code_block", "list"])
                    enhanced_info["enhanced_queries"].extend(
                        [f"{query} 配置参数", f"{query} 配置示例"]
                    )
                elif pattern_name == "tutorial_query":
                    enhanced_info["structure_preference"] = [
                        "title",
                        "list",
                        "code_block",
                    ]
                    enhanced_info["enhanced_queries"].extend(
                        [f"{query} 详细步骤", f"{query} 操作指南"]
                    )
                break

        # 基于技术领域增强查询
        detected_domains = self._detect_domains(query)
        if detected_domains:
            enhanced_info["detected_domains"] = detected_domains
            for domain in detected_domains:
                enhanced_info["enhanced_queries"].append(f"{domain} {query}")

        # 添加上下文增强
        if context:
            if context.get("preferred_format") == "markdown":
                enhanced_info["enhanced_queries"].append(f"{query} markdown格式")

            if context.get("domain"):
                domain_query = f"{context['domain']} {query}"
                enhanced_info["enhanced_queries"].append(domain_query)

            # 基于历史查询模式增强
            if context.get("recent_queries"):
                pattern_queries = self._generate_pattern_based_queries(
                    query, context["recent_queries"]
                )
                enhanced_info["enhanced_queries"].extend(pattern_queries)

        # 限制查询数量
        enhanced_info["enhanced_queries"] = enhanced_info["enhanced_queries"][:8]

        return enhanced_info

    def _detect_domains(self, query: str) -> List[str]:
        """检测查询中的技术领域"""
        query_lower = query.lower()
        detected = []

        for domain, keywords in self.domain_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                detected.append(domain)

        return detected

    def _generate_pattern_based_queries(
        self, query: str, recent_queries: List[str]
    ) -> List[str]:
        """基于历史查询模式生成相关查询"""
        pattern_queries = []

        # 分析最近查询的模式
        recent_domains = []
        for recent in recent_queries[-3:]:  # 只看最近3个查询
            recent_domains.extend(self._detect_domains(recent))

        # 基于最常见的域生成查询
        if recent_domains:
            from collections import Counter

            most_common_domain = Counter(recent_domains).most_common(1)[0][0]
            pattern_queries.append(f"{most_common_domain} {query}")

        return pattern_queries

    def enhance_document_matching(
        self, query_info: Dict[str, Any], document_metadata: Dict[str, Any]
    ) -> float:
        """根据增强元数据计算文档匹配分数"""
        base_score = 1.0

        # 查询类型匹配
        query_type = query_info.get("query_type", "general")
        doc_patterns = document_metadata.get("content_patterns", [])

        type_bonus = 0.0
        for pattern in doc_patterns:
            pattern_type = pattern.get("pattern_type", "")
            if query_type == "code_query" and "code" in pattern_type:
                type_bonus += 0.2
            elif query_type == "troubleshoot_query" and "troubleshoot" in pattern_type:
                type_bonus += 0.3
            elif query_type == "tutorial_query" and "step" in pattern_type:
                type_bonus += 0.2
            elif query_type == "config_query" and "config" in pattern_type:
                type_bonus += 0.25

        # 技术领域匹配
        query_domains = query_info.get("detected_domains", [])
        doc_domains = document_metadata.get("technical_domains", [])

        domain_bonus = 0.0
        if query_domains and doc_domains:
            common_domains = set(query_domains) & set(doc_domains)
            domain_bonus = (
                len(common_domains) / max(len(query_domains), len(doc_domains)) * 0.3
            )

        # 内容复杂度匹配
        complexity = document_metadata.get("content_complexity", "simple")
        complexity_bonus = {
            "simple": 0.0,
            "moderate": 0.05,
            "complex": 0.1,
            "expert": 0.15,
        }.get(complexity, 0.0)

        # 技术深度奖励
        tech_depth = document_metadata.get("technical_depth", 0.0)
        depth_bonus = tech_depth * 0.1

        # 完整性奖励
        completeness = document_metadata.get("completeness_score", 0.0)
        completeness_bonus = completeness * 0.1

        final_score = (
            base_score
            + type_bonus
            + domain_bonus
            + complexity_bonus
            + depth_bonus
            + completeness_bonus
        )

        return min(final_score, 2.0)  # 限制最大加成
