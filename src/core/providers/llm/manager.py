# -*- coding: utf-8 -*-
"""
LLM 提供商自发现管理器 (LLMProviderManager)
===========================================
利用 pkgutil 和 importlib 动态扫描 providers/llm 文件夹下的所有 Python 文件，
并结合 @register_provider 装饰器实现零代码修改、即插即用的自发现插件架构。
"""

import os
import glob
import importlib
from typing import Dict, Any, Type

from src.logic.yaml_config_loader import yaml_config_loader
from src.logic.logger import log
from src.core.providers.llm.base import BaseLLMProvider, PROVIDER_REGISTRY


def _discover_providers():
    """
    大模型提供商自发现器。
    动态遍历当前文件夹下的所有 py 文件，执行 import 从而触发各类声明的 @register_provider 装饰器。
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = glob.glob(os.path.join(current_dir, "*.py"))

    for f in py_files:
        basename = os.path.basename(f)
        module_name = os.path.splitext(basename)[0]
        # 排除抽象基准类、管理器自身
        if module_name in ("__init__", "base", "manager"):
            continue

        import_path = f"src.core.providers.llm.{module_name}"
        try:
            importlib.import_module(import_path)
            log.info(f"[LLM Discovery] Auto-discovered and loaded LLM Provider module: '{import_path}'")
        except Exception as e:
            log.error(f"[LLM Discovery] Failed to import module '{import_path}': {e}", exc_info=True)


class LLMProviderFactory:
    """提供商注册工厂，支持灵活地添加/注册新的第三方大模型底座"""
    @classmethod
    def register(cls, provider_type: str, provider_class: Type[BaseLLMProvider]):
        PROVIDER_REGISTRY[provider_type] = provider_class
        log.info(f"[LLM Factory] Manually registered provider type: '{provider_type}' -> {provider_class.__name__}")

    @classmethod
    def get_class(cls, provider_type: str) -> Type[BaseLLMProvider]:
        if provider_type not in PROVIDER_REGISTRY:
            raise KeyError(f"Provider type '{provider_type}' is not registered and discovered in PROVIDER_REGISTRY")
        return PROVIDER_REGISTRY[provider_type]


class LLMProviderManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LLMProviderManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
            
        self.providers: Dict[str, BaseLLMProvider] = {}
        self.model_to_provider: Dict[str, BaseLLMProvider] = {}
        self.reload()
        self._initialized = True

    def reload(self):
        """重新加载配置文件并重新初始化所有大模型提供商"""
        self.providers.clear()
        self.model_to_provider.clear()

        # 触发包自发现机制，动态加载所有 Provider 插件
        _discover_providers()

        # 加载 configs/llm_config.yaml
        llm_data = yaml_config_loader.get("llm_providers.providers", {})
        if not llm_data:
            log.warning("[LLM Manager] No LLM provider configuration found in llm_config.yaml")
            return

        for provider_name, provider_config in llm_data.items():
            # 强制要求配置显式 type 字段以防止非预期事故发生
            prov_type = provider_config.get("type") or provider_config.get("provider")
            if not prov_type:
                error_msg = f"[LLM Manager] Provider '{provider_name}' configuration error: missing 'type' or 'provider' field."
                log.critical(error_msg)
                raise RuntimeError(error_msg)

            provider_class = LLMProviderFactory.get_class(prov_type)
            try:
                # 实例化对应的 Provider 插件并保存
                provider_instance = provider_class(provider_name, provider_config)
                self.providers[provider_name] = provider_instance
                log.info(f"[LLM Manager] Instantiated provider '{provider_name}' of type '{prov_type}'")

                # 解析该提供商下所支持的所有大模型列表，并构建高效的 model -> provider 快速检索映射
                models_list = provider_config.get("models", [])
                for m in models_list:
                    model_name = m.get("name")
                    if model_name:
                        self.model_to_provider[model_name] = provider_instance
                        log.info(f"  -> Model '{model_name}' mapped to '{provider_name}'")
            except Exception as e:
                log.error(f"[LLM Manager] Failed to load provider '{provider_name}': {e}", exc_info=True)

    def get_provider_for_model(self, model_name: str) -> BaseLLMProvider:
        """
        根据大模型名称，动态路由并返回匹配的 Provider 实例。
        如果找不到，抛出 KeyError，引导 Fail-fast。
        """
        provider = self.model_to_provider.get(model_name)
        if provider:
            return provider

        # 极致防崩兜底：尝试使用本地 Ollama 提供商作为备选
        log.warning(f"[LLM Manager] Model '{model_name}' is not registered under any provider, falling back to 'ollama'")
        ollama_provider = self.providers.get("ollama")
        if ollama_provider:
            return ollama_provider

        # 彻底 Fail-fast
        raise KeyError(f"No provider found for model '{model_name}', and no fallback 'ollama' provider is available.")


llm_provider_manager = LLMProviderManager()
