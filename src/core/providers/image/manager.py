# -*- coding: utf-8 -*-
"""
Image 提供商自发现管理器 (ImageProviderManager)
==============================================
动态扫描 providers/image 文件夹下的所有 Python 文件，
并结合 @register_image_provider 装饰器实现零代码修改、即插即用的自发现插件架构。
"""

import os
import glob
import importlib
from typing import Dict, Any, Type

from src.logic.logger import log
from src.core.providers.image.base import BaseImageProvider, IMAGE_PROVIDER_REGISTRY


def _discover_image_providers():
    """
    生图提供商自发现器。
    动态遍历当前文件夹下的所有 py 文件，执行 import 从而触发各类声明的 @register_image_provider 装饰器。
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = glob.glob(os.path.join(current_dir, "*.py"))

    for f in py_files:
        basename = os.path.basename(f)
        module_name = os.path.splitext(basename)[0]
        # 排除抽象基准类、管理器自身
        if module_name in ("__init__", "base", "manager"):
            continue

        import_path = f"src.core.providers.image.{module_name}"
        try:
            importlib.import_module(import_path)
            log.info(f"[Image Discovery] Auto-discovered and loaded Image Provider module: '{import_path}'")
        except Exception as e:
            log.error(f"[Image Discovery] Failed to import module '{import_path}': {e}", exc_info=True)


class ImageProviderFactory:
    """提供商注册工厂，支持灵活地添加/注册新的第三方生图/工作流引擎"""
    @classmethod
    def register(cls, provider_type: str, provider_class: Type[BaseImageProvider]):
        IMAGE_PROVIDER_REGISTRY[provider_type] = provider_class
        log.info(f"[Image Factory] Manually registered provider type: '{provider_type}' -> {provider_class.__name__}")

    @classmethod
    def get_class(cls, provider_type: str) -> Type[BaseImageProvider]:
        if provider_type not in IMAGE_PROVIDER_REGISTRY:
            raise KeyError(f"Image provider type '{provider_type}' is not registered and discovered in IMAGE_PROVIDER_REGISTRY")
        return IMAGE_PROVIDER_REGISTRY[provider_type]


class ImageProviderManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ImageProviderManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
            
        self.providers: Dict[str, BaseImageProvider] = {}
        self.reload()
        self._initialized = True

    def reload(self):
        """扫描包并重新加载所有提供商"""
        self.providers.clear()

        # 触发包自发现机制，动态加载所有 Provider 插件
        _discover_image_providers()

    def get_provider(self, provider_type: str, name: str = None) -> BaseImageProvider:
        """
        根据提供商类型，获取或实例化对应的提供商实例。
        """
        if provider_type not in self.providers:
            provider_class = ImageProviderFactory.get_class(provider_type)
            try:
                # 图像生成服务通常可以从 service_controller 中获取地址，这里可传递配置（当前传递空字典）
                provider_instance = provider_class(name or provider_type, {})
                self.providers[provider_type] = provider_instance
                log.info(f"[Image Manager] Instantiated provider '{name or provider_type}' of type '{provider_type}'")
            except Exception as e:
                log.error(f"[Image Manager] Failed to load provider '{name or provider_type}': {e}", exc_info=True)
                raise e

        return self.providers[provider_type]


image_provider_manager = ImageProviderManager()
