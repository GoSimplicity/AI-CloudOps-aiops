#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI助手API路由模块，提供智能问答和流式对话功能
"""

import asyncio
import logging
import threading
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, Union
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

# 创建日志器
logger = logging.getLogger("aiops.api.assistant")


def sanitize_for_json(text: Union[str, Any]) -> Union[str, Any]:
  """
  清理文本中的控制字符，确保JSON安全
  """
  if not isinstance(text, str):
    return text

  # 替换换行符为空格，而不是转义序列，避免在JSON响应中出现真实换行符
  text = text.replace('\n', ' ').replace('\r', ' ')
  # 替换多个连续空格为单个空格
  text = re.sub(r'\s+', ' ', text)
  # 移除其他控制字符
  text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
  return text.strip()


def sanitize_result_data(data: Any) -> Any:
  """
  递归清理结果数据中的所有字符串字段
  """
  if isinstance(data, dict):
    return {k: sanitize_result_data(v) for k, v in data.items()}
  elif isinstance(data, list):
    return [sanitize_result_data(item) for item in data]
  elif isinstance(data, str):
    return sanitize_for_json(data)
  else:
    return data


# 创建路由器
router = APIRouter(tags=["assistant"])

# 创建助手代理全局实例
_assistant_agent = None
_init_lock = threading.RLock()  # 使用可重入锁，避免死锁
_is_initializing = False
_init_called = False  # 添加标志位，防止重复初始化


def get_assistant_agent():
  """获取助手代理单例实例，采用懒加载+锁机制优化初始化性能"""
  global _assistant_agent, _is_initializing, _init_called

  # 快速检查，避免不必要的锁竞争
  if _assistant_agent is not None:
    return _assistant_agent

  # 使用锁避免多线程重复初始化
  with _init_lock:
    # 双重检查锁定模式
    if _assistant_agent is not None:
      return _assistant_agent

    if _is_initializing:
      # 如果正在初始化中，等待一小段时间后再检查
      logger.info("另一个线程正在初始化小助手，等待...")
      for _ in range(20):  # 最多等待10秒
        time.sleep(0.5)
        if _assistant_agent is not None:
          return _assistant_agent

      # 等待超时，重置初始化状态
      logger.warning("等待初始化完成超时，重置初始化状态")
      _is_initializing = False

    # 标记为正在初始化
    _is_initializing = True

    try:
      logger.info("初始化智能小助手代理...")
      from app.core.agents.assistant import AssistantAgent
      _assistant_agent = AssistantAgent()
      logger.info("智能小助手代理初始化完成")
    except Exception as ex:
      logger.error(f"初始化智能小助手代理失败: {str(ex)}")
      _assistant_agent = None
    finally:
      _is_initializing = False

    return _assistant_agent


def init_assistant_in_background():
  """在后台线程中初始化小助手，避免首次调用时的延迟"""
  global _init_called
  
  # 防止重复调用
  if _init_called:
    logger.debug("后台初始化已被调用，跳过重复调用")
    return
    
  _init_called = True

  def _init_thread():
    try:
      logger.info("开始在后台预初始化智能小助手...")
      agent = get_assistant_agent()
      if agent:
        logger.info("小助手后台预初始化完成")
      else:
        logger.warning("小助手后台预初始化失败")
    except Exception as ex:
      logger.error(f"后台初始化小助手失败: {str(ex)}")

  thread = threading.Thread(target=_init_thread, daemon=True, name="AssistantInit")
  thread.start()


def safe_async_run(coroutine, timeout: int = 300):
  """
  安全地运行异步函数，处理不同环境下的运行方式

  Args:
      coroutine: 要运行的协程
      timeout: 超时时间（秒）
  """
  try:
    # 检查是否已有事件循环在运行
    try:
      asyncio.get_running_loop()
      # 如果有运行中的事件循环，在新线程中运行
      logger.debug("检测到运行中的事件循环，在新线程中执行")

      result = None
      exception = None

      def run_in_thread():
        nonlocal result, exception
        try:
          new_loop = asyncio.new_event_loop()
          asyncio.set_event_loop(new_loop)
          try:
            if asyncio.iscoroutine(coroutine):
              result = new_loop.run_until_complete(
                asyncio.wait_for(coroutine, timeout=timeout)
              )
            else:
              result = coroutine
          finally:
            new_loop.close()
        except Exception as e:
          exception = e

      thread = threading.Thread(target=run_in_thread, name="AsyncRunner")
      thread.start()
      thread.join(timeout=timeout + 10)  # 额外10秒的线程等待时间

      if thread.is_alive():
        logger.error("异步任务执行超时")
        raise TimeoutError("异步任务执行超时")

      if exception is not None:
        logger.error(f"异步任务执行失败: {exception}")
        raise RuntimeError("异步任务执行失败")
      return result

    except RuntimeError:
      # 没有运行中的事件循环，可以安全创建新的
      logger.debug("没有运行中的事件循环，创建新的事件循环")
      event_loop = asyncio.new_event_loop()
      asyncio.set_event_loop(event_loop)
      try:
        if asyncio.iscoroutine(coroutine):
          return event_loop.run_until_complete(
            asyncio.wait_for(coroutine, timeout=timeout)
          )
        else:
          return coroutine
      finally:
        event_loop.close()

  except Exception as ex:
    logger.error(f"执行异步函数失败: {str(ex)}")
    raise


def create_error_response(code: int, message: str, data: Optional[Dict] = None) -> tuple:
  """创建统一格式的错误响应"""
  return jsonify({
    'code': code,
    'message': message,
    'data': data or {}
  }), code


def create_success_response(message: str, data: Optional[Dict] = None) -> Response:
  """创建统一格式的成功响应"""
  return jsonify({
    'code': 0,
    'message': message,
    'data': data or {}
  })


@assistant_bp.route('/assistant/query', methods=['POST'])
def assistant_query():
  """智能小助手查询API - 同步包装异步函数"""
  try:
    # 详细的请求调试信息
    logger.info(f"收到查询请求 - Content-Type: {request.content_type}")
    logger.debug(f"请求头: {dict(request.headers)}")

    # 验证请求内容类型 - 增强检测与兼容性
    content_type = request.headers.get('Content-Type', '').lower()
    is_json_type = 'json' in content_type

    if not is_json_type and not request.is_json:
      logger.error(f"请求不是JSON格式 - Content-Type: {request.content_type}")
      return create_error_response(
        400,
        '请求必须是JSON格式，请设置Content-Type为application/json'
      )

    # 获取并验证请求数据
    try:
      data = request.get_json(force=True)
      if data is None:
        raise ValueError("请求体为空")
    except Exception as ex:
      logger.error(f"请求体解析失败: {str(ex)}")
      return create_error_response(400, '请求体必须包含有效的JSON数据')

    logger.debug(f"解析的JSON数据: {data}")

    # 参数验证和提取
    question = data.get('question', '').strip()
    session_id = data.get('session_id')
    max_context_docs = max(1, min(20, data.get('max_context_docs', 4)))  # 限制范围
    mode = data.get('mode', 'rag').lower()

    if not question:
      return create_error_response(400, '问题不能为空')

    # 根据模式处理查询
    if mode == 'mcp':
      logger.info("使用MCP模式处理查询")
      try:
        from app.mcp.mcp_client import MCPAssistant
        mcp_assistant = MCPAssistant()

        # 检查MCP服务是否可用
        is_available = safe_async_run(mcp_assistant.is_available(), timeout=30)
        if not is_available:
          return create_error_response(503, 'MCP服务暂时不可用')

        # 使用MCP处理查询
        answer = safe_async_run(mcp_assistant.process_query(question), timeout=120)

        return create_success_response('查询成功', {
          'answer': answer,
          'session_id': session_id,
          'mode': 'mcp',
          'timestamp': datetime.now().isoformat()
        })

      except Exception as ex:
        logger.error(f"MCP模式处理失败: {str(ex)}")
        return create_error_response(500, f'MCP模式处理失败: {str(ex)}')

    # 默认使用RAG模式
    agent = get_assistant_agent()
    if not agent:
      return create_error_response(500, '智能小助手服务未正确初始化')

    # 调用助手代理获取回答
    try:
      result = safe_async_run(agent.get_answer(
        question=question,
        session_id=session_id,
        max_context_docs=max_context_docs
      ), timeout=180)
    except Exception as ex:
      logger.error(f"获取回答失败: {str(ex)}")
      return create_error_response(500, f'获取回答时出错: {str(ex)}')

    # 生成会话ID（如果不存在）
    if not session_id:
      session_id = agent.create_session()

    # 清理结果数据，确保JSON安全
    clean_result = sanitize_result_data(result)

    return create_success_response('查询成功', {
      'answer': clean_result.get('answer', ''),
      'session_id': session_id,
      'relevance_score': clean_result.get('relevance_score'),
      'recall_rate': clean_result.get('recall_rate', 0.0),
      'sources': clean_result.get('source_documents', []),
      'follow_up_questions': clean_result.get('follow_up_questions', []),
      'mode': 'rag',
      'timestamp': datetime.now().isoformat()
    })

  except Exception as ex:
    # 更详细的错误日志
    logger.error(f"查询处理失败 - 异常类型: {type(ex).__name__}")
    logger.error(f"查询处理失败 - 异常信息: {str(ex)}")
    logger.error(f"查询处理失败 - 请求方法: {request.method}")
    logger.error(f"查询处理失败 - 请求URL: {request.url}")
    logger.error(f"查询处理失败 - Content-Type: {request.content_type}")

    return create_error_response(500, f'处理查询时出错: {str(ex)}')


@assistant_bp.route('/assistant/session', methods=['POST'])
def create_session():
  """创建新会话"""
  try:
    agent = get_assistant_agent()
    if not agent:
      return create_error_response(500, '智能小助手服务未正确初始化')

    session_id = agent.create_session()

    return create_success_response('会话创建成功', {
      'session_id': session_id,
      'timestamp': datetime.now().isoformat()
    })
  except Exception as ex:
    logger.error(f"创建会话失败: {str(ex)}")
    return create_error_response(500, f'创建会话时出错: {str(ex)}')


@assistant_bp.route('/assistant/refresh', methods=['POST'])
def refresh_knowledge_base():
  """刷新知识库"""
  try:
    agent = get_assistant_agent()
    if not agent:
      return create_error_response(500, '智能小助手服务未正确初始化')

    try:
      # 强制清理缓存
      if hasattr(agent, 'response_cache'):
        agent.response_cache = {}
        logger.info("API层强制清理了响应缓存")

      # 刷新知识库
      result = safe_async_run(agent.refresh_knowledge_base(), timeout=300)

      # 为确保向量数据库完全初始化，添加小延迟
      time.sleep(1)

    except Exception as ex:
      logger.error(f"刷新知识库失败: {str(ex)}")
      return create_error_response(500, f'刷新知识库时出错: {str(ex)}')

    return create_success_response('知识库刷新成功', {
      'documents_count': result.get('documents_count', 0),
      'timestamp': datetime.now().isoformat()
    })
  except Exception as ex:
    logger.error(f"刷新知识库失败: {str(ex)}")
    return create_error_response(500, f'刷新知识库时出错: {str(ex)}')


@assistant_bp.route('/assistant/add-document', methods=['POST'])
def add_document():
  """添加文档到知识库"""
  try:
    data = request.get_json()
    if not data:
      return create_error_response(400, '请求体必须包含有效的JSON数据')

    content = data.get('content', '').strip()
    metadata = data.get('metadata', {})

    if not content:
      return create_error_response(400, '文档内容不能为空')

    agent = get_assistant_agent()
    if not agent:
      return create_error_response(500, '智能小助手服务未正确初始化')

    # 添加文档到知识库
    success = agent.add_document(content, metadata)

    if success:
      # 刷新知识库
      try:
        # 强制清理缓存
        if hasattr(agent, 'response_cache'):
          agent.response_cache = {}
          logger.info("API层强制清理了响应缓存")

        # 刷新知识库
        result = safe_async_run(agent.refresh_knowledge_base(), timeout=300)

        # 为确保向量数据库完全初始化，添加小延迟
        time.sleep(1)

        documents_count = result.get('documents_count', 0)
      except Exception as ex:
        logger.error(f"添加文档后刷新知识库失败: {str(ex)}")
        documents_count = 0

      return create_success_response('文档添加成功', {
        'success': True,
        'documents_count': documents_count,
        'timestamp': datetime.now().isoformat()
      })
    else:
      return create_error_response(500, '文档添加失败')

  except Exception as ex:
    logger.error(f"添加文档失败: {str(ex)}")
    return create_error_response(500, f'添加文档时出错: {str(ex)}')


@assistant_bp.route('/assistant/clear-cache', methods=['POST'])
def clear_cache():
  """清除智能小助手的缓存"""
  try:
    agent = get_assistant_agent()
    if not agent:
      return create_error_response(500, '智能小助手服务未正确初始化')

    # 使用新的Redis缓存清空功能
    try:
      result = agent.clear_cache()
      return create_success_response(
        result.get('message', '缓存清除成功'),
        {
          'cleared_items': result.get('cleared_count', 0),
          'success': result.get('success', True)
        }
      )
    except Exception as ex:
      logger.error(f"清空缓存失败: {str(ex)}")
      return create_error_response(500, f'清空缓存失败: {str(ex)}')
  except Exception as ex:
    logger.error(f"清除缓存失败: {str(ex)}")
    return create_error_response(500, f'清除缓存时出错: {str(ex)}')


@assistant_bp.route('/assistant/reinitialize', methods=['POST'])
def reinitialize_assistant():
  """手动重新初始化智能小助手 - 完全重置助手状态，重建向量数据库，重新载入知识库"""
  try:
    global _assistant_agent, _is_initializing

    logger.info("开始手动重新初始化智能小助手...")

    # 使用锁保证线程安全
    with _init_lock:
      # 如果正在初始化，等待初始化完成
      if _is_initializing:
        logger.info("检测到正在进行初始化，等待初始化完成...")
        for _ in range(40):  # 等待20秒
          time.sleep(0.5)
          if not _is_initializing:
            break
        else:
          logger.error("等待初始化完成超时")
          return create_error_response(500, '等待初始化完成超时，请稍后重试')

      # 如果存在当前实例，先进行强制重新初始化
      if _assistant_agent is not None:
        try:
          logger.info("对现有小助手实例进行强制重新初始化...")

          # 检查是否有force_reinitialize方法
          if hasattr(_assistant_agent, 'force_reinitialize'):
            result = safe_async_run(_assistant_agent.force_reinitialize(), timeout=600)

            if result.get('success'):
              logger.info("小助手强制重新初始化成功")
              return create_success_response('智能小助手重新初始化成功', {
                'status': 'success',
                'documents_count': result.get('documents_count', 0),
                'message': result.get('message', ''),
                'processing_time': result.get('processing_time', 0),
                'timestamp': datetime.now().isoformat()
              })
            else:
              logger.error(f"强制重新初始化失败: {result.get('error')}")
          else:
            logger.warning("当前实例没有force_reinitialize方法，继续完全重建")

        except Exception as ex:
          logger.error(f"强制重新初始化时出错: {str(ex)}")

      # 如果强制重新初始化失败或没有现有实例，进行完全重建
      logger.info("进行完全重建小助手...")

      # 关闭当前实例（如果存在）
      if _assistant_agent is not None:
        try:
          logger.info("关闭当前小助手实例...")
          if hasattr(_assistant_agent, 'shutdown'):
            safe_async_run(_assistant_agent.shutdown(), timeout=60)
          logger.info("当前小助手实例已关闭")
        except Exception as ex:
          logger.error(f"关闭当前小助手实例时出错: {str(ex)}")

      # 重置实例
      _assistant_agent = None
      _is_initializing = False

      # 清理缓存和向量存储
      try:
        logger.info("清理残留的缓存和向量数据...")
        from app.core.cache.redis_cache_manager import RedisCacheManager
        from app.config.settings import config

        # 创建临时缓存管理器来清理数据
        redis_config = {
          'host': config.redis.host,
          'port': config.redis.port,
          'db': config.redis.db + 1,
          'password': config.redis.password,
          'connection_timeout': config.redis.connection_timeout,
          'socket_timeout': config.redis.socket_timeout,
          'max_connections': config.redis.max_connections,
          'decode_responses': config.redis.decode_responses
        }

        temp_cache_manager = RedisCacheManager(
          redis_config=redis_config,
          cache_prefix="aiops_assistant_cache:",
          default_ttl=3600,
          max_cache_size=1000,
          enable_compression=True
        )
        temp_cache_manager.clear_all()
        temp_cache_manager.shutdown()
        logger.info("缓存数据清理完成")

      except Exception as ex:
        logger.warning(f"清理缓存数据时出现警告: {str(ex)}")

      # 重新初始化小助手
      logger.info("开始创建新的小助手实例...")
      agent = get_assistant_agent()

      if agent is not None:
        # 创建成功后，再次进行强制重新初始化以确保所有数据都是最新的
        try:
          logger.info("对新创建的小助手实例进行强制重新初始化...")
          if hasattr(agent, 'force_reinitialize'):
            result = safe_async_run(agent.force_reinitialize(), timeout=600)

            if result.get('success'):
              logger.info("智能小助手完全重新初始化成功")
              return create_success_response('智能小助手完全重新初始化成功', {
                'status': 'success',
                'documents_count': result.get('documents_count', 0),
                'message': '已完全重建向量数据库和重新载入知识库',
                'processing_time': result.get('processing_time', 0),
                'timestamp': datetime.now().isoformat()
              })
            else:
              logger.warning(f"新实例强制重新初始化失败: {result.get('error')}")
              return create_success_response(
                '智能小助手重新初始化完成（部分功能可能需要时间生效）',
                {
                  'status': 'partial_success',
                  'warning': result.get('error'),
                  'timestamp': datetime.now().isoformat()
                }
              )
          else:
            logger.info("新实例创建成功，但没有force_reinitialize方法")
            return create_success_response('智能小助手重新初始化完成', {
              'status': 'success',
              'message': '已创建新的小助手实例',
              'timestamp': datetime.now().isoformat()
            })

        except Exception as ex:
          logger.warning(f"新实例强制重新初始化时出错: {str(ex)}")
          return create_success_response(
            '智能小助手重新初始化完成（部分功能可能需要时间生效）',
            {
              'status': 'partial_success',
              'warning': str(ex),
              'timestamp': datetime.now().isoformat()
            }
          )

      else:
        logger.error("智能小助手重新初始化失败")
        return create_error_response(500, '智能小助手重新初始化失败')

  except Exception as ex:
    logger.error(f"重新初始化小助手时发生错误: {str(ex)}")
    return create_error_response(500, f'重新初始化小助手时发生错误: {str(ex)}')
