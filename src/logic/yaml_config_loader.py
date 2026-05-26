import os
import re
import yaml
import copy
from pathlib import Path
from typing import Any, Optional


class YamlConfigLoader:
    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = Path(config_path)
        self._data: dict = {}
        self.reload()

    def reload(self):
        if self._config_path.exists():
            with open(self._config_path, encoding="utf-8") as f:
                raw_data = yaml.safe_load(f) or {}
            # Recursively resolve any sub-configs and imports
            self._data = self._resolve_sub_configs(raw_data)
        else:
            self._data = {}

    def get(self, key_path: str, default: Any = None) -> Any:
        node = self._data
        for part in key_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return self._resolve_env(node)

    def _resolve_env(self, value: Any) -> Any:
        if isinstance(value, str):
            def _replace(m):
                var = m.group(1)
                return os.environ.get(var, m.group(0))
            return re.sub(r"\$\{(\w+)\}", _replace, value)
        if isinstance(value, dict):
            return {k: self._resolve_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_env(v) for v in value]
        return value

    def _deep_merge(self, dest: dict, src: dict) -> dict:
        for key, value in src.items():
            if key in dest:
                if isinstance(dest[key], dict) and isinstance(value, dict):
                    self._deep_merge(dest[key], value)
                else:
                    dest[key] = copy.deepcopy(value)
            else:
                dest[key] = copy.deepcopy(value)
        return dest

    def _resolve_sub_configs(self, data: Any) -> Any:
        if isinstance(data, dict):
            # 1. First extract and handle imports if present
            if "imports" in data and isinstance(data["imports"], list):
                imports_list = data.pop("imports")
                for imp_path in imports_list:
                    if isinstance(imp_path, str) and imp_path.endswith((".yaml", ".yml")):
                        file_path = self._config_path.parent / imp_path
                        if not file_path.exists():
                            file_path = Path(imp_path)
                        
                        if file_path.exists():
                            try:
                                with open(file_path, encoding="utf-8") as f:
                                    sub_data = yaml.safe_load(f) or {}
                                sub_data = self._resolve_sub_configs(sub_data)
                                if isinstance(sub_data, dict):
                                    self._deep_merge(data, sub_data)
                            except Exception as e:
                                print(f"[YamlConfigLoader] Error loading imported config {imp_path}: {e}")

            # 2. Recursively resolve sub-configs under all dictionary keys
            for k, v in list(data.items()):
                resolved_value = self._resolve_sub_configs(v)
                # Smart unwrapper: If resolved_value is a dict and has key `k`, unwrap it!
                if isinstance(resolved_value, dict) and k in resolved_value:
                    resolved_value = resolved_value[k]
                data[k] = resolved_value
            return data

        elif isinstance(data, list):
            return [self._resolve_sub_configs(item) for item in data]

        elif isinstance(data, str) and data.endswith((".yaml", ".yml")):
            # Resolve value as nested config in-place if it points to a yaml file
            file_path = self._config_path.parent / data
            if not file_path.exists():
                file_path = Path(data)

            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        sub_data = yaml.safe_load(f) or {}
                    return self._resolve_sub_configs(sub_data)
                except Exception as e:
                    print(f"[YamlConfigLoader] Error loading in-place config {data}: {e}")

        return data


yaml_config_loader = YamlConfigLoader()
