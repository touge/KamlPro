# -*- coding: utf-8 -*-
"""
LLM 提供商基础抽象类
====================
定义所有大模型底座接口规范。
"""

import os
import re
import queue
import threading
from typing import Dict, Any

PROVIDER_REGISTRY: Dict[str, type] = {}

def register_provider(provider_type: str):
    """
    大模型提供商自注册类装饰器。
    用于在各 Provider 实现类声明上自动将其装载到全局注册表中。
    """
    def decorator(cls):
        PROVIDER_REGISTRY[provider_type] = cls
        return cls
    return decorator


class BaseLLMProvider:
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        初始化提供商。
        :param name: 提供商配置节点名称 (如 'ollama', 'deepseek')
        :param config: 该提供商对应的 YAML 配置字典
        """
        self.name = name
        self.config = config

    def generate(self, model: str, prompt: str, options: dict) -> dict:
        """
        同步非流式大模型文本生成接口。
        :param model: 模型名称
        :param prompt: 用户提示词
        :param options: 附加参数 (temperature, max_tokens 等)
        :return: 包含 text 结果的字典
        """
        raise NotImplementedError

    def generate_stream(self, task_id: str, model: str, prompt: str, options: dict,
                        output_queue: queue.Queue, started_event: threading.Event = None) -> None:
        """
        流式大模型文本生成接口。
        :param task_id: 任务唯一标识符
        :param model: 模型名称
        :param prompt: 用户提示词
        :param options: 附加参数
        :param output_queue: 输出中转 NDJSON 数据队列
        :param started_event: 流式连接就绪触发事件，若不为 None 则需进行流式握手握手同步
        """
        raise NotImplementedError

    def discover_models(self) -> list[dict]:
        """
        动态发现/获取该提供商当前可用的模型列表。
        子类可选择重写此方法（例如通过 GET /v1/models 或扫描本地服务配置文件）。
        """
        return []

    def _resolve_env_var(self, val: str) -> str:
        """解析环境变量引用，如 ${ENV_VAR}"""
        if not val or not isinstance(val, str):
            return val
        pattern = re.compile(r'\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)')
        def replace(match):
            var = match.group(1) or match.group(2)
            return os.environ.get(var, "")
        return pattern.sub(replace, val)
