# -*- coding: utf-8 -*-
"""
KamlPro 通用工具包 (Utils)
=========================
包含用于大模型 JSON 清洗解析、跨平台路径翻译转换等辅助模块。
"""

from .json_helper import clean_json_str, safe_load_json
from .path_helper import translate_path, translate_workflow_paths
