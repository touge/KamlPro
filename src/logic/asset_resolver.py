# -*- coding: utf-8 -*-
"""
资产复用与秒传解析器 (AssetResolver)
=====================================
用于短剧等批量分镜场景下的资产复用 (reuse_assets)：
- 自动在 ComfyUI input/、历史 tasks/、全局 assets/ 目录寻址匹配素材；
- 安全软链/硬链/拷贝至当前任务目录，实现零网络传输秒级开闸。
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional

from src.logic.yaml_config_loader import yaml_config_loader
from src.core.service_controller import service_controller
from src.logic.logger import log


class AssetResolver:
    """资产定位与复用处理器"""

    @classmethod
    def get_candidate_search_dirs(cls) -> List[Path]:
        """动态收集所有可能的资产搜索根目录"""
        candidate_dirs: List[Path] = []

        # 1. 配置中显式指定的资产目录
        custom_input = yaml_config_loader.get("paths.comfyui_input")
        if custom_input:
            p = Path(custom_input)
            if p.exists() and p.is_dir():
                candidate_dirs.append(p)

        custom_assets = yaml_config_loader.get("paths.assets_folder", "assets")
        if custom_assets:
            p = Path(custom_assets)
            if p.exists() and p.is_dir():
                candidate_dirs.append(p)

        # 2. 从 ComfyUI 服务配置自动推导 input 目录
        comfy_config = service_controller.get_service_config("ComfyUI") or {}
        comfy_path = comfy_config.get("path")
        if comfy_path:
            launcher_path = Path(comfy_path)
            # 尝试推导上级或同级 input
            possible_roots = [launcher_path.parent, launcher_path.parent.parent]
            for root in possible_roots:
                for sub in ("input", "ComfyUI/input"):
                    input_dir = root / sub
                    if input_dir.exists() and input_dir.is_dir() and input_dir not in candidate_dirs:
                        candidate_dirs.append(input_dir)

        # 3. 常见本地路径兜底
        for default_candidate in (Path("input"), Path("tasks"), Path("assets")):
            if default_candidate.exists() and default_candidate.is_dir() and default_candidate not in candidate_dirs:
                candidate_dirs.append(default_candidate)

        return candidate_dirs

    @classmethod
    def resolve_asset(cls, asset_name: str) -> Optional[Path]:
        """
        在所有候选目录与历史任务中按文件名查找已存在的物料文件。
        返回匹配到的文件 Path，未找到返回 None。
        """
        if not asset_name:
            return None

        clean_name = os.path.basename(asset_name)

        # 如果直接是已存在的合法路径
        direct_path = Path(asset_name)
        if direct_path.is_file():
            return direct_path

        # 遍历候选搜索目录
        for base_dir in cls.get_candidate_search_dirs():
            # 直接匹配子文件
            target = base_dir / clean_name
            if target.is_file():
                return target

            # 如果是 tasks 目录，向下一层遍历查找历史任务产出或输入
            if base_dir.name == "tasks":
                try:
                    for task_subdir in base_dir.iterdir():
                        if task_subdir.is_dir():
                            sub_target = task_subdir / clean_name
                            if sub_target.is_file():
                                return sub_target
                except Exception:
                    pass

        return None

    @classmethod
    def link_or_copy(cls, source_file: Path, target_dir: Path, target_name: Optional[str] = None) -> Path:
        """
        将已有文件映射到目标任务工作目录。
        优先使用软链 (symlink)，权限不足时回退到硬链 (hardlink) 或拷贝 (copy)。
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = target_name or source_file.name
        destination = target_dir / filename

        # 如果已经是同一个文件且存在，直接返回
        if destination.exists():
            return destination

        # 1. 尝试软链接 (零磁盘占用)
        try:
            destination.symlink_to(source_file.resolve())
            log.info(f"[AssetResolver] Symlinked {source_file} -> {destination}")
            return destination
        except (OSError, NotImplementedError):
            pass

        # 2. 尝试硬链接
        try:
            os.link(source_file, destination)
            log.info(f"[AssetResolver] Hardlinked {source_file} -> {destination}")
            return destination
        except (OSError, NotImplementedError):
            pass

        # 3. 回退为快速拷贝
        shutil.copy2(source_file, destination)
        log.info(f"[AssetResolver] Copied {source_file} -> {destination}")
        return destination
