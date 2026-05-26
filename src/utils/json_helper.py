# -*- coding: utf-8 -*-
"""
JSON 辅助处理工具
=================
提供对大模型 (LLM) 返回的非标准、混杂文本进行清洗、Markdown 代码块剥离，
以及异常安全解析为 Python 字典/列表的工具函数。
"""

import re
import json
from typing import Any, Optional
from src.logic.logger import log

def clean_json_str(text: str) -> str:
    """
    清洗大模型输出的文本，提取其中合法的 JSON 字符串部分。
    支持剥离 ```json ... ``` 或 ``` ... ``` 标记，并提取最外层的花括号 { } 或中括号 [ ]。
    """
    if not text:
        return ""

    text = text.strip()

    # 1. 尝试剥离 Markdown 的代码块围栏
    # 格式如: ```json\n{...}\n``` 或 ```\n{...}\n```
    fenced_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(fenced_pattern, text)
    if match:
        text = match.group(1).strip()

    # 2. 寻找最外层的 { } 或 [ ] 包裹范围，剥离可能存在的外部杂质文本
    start_brace = text.find("{")
    start_bracket = text.find("[")
    
    # 确定最先出现的括号类型
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        # 寻找 { }
        end_brace = text.rfind("}")
        if end_brace != -1 and end_brace > start_brace:
            text = text[start_brace:end_brace + 1]
    elif start_bracket != -1:
        # 寻找 [ ]
        end_bracket = text.rfind("]")
        if end_bracket != -1 and end_bracket > start_bracket:
            text = text[start_bracket:end_bracket + 1]

    return text

def safe_load_json(text: Any, default_val: Any = None) -> Any:
    """
    异常安全的 JSON 解析函数。
    支持直接处理 dict/list，或对 string 执行自动清洗后解析。
    解析失败时记录 warning 并返回默认值，绝不抛出异常打断业务线程。
    """
    if text is None:
        return default_val or {}

    if isinstance(text, (dict, list)):
        return text

    if not isinstance(text, str):
        try:
            return dict(text)
        except Exception:
            return default_val or {}

    cleaned_text = clean_json_str(text)
    if not cleaned_text:
        return default_val or {}

    try:
        return json.loads(cleaned_text)
    except json.JSONDecodeError as e:
        log.warning(f"[JsonHelper] Failed to decode JSON (non-critical): {e}. "
                    f"Raw snippet: {text[:200]}...")
        return default_val or {}
