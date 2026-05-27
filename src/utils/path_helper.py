# -*- coding: utf-8 -*-
import re
from typing import Any, Optional, Dict
from src.logic.yaml_config_loader import yaml_config_loader

DEFAULT_AIGC_EXTENSIONS = [
    # 权重与大模型
    "safetensors", "ckpt", "pt", "pth", "bin", "gguf", "ggml", "onnx", "engine", "trt", "tflite", "xml", "patch", "sft",
    "lora", "lycoris", "lokr", "loha", "glora", "dylora", "pkl", "h5", "pb", "hdf5", "ot", "torchscript", "msgpack",
    # 图像视觉
    "png", "jpg", "jpeg", "webp", "exr", "hdr", "gif", "bmp", "svg",
    # 视频
    "mp4", "webm", "mkv", "mov", "avi",
    # 音频
    "wav", "mp3", "flac", "m4a", "ogg",
    # 字幕与配置
    "txt", "srt", "vtt", "json", "yaml", "yml"
]

_CACHED_EXTENSIONS = None
_AIGC_PATTERN_CACHE = None

def get_aigc_pattern():
    global _CACHED_EXTENSIONS, _AIGC_PATTERN_CACHE
    exts = yaml_config_loader.get("model_extensions")
    if not exts or not isinstance(exts, list):
        exts = DEFAULT_AIGC_EXTENSIONS
    
    exts_tuple = tuple(exts)
    if _CACHED_EXTENSIONS != exts_tuple:
        _CACHED_EXTENSIONS = exts_tuple
        _AIGC_PATTERN_CACHE = re.compile(rf"\.({'|'.join(exts)})$", re.IGNORECASE)
    return _AIGC_PATTERN_CACHE


def translate_path(path_str: str, target_os: str, base_mappings: Optional[Dict[str, str]] = None) -> str:
    """
    将单个文件或目录路径翻译为目标操作系统的路径格式。
    
    :param path_str: 待转换的原始路径字符串
    :param target_os: 目标操作系统标识 — "windows" 或 "linux" (大小写不敏感)
    :param base_mappings: 盘符或基准路径映射关系字典，例如 {"D:/AIGC": "/root/workspace"}
    :return: 翻译转换后的路径字符串
    """
    if not path_str or not isinstance(path_str, str):
        return path_str

    target = target_os.lower()
    
    # 替换可能混合的正反斜杠为标准斜杠（先用正斜杠处理，利于前缀映射匹配）
    normalized = path_str.replace('\\', '/')

    # 1. 深度执行自定义前缀映射替换
    if base_mappings:
        for win_base, lin_base in base_mappings.items():
            win_norm = win_base.replace('\\', '/').rstrip('/')
            lin_norm = lin_base.replace('\\', '/').rstrip('/')
            
            if normalized.startswith(win_norm):
                normalized = normalized.replace(win_norm, lin_norm, 1)
                break
            elif normalized.startswith(lin_norm):
                normalized = normalized.replace(lin_norm, win_norm, 1)
                break

    # 2. 针对目标 OS 执行物理斜杠和盘符的标准转换
    if target == "linux":
        # Windows 转 Linux:
        # a. 统一为正斜杠
        result = normalized
        # b. 去除盘符前缀（如将 "D:/" 替换为 "/"，如果它前面没有被 mapping 替换）
        result = re.sub(r'^[a-zA-Z]:', '', result)
        # c. 清理双斜杠
        result = re.sub(r'//+', '/', result)
        return result
        
    elif target == "windows":
        # Linux 转 Windows:
        # a. 统一为反斜杠
        result = normalized.replace('/', '\\')
        # b. 如果第一位是反斜杠且无盘符，且映射表不存在，可补全默认盘符或直接返回
        return result

    # 默认返回原样
    return path_str

def translate_workflow_paths(workflow: Any, target_os: str, base_mappings: Optional[Dict[str, str]] = None) -> Any:
    """
    深度递归遍历 ComfyUI 工作流字典/列表，识别其中所有的模型或临时文件路径，
    并将其一键转换为符合目标系统格式的路径。
    """
    if isinstance(workflow, dict):
        new_dict = {}
        for k, v in workflow.items():
            # 排除非路径常用键，避免误译（例如 MIME 类型 video/h264-mp4, 数学表达式 a/2 等）
            if k.lower() in (
                "format", "expression", "sampler_name", "scheduler", "mode", "type", 
                "device", "codec", "pix_fmt", "method", "crop_position", 
                "keep_proportion", "upscale_method"
            ):
                new_dict[k] = v
                continue

            # 针对 ComfyUI 常见的路径/模型字段名做智能识别
            if isinstance(v, str) and (
                k.lower() in (
                    "path", "file_path", "image_path", "model_path", "dir", "folder", 
                    "filename", "clip_name", "ckpt_name", "lora_name", "unet_name", 
                    "vae_name", "clip_name1", "clip_name2", "filename_prefix", "model_name"
                ) or
                # 检测值本身符合 AIGC 权重后缀，且包含斜杠/反斜杠特征
                (re.search(r'[/\\]', v) and get_aigc_pattern().search(v))
            ):
                new_dict[k] = translate_path(v, target_os, base_mappings)
            else:
                new_dict[k] = translate_workflow_paths(v, target_os, base_mappings)
        return new_dict
        
    elif isinstance(workflow, list):
        return [translate_workflow_paths(item, target_os, base_mappings) for item in workflow]
        
    return workflow

