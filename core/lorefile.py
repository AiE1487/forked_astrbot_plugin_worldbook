from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger

from .entry import LoreEntry


class LoreFile:
    """
    世界书文件层 (支持JSON / YAML)
    """

    # === 内部工具 ===

    @staticmethod
    def _convert_sillytavern(raw: Any) -> list[dict[str, Any]] | None:
        """
        识别酒馆 SillyTavern Lorebook 格式（entries 为按索引的对象）并转换为本插件格式。

        字段映射：
        - comment/name -> name
        - key/keys     -> keywords
        - content      -> content
        - disable      -> enabled（取反）
        - probability  -> probability（酒馆为 0-100，>1 时除以 100）
        - constant     -> 常驻条目（resident 模板，不依赖触发词）
        """
        if not (isinstance(raw, dict) and isinstance(raw.get("entries"), dict)):
            return None

        entries: list[dict[str, Any]] = []
        for item in raw["entries"].values():
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            name = (
                str(item.get("comment") or item.get("name") or "").strip()
                or "未命名条目"
            )
            keys = item.get("key") or item.get("keys") or []
            keywords = [str(k).strip() for k in keys if str(k).strip()]
            disable = item.get("disable")
            enabled = (not disable) if isinstance(disable, bool) else True
            constant = bool(item.get("constant"))
            probability = item.get("probability", item.get("prob", 100))
            try:
                probability = float(probability)
                if probability > 1:
                    probability = probability / 100
            except (TypeError, ValueError):
                probability = 1.0

            entries.append(
                {
                    "template": "resident" if constant else "common",
                    "name": name,
                    "enabled": enabled,
                    "keywords": keywords or ([name] if not constant else []),
                    "content": content,
                    "probability": probability,
                }
            )

        # 酒馆条目名可能重复（comment 常为空），兜底保证唯一
        seen: set[str] = set()
        for entry in entries:
            base, candidate, idx = entry["name"], entry["name"], 2
            while candidate in seen:
                candidate = f"{base}({idx})"
                idx += 1
            seen.add(candidate)
            entry["name"] = candidate
        return entries

    @staticmethod
    def _load_raw(path: Path) -> Any:
        suffix = path.suffix.lower()
        try:
            with path.open("r", encoding="utf-8") as f:
                if suffix in {".yaml", ".yml"}:
                    import yaml

                    return yaml.safe_load(f)

                if suffix == ".json":
                    import json

                    return json.load(f)

                raise ValueError(f"不支持的文件类型: {suffix}")
        except Exception as e:
            raise RuntimeError(f"读取 lorefile 失败: {e}") from e

    # === 对外接口 ===

    @staticmethod
    def load(path: Path) -> list[dict[str, Any]]:
        """
        从 lorefile 中读取原始 entry dict 列表
        """
        if not path.exists():
            raise FileNotFoundError(path)

        data = LoreFile._load_raw(path)

        # 兼容酒馆 SillyTavern Lorebook 格式（entries 为按索引的对象）
        st_entries = LoreFile._convert_sillytavern(data)
        if st_entries is not None:
            logger.info(f"[lorefile] 识别为酒馆 Lorebook 格式，共 {len(st_entries)} 条")
            return st_entries

        # 兼容：
        # - list[dict]
        # - { entries: [...] }
        if isinstance(data, dict) and "entries" in data:
            data = data["entries"]

        if not isinstance(data, list):
            raise ValueError(
                f"文件格式错误: {path}，必须是 list[dict] 或 {{entries: [...]}}"
            )

        entries: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                logger.warning(f"[lorefile] 跳过非法项: {item}")
                continue
            entries.append(item)

        return entries

    @staticmethod
    def dump(entries: list[LoreEntry]) -> list[dict[str, Any]]:
        """
        LoreEntry -> 可序列化 dict（不包含运行时状态）
        """
        result: list[dict[str, Any]] = []
        for e in entries:
            result.append(e.to_dict())
        return result

    @staticmethod
    def save(path: Path, entries: list[LoreEntry]) -> None:
        """
        将 LoreEntry 列表写入 lorefile
        """
        payload = {
            "entries": LoreFile.dump(entries),
        }

        suffix = path.suffix.lower()
        try:
            if suffix in {".yaml", ".yml"}:
                with path.open("w", encoding="utf-8") as f:
                    yaml.safe_dump(
                        payload,
                        f,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                return

            if suffix == ".json":
                with path.open("w", encoding="utf-8") as f:
                    json.dump(
                        payload,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                return

            raise ValueError(f"不支持的文件类型: {suffix}")

        except Exception as e:
            raise RuntimeError(f"写入 lorefile 失败: {e}") from e


