# core/lorebook.py
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .entry import LoreEntry
from .lorefile import LoreFile
from .schedule import (
    is_empty_schedule,
    normalize_schedule_dict,
    parse_cron_to_schedule,
)


# ================= 旧数据清洗 =================

# v2.6.0 起废弃的字段：__template_key（旧模板键名，直接删除）；
# template 字段保留但统一为 "default"（AstrBot 面板 template_list 渲染路由需要）
_LEGACY_KEYS = ("__template_key",)


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "启用")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def _to_str_list(value: Any) -> list[str]:
    """keywords / scope 字段兜底：字符串按行/逗号拆分为列表"""
    if isinstance(value, str):
        raw: list[str] = value.replace("，", ",").splitlines()
        items: list[str] = []
        for part in raw:
            items.extend(p.strip() for p in part.split(","))
        return [p for p in items if p]
    if isinstance(value, (list, tuple)):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def _clean_entry_dict(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    清洗单个条目 dict 为当前版本格式，返回 (清洗结果, 是否有改动)

    - 旧 cron 表达式转换为结构化 schedule（不可转换的保留为 mode=cron + expr）
    - template 字段统一为 "default"，删除废弃字段 __template_key / cron
    - schedule 规范化补全
    - 字段类型修正（bool/int/float/list[str]）
    """
    new_item = dict(item)
    changed = False

    # 1. 旧 cron 表达式 → 结构化 schedule
    legacy_cron = str(new_item.pop("cron", "") or "").strip()
    if "cron" in item:
        changed = True
    if legacy_cron:
        sched = normalize_schedule_dict(new_item.get("schedule"))
        if sched.get("mode") in ("", "none") or is_empty_schedule(new_item.get("schedule")):
            new_item["schedule"] = parse_cron_to_schedule(legacy_cron)
        elif sched.get("mode") == "cron" and not sched.get("expr"):
            sched["expr"] = legacy_cron
            new_item["schedule"] = sched

    # 2. 模板标记统一 + 删除废弃字段
    if new_item.get("template") != "default":
        new_item["template"] = "default"
        changed = True
    for key in _LEGACY_KEYS:
        if key in new_item:
            new_item.pop(key)
            changed = True

    # 3. schedule 规范化（补全字段 / 清洗非法值 / 迁入 expr）
    norm_schedule = normalize_schedule_dict(new_item.get("schedule"))
    if new_item.get("schedule") != norm_schedule:
        changed = True
    new_item["schedule"] = norm_schedule

    # 4. 字段类型修正
    if "enabled" in new_item:
        enabled = _to_bool(new_item["enabled"], True)
        if new_item["enabled"] != enabled:
            changed = True
        new_item["enabled"] = enabled
    for key in ("priority", "duration", "times", "cooldown"):
        if key in new_item:
            value = _to_int(new_item[key], 0 if key != "priority" else 50)
            if new_item[key] != value:
                changed = True
            new_item[key] = value
    if "probability" in new_item:
        value = _to_float(new_item["probability"], 1.0)
        if new_item["probability"] != value:
            changed = True
        new_item["probability"] = value
    for key in ("keywords", "scope"):
        if key in new_item:
            value = _to_str_list(new_item[key])
            if new_item[key] != value:
                changed = True
            new_item[key] = value
    if "inject_position" in new_item:
        pos = str(new_item["inject_position"]).strip()
        if pos not in ("default", "system_prompt", "user_input"):
            changed = True
            new_item["inject_position"] = "default"

    return new_item, changed


class Lorebook:
    """
    世界书核心业务层
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.entry_map: dict[str, LoreEntry] = {}
        self.on_changed: list[Callable[[], None]] = []

    @property
    def entries(self) -> list[LoreEntry]:
        return list(self.entry_map.values())

    async def initialize(self):
        # 启动时检测旧版数据：无旧数据则原样启动，有则清洗后启动
        self._migrate_entry_storage()

        if self.cfg.entry_storage:
            names = self._register_entry(self.cfg.entry_storage)
        else:
            logger.debug("[lorebook] 未配置 entry_storage, 将使用默认配置")
            names = self.load_entry_from_lorefile(self.cfg.default_lorefile)
        logger.debug(f"已注册条目: {names}")

    def _migrate_entry_storage(self) -> None:
        """
        旧数据自动迁移清洗（插件启动时执行一次）

        - 无 entry_storage 时直接跳过（正常启动）
        - 逐条清洗：cron→schedule、删除废弃字段、字段类型修正
        - 无效条目（缺名称/内容）与重名条目剔除
        - 有任何改动才写回配置
        """
        if not self.cfg.entry_storage:
            return

        changed = False
        cleaned: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for item in self.cfg.entry_storage:
            if not isinstance(item, dict):
                changed = True
                continue
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "").strip()
            if not name or not content:
                logger.warning(
                    f"[lorebook] 迁移时发现无效条目（缺少名称或内容），已跳过: {name!r}"
                )
                changed = True
                continue
            if name in seen_names:
                logger.warning(f"[lorebook] 迁移时发现重复条目，已去重: {name}")
                changed = True
                continue
            seen_names.add(name)

            new_item, item_changed = _clean_entry_dict(item)
            new_item["name"] = name
            cleaned.append(new_item)
            changed = changed or item_changed

        if changed:
            self.cfg.entry_storage[:] = cleaned
            self.cfg.save_config()
            logger.info("[lorebook] 检测到旧版数据格式，已完成自动迁移清洗")

    def _register_entry(
        self, items: list[dict[str, Any]], skip_same: bool = True,
    ) -> list[str]:
        """注册条目（兜底保证 name 唯一）"""
        registered_names: list[str] = []
        need_save = False
        need_emit = False
        for item in items:
            name = item.get("name")
            content = item.get("content")
            if not name or not content:
                continue
            if skip_same and name in self.entry_map:
                logger.warning(f"[lorebook] 已存在同名条目: {name}")
                continue
            entry = LoreEntry(item)
            self.entry_map[name] = entry
            registered_names.append(entry.name)
            if item not in self.cfg.entry_storage:
                self.cfg.entry_storage.append(item)
                need_save = True
            if entry.schedule_enabled:
                need_emit = True
        if need_emit:
            self._emit_changed()
        if need_save:
            self._save_config()
        return registered_names

    def _save_config(self) -> None:
        """
        保存配置前统一兜底：
        - 按 priority 排序
        - 同步 entries / entry_storage 顺序
        """
        cfg_map = {cfg["name"]: cfg for cfg in self.cfg.entry_storage}
        sorted_entries = sorted(self.entry_map.values(), key=lambda e: e.priority)
        self.cfg.entry_storage[:] = [cfg_map[e.name] for e in sorted_entries]
        self.cfg.save_config()

    def _emit_changed(self):
        for cb in self.on_changed:
            cb()

    # ================= 查询接口 =================

    def get_entry(self, name: str) -> LoreEntry | None:
        """按 name 获取单个条目"""
        return self.entry_map.get(name)

    def list_entries(self) -> list[LoreEntry]:
        """获取全部条目（包含启用和禁用）"""
        return list(self.entry_map.values())

    def list_enabled_entries(self) -> list[LoreEntry]:
        """获取全部启用的条目"""
        return [entry for entry in self.entries if entry.enabled]

    def list_disabled_entries(self) -> list[LoreEntry]:
        """获取当前已禁用的条目"""
        return [e for e in self.entries if not e.enabled]

    def list_entries_sorted(self) -> list[LoreEntry]:
        """获取按 priority 排序的全部 entries"""
        return sorted(self.entries, key=lambda p: p.priority)

    # ================= CRUD 接口 =================

    def _resolve_priority(self, data: dict) -> int:
        """
        priority 规则：
        - 用户显式指定：直接使用
        - 否则：从基准 priority（50）开始，取第一个未被占用的自增优先级
        """
        # 1. 用户指定（最高优先级）
        if "priority" in data:
            return data["priority"]

        # 2. 基准 priority
        base = 50

        # 3. 收集所有已占用的 priority
        used = {e.priority for e in self.entries}

        # 4. 从 base + 1 开始，找第一个未被占用的
        p = base + 1
        while p in used:
            p += 1

        return p

    def add_entries(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        name: str | None = None,
        content: str | None = None,
    ) -> list[str]:
        """
        新增一个条目

        必填：
            - name
            - content
        其余字段缺省时使用通用默认值（缺省值由 LoreEntry / _conf_schema 定义）
        """
        if items is None:
            items = []

        if name is not None and content is not None:
            items.append({"name": name, "content": content})

        if not items:
            raise ValueError("add_entries 缺少参数")

        full_items = []
        for item in items:
            if not item.get("name"):
                raise ValueError("缺少必须的 name 参数")
            if not item.get("content"):
                raise ValueError("缺少必须的 content 参数")

            # ===== 字段统一解析（不再区分模板，未知字段直接丢弃）=====
            full_item: dict[str, Any] = {
                # 面板 template_list 渲染路由所需的固定标记（单一模板）
                "template": "default",
                "name": item["name"],
                "enabled": _to_bool(item.get("enabled"), True),
                "priority": self._resolve_priority(item),
                "scope": _to_str_list(item.get("scope", [])),
                "keywords": _to_str_list(item.get("keywords", [])),
                "duration": _to_int(item.get("duration", 180), 180),
                "times": _to_int(item.get("times", 5), 5),
                "probability": _to_float(item.get("probability", 1.0), 1.0),
                "inject_position": item.get("inject_position", "default"),
                "cooldown": _to_int(item.get("cooldown", 0), 0),
                "schedule": normalize_schedule_dict(
                    item.get("schedule") or {}
                ),
                "content": item["content"],
            }
            if full_item["inject_position"] not in (
                "default", "system_prompt", "user_input",
            ):
                full_item["inject_position"] = "default"
            full_items.append(full_item)

        registered_names = self._register_entry(full_items)
        return registered_names

    def rename_entry(self, old_name: str, new_name: str) -> tuple[bool, str]:
        """
        重命名条目（同步 entry_map / entry_storage / 运行时数据）

        返回 (是否成功, 失败原因)
        """
        old_name = str(old_name).strip()
        new_name = str(new_name).strip()
        if not new_name:
            return False, "新名称不能为空"
        if new_name == old_name:
            return True, ""
        if old_name not in self.entry_map:
            return False, f"未找到条目：{old_name}"
        if new_name in self.entry_map:
            return False, f"已存在同名条目：{new_name}"

        entry = self.entry_map.pop(old_name)
        entry.name = new_name
        self.entry_map[new_name] = entry

        # entry._data 与 entry_storage 中的 dict 是同一对象，重命名已同步；
        # 这里再兜底扫一遍，防止未来出现副本注册的情况
        for cfg in self.cfg.entry_storage:
            if isinstance(cfg, dict) and cfg.get("name") == old_name:
                cfg["name"] = new_name

        self._save_config()
        logger.info(f"[lorebook] 条目已重命名: {old_name} → {new_name}")
        return True, ""

    def remove_entries(self, names: list[str]) -> tuple[list[str], list[str]]:
        """按 name 批量删除条目"""
        success: list[str] = []
        failed: list[str] = []
        removed_names: set[str] = set()

        for name in names:
            if self.entry_map.pop(name, None) is not None:
                success.append(name)
                removed_names.add(name)
            else:
                failed.append(name)

        if removed_names:
            self.cfg.entry_storage[:] = [
                cfg
                for cfg in self.cfg.entry_storage
                if cfg.get("name") not in removed_names
            ]
        if success:
            self._save_config()
            self._emit_changed()

        return success, failed

    # ================= 配置接口 =================

    def add_scope_to_entry(self, name: str, scope: str) -> bool:
        e = self.entry_map.get(name)
        if not e:
            return False
        changed = e.add_scope(scope)
        if changed:
            self._save_config()
        return True

    def remove_scope_from_entry(self, name: str, scope: str) -> bool:
        entry = self.entry_map.get(name)
        if not entry:
            return False
        changed = entry.remove_scope(scope)
        if changed:
            self._save_config()
        return True

    def update_keywords(self, name: str, keywords: list[str]) -> bool:
        entry = self.entry_map.get(name)
        if not entry:
            return False

        entry.set_keywords(keywords)
        self._save_config()
        return True

    def update_priority(self, name: str, priority: int) -> bool:
        entry = self.entry_map.get(name)
        if not entry:
            return False

        entry.set_priority(priority)
        self._save_config()
        return True

    # ================= WebUI 更新接口 =================

    # 这些字段变更会影响定时注册，更新后需要重载调度器
    _RESCHEDULE_KEYS = {"schedule", "enabled"}

    _UPDATABLE_FIELDS = {
        "enabled",
        "priority",
        "scope",
        "keywords",
        "probability",
        "duration",
        "times",
        "content",
        "inject_position",
        "cooldown",
        "schedule",
    }

    def update_entry_fields(self, name: str, fields: dict[str, Any]) -> LoreEntry | None:
        """
        批量更新条目字段（WebUI 使用）

        - 只接受白名单内字段；名称变更走 rename_entry
        - 更新后保持 entry_storage 同步并落盘
        - schedule / enabled 变更会触发调度器重载
        """
        entry = self.entry_map.get(name)
        if not entry:
            return None

        for key, value in (fields or {}).items():
            if key not in self._UPDATABLE_FIELDS:
                continue
            if key == "keywords":
                entry.set_keywords([str(k) for k in (value or []) if str(k).strip()])
            elif key == "priority":
                entry.set_priority(int(value))
            elif key == "cooldown":
                entry.cooldown = max(0, int(value or 0))
            elif key == "probability":
                entry.probability = float(value or 0)
            elif key == "scope":
                entry.scope = [str(s) for s in (value or [])]
            elif key == "schedule":
                entry.set_schedule(value)
            else:
                setattr(entry, key, value)

        self._save_config()
        if self._RESCHEDULE_KEYS & set(fields or {}):
            self._emit_changed()
        return entry

    # ================= 读取文件 =================

    def load_entry_from_lorefile(self, file_path: Path) -> None:
        """
        从世界书文件中加载条目到配置中
        规则:
          - 仅支持 Json 和 Yaml 文件
          - 同名条目会跳过
        """
        try:
            raw_entries = LoreFile.load(file_path)
            self.add_entries(raw_entries)
        except Exception as e:
            logger.error(f"[lorebook] 加载失败: {file_path} ({e})")
            return

    def export_lorefile(self, path: str) -> None:
        """
        导出当前世界书为 lorefile（用于分享）
        """
        file_path = Path(path)
        try:
            LoreFile.save(file_path, self.entries)
            logger.info(f"[lorebook] 世界书已导出: {file_path}")
        except Exception as e:
            logger.error(f"[lorebook] 导出失败: {file_path} ({e})")
