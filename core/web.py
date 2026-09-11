# core/web.py
"""
WebUI 管理页后端

基于 AstrBot 官方 Plugin Pages 能力（需要宿主 >= v4.24.1）：
- 后端：context.register_web_api 注册 /astrbot_plugin_worldbook/* 路由
- 前端：pages/manager/ 目录（Dashboard 以受限 iframe 加载，经 bridge 转发请求）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .config import PluginConfig
from .lorebook import Lorebook
from .schedule import (
    HolidayProvider,
    ScheduleConfig,
    describe_schedule,
    next_fire_times,
    normalize_schedule_dict,
)
from .session import SessionCache

PLUGIN_NAME = "astrbot_plugin_worldbook"

# 新建条目时允许携带的字段
_CREATE_KEYS = {
    "template",
    "enabled",
    "priority",
    "scope",
    "keywords",
    "probability",
    "cron",
    "duration",
    "times",
    "content",
    "inject_position",
    "cooldown",
    "schedule",
}


class WorldbookWeb:
    """世界书 WebUI 管理页"""

    def __init__(
        self,
        context,
        config: PluginConfig,
        lorebook: Lorebook,
        sessions: SessionCache,
    ):
        self._context = context
        self._cfg = config
        self._lorebook = lorebook
        self._sessions = sessions
        self._holidays = HolidayProvider(config.data_dir)
        self._version = self._load_version()

    def _load_version(self) -> str:
        try:
            meta = yaml.safe_load(
                (Path(self._cfg.plugin_dir) / "metadata.yaml").read_text("utf-8")
            )
            return str((meta or {}).get("version") or "unknown")
        except Exception:
            return "unknown"

    # ================= 注册 =================

    def register(self) -> None:
        """向 Dashboard 注册本插件的 Web API（路由必须以插件名开头）"""
        register = self._context.register_web_api
        base = f"/{PLUGIN_NAME}"
        register(f"{base}/meta", self.get_meta, ["GET"], "世界书插件元信息")
        register(f"{base}/entries", self.list_entries, ["GET"], "列出全部条目")
        register(f"{base}/entries/save", self.save_entry, ["POST"], "新建/更新条目")
        register(f"{base}/entries/delete", self.delete_entry, ["POST"], "删除条目")
        register(
            f"{base}/schedule/preview", self.preview_schedule, ["POST"], "定时配置预览"
        )
        register(f"{base}/config/save", self.save_global_config, ["POST"], "保存全局配置")
        logger.info("[worldbook] WebUI API 注册完成")

    # ================= 接口实现 =================

    async def get_meta(self):
        return json_response(
            {
                "plugin_name": PLUGIN_NAME,
                "version": self._version,
                "global": {
                    "max_inject_count": self._cfg.max_inject_count,
                    "allow_same_priority": self._cfg.allow_same_priority,
                    "inject_position": self._cfg.inject_position,
                },
                "holiday_coverage_year": self._holidays.coverage_year,
            }
        )

    async def list_entries(self):
        entries: list[dict[str, Any]] = []
        now = datetime.now()
        for e in self._lorebook.list_entries_sorted():
            item = e.to_dict()
            item["schedule_enabled"] = e.schedule_enabled
            item["schedule_text"] = describe_schedule(e.schedule, e.cron)
            item["next_fires"] = []
            if e.schedule_enabled:
                try:
                    moments = await next_fire_times(
                        e.schedule, e.cron, count=3, holiday=self._holidays, now=now
                    )
                    item["next_fires"] = [m.strftime("%Y-%m-%d %H:%M") for m in moments]
                except Exception as ex:
                    logger.warning(f"[worldbook-web] 计算下次执行失败 {e.name}: {ex}")
            remaining_time = e.remaining_time
            remaining_times = e.remaining_times
            item["runtime"] = {
                "active": bool(e.active),
                "remaining_time": None if remaining_time == float("inf") else remaining_time,
                "remaining_times": None if remaining_times == float("inf") else remaining_times,
            }
            entries.append(item)
        return json_response({"entries": entries})

    async def save_entry(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("条目名称不能为空")
        if len(name) > 20:
            return error_response("条目名称过长（不超过 20 字符）")

        existing = self._lorebook.get_entry(name)
        if existing is None:
            return self._create_entry(payload, name)

        fields = payload.get("fields") or {}
        entry = self._lorebook.update_entry_fields(name, fields)
        if entry is None:
            return error_response("条目不存在")
        # 同步刷新各会话中的激活副本（保留运行状态）
        self._sessions.refresh_entry(name, entry)
        return json_response({"updated": name})

    def _create_entry(self, payload: dict, name: str) -> Any:
        # 前端把全部表单值放在 fields 中，兼容顶层直传
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        content = str(payload.get("content") or fields.get("content") or "").strip()
        if not content:
            return error_response("条目内容不能为空")
        item: dict[str, Any] = {"name": name, "content": content}
        for key in _CREATE_KEYS:
            if key == "content":
                continue
            value = fields.get(key, payload.get(key))
            if value not in (None, ""):
                item[key] = value
        try:
            names = self._lorebook.add_entries([item])
        except Exception as e:
            logger.error(f"[worldbook-web] 创建条目失败: {e}")
            return error_response(f"创建失败: {e}")
        if not names:
            return error_response("创建失败：条目已存在")
        return json_response({"created": names})

    async def delete_entry(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("条目名称不能为空")
        ok, failed = self._lorebook.remove_entries([name])
        if name in ok:
            self._sessions.remove_everywhere(name)
            return json_response({"deleted": name})
        return error_response(f"条目不存在: {', '.join(failed)}")

    async def preview_schedule(self):
        payload = await request.json(default={})
        raw = payload.get("schedule") or {}
        cron = str(payload.get("cron") or "")
        try:
            count = max(1, min(int(payload.get("count") or 5), 10))
        except (TypeError, ValueError):
            count = 5

        schedule_dict = normalize_schedule_dict(raw)
        schedule = ScheduleConfig(schedule_dict)
        try:
            moments = await next_fire_times(
                schedule, cron, count=count, holiday=self._holidays
            )
        except Exception as e:
            return error_response(f"计算失败: {e}")
        return json_response(
            {
                "fires": [m.strftime("%Y-%m-%d %H:%M") for m in moments],
                "describe": describe_schedule(schedule, cron),
                "mode": schedule_dict.get("mode"),
            }
        )

    async def save_global_config(self):
        payload = await request.json(default={})
        gp = payload.get("global") or {}
        if not isinstance(gp, dict):
            return error_response("参数错误")

        if "inject_position" in gp:
            position = gp["inject_position"]
            if position not in ("user_input", "system_prompt"):
                return error_response("非法的注入位置")
            self._cfg.inject_position = position
        if "max_inject_count" in gp:
            try:
                self._cfg.max_inject_count = max(0, int(gp["max_inject_count"]))
            except (TypeError, ValueError):
                return error_response("最大注入数必须是整数")
        if "allow_same_priority" in gp:
            self._cfg.allow_same_priority = bool(gp["allow_same_priority"])

        self._cfg.save_config()
        return json_response({"saved": True})
