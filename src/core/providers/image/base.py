# -*- coding: utf-8 -*-
"""
Image/Workflow 提供商基础抽象类与注册工具
====================
定义所有生图/工作流底座接口规范。
"""

from typing import Dict, Any

IMAGE_PROVIDER_REGISTRY: Dict[str, type] = {}

def register_image_provider(provider_type: str):
    """
    生图/工作流提供商自注册类装饰器。
    用于在各 Provider 实现类声明上自动将其装载到全局注册表中。
    """
    def decorator(cls):
        IMAGE_PROVIDER_REGISTRY[provider_type] = cls
        return cls
    return decorator


class BaseImageProvider:
    def __init__(self, name: str, config: Dict[str, Any]):
        """
        初始化提供商。
        :param name: 提供商配置节点名称 (如 'comfyui')
        :param config: 该提供商对应的 YAML 配置字典
        """
        self.name = name
        self.config = config

    def generate_image(self, payload: Dict[str, Any], progress_callback: callable) -> Dict[str, Any]:
        """
        执行工作流/生图任务接口。
        :param payload: 任务参数字典 (例如 workflow, final_output_node_id, 等)
        :param progress_callback: 进度回调函数，用于回传实时步骤和生成进度
        :return: 包含输出结果的字典 (如 outputs 等)
        """
        raise NotImplementedError
