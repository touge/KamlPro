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
    若对应的底层服务在 services.yaml 中配置了 enabled: false，跳过导入。
    """
    from src.core.service_controller import service_controller

    current_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = glob.glob(os.path.join(current_dir, "*.py"))

    for f in py_files:
        basename = os.path.basename(f)
        module_name = os.path.splitext(basename)[0]
        # 排除抽象基准类、管理器自身
        if module_name in ("__init__", "base", "manager"):
            continue

        # 检查是否关联了被禁用的底层服务（如 ollama）
        if service_controller.has_service(module_name) and not service_controller.is_enabled(module_name):
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
        self.model_to_tier: Dict[str, str] = {}
        self.model_to_modalities: Dict[str, list] = {}
        self.reload()
        self._initialized = True

    def reload(self):
        """重新加载配置文件并重新初始化所有大模型提供商"""
        self.providers.clear()
        self.model_to_provider.clear()
        self.model_to_tier.clear()
        self.model_to_modalities.clear()

        # 触发包自发现机制，动态加载所有 Provider 插件
        _discover_providers()

        # 加载 configs/llm_config.yaml
        llm_data = yaml_config_loader.get("llm_providers.providers", {})
        if not llm_data:
            log.warning("[LLM Manager] No LLM provider configuration found in llm_config.yaml")
            return

        for provider_name, provider_config in llm_data.items():
            # 1. 检查 llm_config.yaml 中显式配置的 enabled: false
            if not provider_config.get("enabled", True):
                continue

            # 2. 检查绑定的底层微服务（如 Ollama）在 services.yaml 中是否被禁用
            from src.core.service_controller import service_controller
            if service_controller.has_service(provider_name) and not service_controller.is_enabled(provider_name):
                continue

            # 强制要求配置显式 type 字段以防止非预期事故发生
            prov_type = provider_config.get("type") or provider_config.get("provider")
            if not prov_type:
                error_msg = f"[LLM Manager] Provider '{provider_name}' configuration error: missing 'type' or 'provider' field."
                log.critical(error_msg)
                raise RuntimeError(error_msg)

            # Provider 级别默认 tier 与 modalities（优先配置段中的定义，缺省为 api / ['text']）
            prov_tier = provider_config.get("tier", "api")
            prov_modalities = provider_config.get("modalities", ["text"])
            if isinstance(prov_modalities, str):
                prov_modalities = [prov_modalities]

            provider_class = LLMProviderFactory.get_class(prov_type)
            try:
                # 实例化对应的 Provider 插件并保存
                provider_instance = provider_class(provider_name, provider_config)
                self.providers[provider_name] = provider_instance
                log.info(f"[LLM Manager] Instantiated provider '{provider_name}' of type '{prov_type}' (tier={prov_tier})")

                # 解析该提供商下所支持的所有大模型列表，并构建高效快速检索映射
                models_list = provider_config.get("models", [])
                for m in models_list:
                    if not m.get("enabled", True):
                        continue
                    model_name = m.get("name")
                    if model_name:
                        self.model_to_provider[model_name] = provider_instance
                        # 模型优先使用自身显式指定的 tier，未指定时继承 Provider 的 tier
                        model_tier = m.get("tier") or prov_tier
                        self.model_to_tier[model_name] = model_tier

                        # 模型模态解析（支持 text, vision, audio 等）
                        raw_mods = m.get("modalities") or prov_modalities
                        if isinstance(raw_mods, str):
                            raw_mods = [raw_mods]
                        cleaned_mods = [str(x).lower().strip() for x in raw_mods]
                        self.model_to_modalities[model_name] = cleaned_mods

                        log.info(f"  -> Model '{model_name}' mapped to '{provider_name}' (tier={model_tier}, modalities={cleaned_mods})")
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

    def get_model_tier(self, model_name: str) -> str:
        """
        获取大模型的调度层级 tier:
        - "local": 本地 GPU 串行队列 (FIFO, 显卡算力独占)
        - "api": 云端 API 并发队列 (线程池, 并发受限)
        """
        tier = self.model_to_tier.get(model_name)
        if tier:
            return tier
        try:
            prov = self.get_provider_for_model(model_name)
            return prov.config.get("tier", "api")
        except Exception:
            return "api"

    def get_service_name_for_model(self, model_name: str) -> str | None:
        """
        获取该模型关联的物理微服务名称（如 "Ollama"）。
        约定：type == "ollama" 对应微服务 "Ollama"；云端 API 模型对应 None。
        """
        try:
            prov = self.get_provider_for_model(model_name)
            if "service_name" in prov.config:
                return prov.config.get("service_name")
            prov_type = prov.config.get("type") or prov.config.get("provider", "")
            if prov_type == "ollama":
                return prov.config.get("service_name", "Ollama")
            return None
        except Exception:
            return None

    def get_model_modalities(self, model_name: str) -> list[str]:
        """
        获取大模型支持的模态列表，例如 ['text', 'vision']。
        若模型未显式配置，缺省返回 ['text']。
        """
        if model_name in self.model_to_modalities:
            return self.model_to_modalities[model_name]
        try:
            prov = self.get_provider_for_model(model_name)
            prov_mods = prov.config.get("modalities", ["text"])
            if isinstance(prov_mods, str):
                prov_mods = [prov_mods]
            return [str(x).lower().strip() for x in prov_mods]
        except Exception:
            return ["text"]

    def supports_modality(self, model_name: str, modality: str) -> bool:
        """
        检查指定模型是否支持特定模态（如 'vision', 'text', 'audio'）。
        """
        mods = self.get_model_modalities(model_name)
        return modality.lower().strip() in mods


llm_provider_manager = LLMProviderManager()

