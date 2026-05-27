# -*- coding: utf-8 -*-
"""
Audio/Dubbing 提供商基础抽象类与注册工具
====================
定义所有音频/配音（语音合成、字幕对齐等）底座接口规范。
"""

from typing import Dict, Any

AUDIO_PROVIDER_REGISTRY: Dict[str, type] = {}

def register_audio_provider(provider_type: str):
    """
    音频/配音提供商自注册类装饰器。
    用于在各 Provider 实现类声明上自动将其装载到全局注册表中。
    """
    def decorator(cls):
        AUDIO_PROVIDER_REGISTRY[provider_type] = cls
        return cls
    return decorator


class BaseAudioProvider:
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        初始化提供商。
        :param name: 提供商配置节点名称 (如 'index_tts')
        :param config: 该提供商对应的 YAML 配置字典
        """
        self.name = name
        self.config = config

    def generate_speech(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        """
        TTS 语音合成接口。
        :param payload: 合成参数字典 (例如 text, speaker, path, 等)
        :param progress_callback: 进度回调函数，用于回传实时步骤和生成进度
        :return: 包含输出结果的字典 (如 status_data 等)
        """
        raise NotImplementedError

    def align_subtitle(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        """
        音频与文本字幕强制对齐接口。
        :param payload: 字幕对齐参数字典 (例如 text, audio_file_path, 等)
        :param progress_callback: 进度回调函数，用于回传实时步骤和生成进度
        :return: 包含对齐产物结果的字典 (如 status_data 等)
        """
        raise NotImplementedError
