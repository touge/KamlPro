# -*- coding: utf-8 -*-
"""
媒体多模态加载与转码工具类
==========================
负责将下游传入的多样化图片输入（Base64、Data URL、本地文件路径、网络 HTTP URL）
统一定向清洗并转换为大模型底层所需的标准 Base64 编码字符串。
"""

import os
import re
import base64
import requests
from typing import List, Union

from src.logic.logger import log


def normalize_image(img_input: Union[str, bytes]) -> str:
    """
    将单个图片输入源统一转换为纯 Base64 编码字符串。

    支持输入形态：
    1. 纯 Base64 字符串 (如 "iVBORw0KGgoAAAANSUhEUgAA...")
    2. Data URL 格式 (如 "data:image/png;base64,iVBORw0KGgo...")
    3. 本地磁盘文件绝对/相对路径 (如 "D:/AIGC/test.jpg" 或 "./images/demo.png")
    4. 网络 HTTP/HTTPS 图片链接 (如 "https://example.com/sample.png")
    """
    if not img_input:
        raise ValueError("图片输入不能为空")

    if isinstance(img_input, bytes):
        return base64.b64encode(img_input).decode("utf-8")

    if not isinstance(img_input, str):
        raise TypeError(f"不支持的图片输入类型: {type(img_input)}")

    cleaned = img_input.strip()

    # 1. 处理 Data URL (形如 data:image/jpeg;base64,xxxx)
    if cleaned.startswith("data:image/") and ";base64," in cleaned:
        return cleaned.split(";base64,", 1)[1].strip()

    # 2. 处理网络 URL (http:// 或 https://)
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        log.info(f"[MediaLoader] 正在从网络下载图片: {cleaned[:80]}...")
        try:
            resp = requests.get(cleaned, timeout=10)
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"网络图片下载失败 ({cleaned}): {e}")

    # 3. 处理本地磁盘文件路径
    # 如果路径真实存在，优先作为本地文件读取
    if os.path.isfile(cleaned):
        try:
            with open(cleaned, "rb") as f:
                file_bytes = f.read()
            return base64.b64encode(file_bytes).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"读取本地图片文件失败 ({cleaned}): {e}")

    # 4. 纯 Base64 字符串处理
    # 移除可能存在的换行符和空格
    cleaned_base64 = re.sub(r"\s+", "", cleaned)
    
    # 简单校验 Base64 合法性（长度及可解码测试）
    try:
        # 只尝试解码前 32 个字节以极低开销快速校验
        sample = cleaned_base64[:64]
        # 补全 padding 进行检测
        padding = len(sample) % 4
        if padding:
            sample += "=" * (4 - padding)
        base64.b64decode(sample)
        return cleaned_base64
    except Exception:
        # 如果不是合法文件也不是合法 Base64，抛出清晰异常
        if os.path.exists(cleaned) is False and (len(cleaned) < 256):
            raise FileNotFoundError(f"本地图片文件不存在: {cleaned}")
        raise ValueError("传入的字符串既不是存在的本地文件路径，也不是合法的 Base64 图像数据")


def normalize_images(images: Union[List[Union[str, bytes]], str, bytes, None]) -> List[str]:
    """
    批量清洗与转换图片列表，返回纯 Base64 字符串列表。
    """
    if not images:
        return []

    if isinstance(images, (str, bytes)):
        images = [images]

    if not isinstance(images, (list, tuple)):
        raise TypeError(f"images 期望为列表或单个图片字符串，但收到了 {type(images)}")

    normalized_list = []
    for idx, item in enumerate(images):
        try:
            b64_str = normalize_image(item)
            normalized_list.append(b64_str)
        except Exception as e:
            raise ValueError(f"第 {idx + 1} 张图片处理失败: {e}")

    return normalized_list
