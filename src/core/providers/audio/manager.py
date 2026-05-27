# -*- coding: utf-8 -*-
"""
Audio 提供商自发现管理器 (AudioProviderManager)
==============================================
动态扫描 providers/audio 文件夹下的所有 Python 文件，
并结合 @register_audio_provider 装饰器实现零代码修改、即插即用的自发现插件架构。
"""

import os
import glob
import importlib
from typing import Dict, Any, Type

from src.logic.logger import log
from src.core.providers.audio.base import BaseAudioProvider, AUDIO_PROVIDER_REGISTRY


def _discover_audio_providers():
    """
    音频提供商自发现器。
    动态遍历当前文件夹下的所有 py 文件，执行 import 从而触发各类声明的 @register_audio_provider 装饰器。
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = glob.glob(os.path.join(current_dir, "*.py"))

    for f in py_files:
        basename = os.path.basename(f)
        module_name = os.path.splitext(basename)[0]
        # 排除抽象基准类、管理器自身
        if module_name in ("__init__", "base", "manager"):
            continue

        import_path = f"src.core.providers.audio.{module_name}"
        try:
            importlib.import_module(import_path)
            log.info(f"[Audio Discovery] Auto-discovered and loaded Audio Provider module: '{import_path}'")
        except Exception as e:
            log.error(f"[Audio Discovery] Failed to import module '{import_path}': {e}", exc_info=True)


class AudioProviderFactory:
    """提供商注册工厂，支持灵活地添加/注册新的第三方配音/音频服务"""
    @classmethod
    def register(cls, provider_type: str, provider_class: Type[BaseAudioProvider]):
        AUDIO_PROVIDER_REGISTRY[provider_type] = provider_class
        log.info(f"[Audio Factory] Manually registered provider type: '{provider_type}' -> {provider_class.__name__}")

    @classmethod
    def get_class(cls, provider_type: str) -> Type[BaseAudioProvider]:
        if provider_type not in AUDIO_PROVIDER_REGISTRY:
            raise KeyError(f"Audio provider type '{provider_type}' is not registered and discovered in AUDIO_PROVIDER_REGISTRY")
        return AUDIO_PROVIDER_REGISTRY[provider_type]


class AudioProviderManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AudioProviderManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
            
        self.providers: Dict[str, BaseAudioProvider] = {}
        self.reload()
        self._initialized = True

    def reload(self):
        """扫描包并重新加载所有提供商"""
        self.providers.clear()

        # 触发包自发现机制，动态加载所有 Provider 插件
        _discover_audio_providers()

    def get_provider(self, provider_type: str, name: str = None) -> BaseAudioProvider:
        """
        根据提供商类型，获取或实例化对应的提供商实例。
        """
        if provider_type not in self.providers:
            provider_class = AudioProviderFactory.get_class(provider_type)
            try:
                provider_instance = provider_class(name or provider_type, {})
                self.providers[provider_type] = provider_instance
                log.info(f"[Audio Manager] Instantiated provider '{name or provider_type}' of type '{provider_type}'")
            except Exception as e:
                log.error(f"[Audio Manager] Failed to load provider '{name or provider_type}': {e}", exc_info=True)
                raise e

        return self.providers[provider_type]


image_provider_manager = AudioProviderManager()  # Support standard singleton (or custom renamed)
audio_provider_manager = image_provider_manager  # Set exact name to keep imports highly readable
