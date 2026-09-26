# core/web.py
"""
WebUI 管理页后端

基于 AstrBot 官方 Plugin Pages 能力（需要宿主 >= v4.24.1）：
- 后端：context.register_web_api 注册 /astrbot_plugin_worldbook/* 路由
- 前端：pages/manager/ 目录（Dashboard 以受限 iframe 加载，经 bridge 转发请求）

导入导出（v2.7.0 起为唯一入口，替代原聊天命令）：
- 导出：GET /export 返回 JSON 文件（宿主经 iframe 触发浏览器下载）
- 导入：POST /import（同名跳过）/ POST /import/overwrite（同名覆盖），
  接收 bridge.upload 上传的单文件（multipart 字段名固定为 file）
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger
from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from .config import PluginConfig
from .lorefile import LoreFile
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

# 导入文件允许的后缀
_IMPORT_SUFFIXES = (".json", ".yaml", ".yml")


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
        register(f"{base}/export", self.export_lorebook, ["GET"], "导出世界书 JSON")
        register(f"{base}/import", self.import_lorebook, ["POST"], "导入世界书（同名跳过）")
        register(
            f"{base}/import/overwrite",
            self.import_lorebook_overwrite,
            ["POST"],
            "导入世界书（同名覆盖）",
        )
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
            item["schedule_text"] = describe_schedule(e.schedule)
            item["next_fires"] = []
            if e.schedule_enabled:
                try:
                    moments = await next_fire_times(
                        e.schedule, count=3, holiday=self._holidays, now=now
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

        # 编辑时前端携带 original_name；与 name 不同则视为重命名
        original = str(payload.get("original_name") or "").strip() or name
        existing = self._lorebook.get_entry(original)
        if existing is None:
            return self._create_entry(payload, name)

        if name != original:
            ok, err = self._lorebook.rename_entry(original, name)
            if not ok:
                return error_response(f"重命名失败：{err}")
            # 会话中已激活副本同步改名，随后统一刷新为最新配置
            self._sessions.rename_everywhere(original, name)

        fields = payload.get("fields") or {}
        entry = self._lorebook.update_entry_fields(name, fields)
        if entry is None:
            return error_response("条目不存在")
        # 同步刷新各会话中的激活副本（保留运行状态）
        self._sessions.refresh_entry(name, entry)
        return json_response({"updated": name, "original": original})

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
        try:
            count = max(1, min(int(payload.get("count") or 5), 10))
        except (TypeError, ValueError):
            count = 5

        schedule_dict = normalize_schedule_dict(raw)
        schedule = ScheduleConfig(schedule_dict)
        try:
            moments = await next_fire_times(schedule, count=count, holiday=self._holidays)
        except Exception as e:
            return error_response(f"计算失败: {e}")
        return json_response(
            {
                "fires": [m.strftime("%Y-%m-%d %H:%M") for m in moments],
                "describe": describe_schedule(schedule),
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

    # ================= 导入导出 =================

    def _clean_export_dir(self) -> None:
        """导出前清理历史导出文件，避免临时目录堆积"""
        try:
            for old in self._cfg.export_dir.iterdir():
                if old.is_file():
                    old.unlink(missing_ok=True)
                else:
                    shutil.rmtree(old, ignore_errors=True)
        except Exception as e:
            logger.warning(f"[worldbook-web] 清理导出目录失败: {e}")

    async def export_lorebook(self):
        """
        导出全部条目为 JSON 文件（宿主触发浏览器下载）

        - 复用 LoreFile.save 的 {"entries": [...]} 结构，与导入格式完全对称
        - 文件落在数据目录的 export/ 下，每次导出前清理历史文件
        """
        self._clean_export_dir()
        filename = f"worldbook_{datetime.now():%Y%m%d_%H%M%S}.json"
        target = self._cfg.export_dir / filename
        try:
            LoreFile.save(target, self._lorebook.list_entries_sorted())
        except Exception as e:
            logger.error(f"[worldbook-web] 导出失败: {e}")
            return error_response(f"导出失败: {e}")
        logger.info(f"[worldbook-web] 已导出世界书: {target}")
        return file_response(
            target, filename=filename, content_type="application/json"
        )

    async def import_lorebook(self):
        """导入世界书（同名条目跳过，保留现有条目）"""
        return await self._import_lorebook(overwrite=False)

    async def import_lorebook_overwrite(self):
        """导入世界书（同名条目覆盖为文件中的版本）"""
        return await self._import_lorebook(overwrite=True)

    async def _import_lorebook(self, *, overwrite: bool):
        """
        导入实现（两种同名策略共用）

        - 接收 bridge.upload 的单文件（multipart 字段名固定 file）
        - 落盘后用 LoreFile.load 解析（自动兼容原生格式与酒馆格式）
        - 逐条校验：缺名称/内容的条目单独统计，不影响其余条目导入
        - 覆盖模式：先清除同名条目的会话激活副本与注册，再统一写入
        """
        upload = (await request.files()).get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("未收到上传文件")

        filename = str(upload.filename or "").strip()
        if not filename.lower().endswith(_IMPORT_SUFFIXES):
            return error_response("仅支持 .json / .yaml / .yml 世界书文件")

        workdir = self._cfg.import_dir / uuid.uuid4().hex
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            target = workdir / Path(filename).name
            await upload.save(target)
            raw_entries = LoreFile.load(target)
        except Exception as e:
            logger.error(f"[worldbook-web] 导入解析失败: {e}")
            return error_response(f"文件解析失败: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # 逐条校验：坏条目只记录，不中断整体导入
        valid: list[dict[str, Any]] = []
        invalid: list[str] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                invalid.append("非对象条目")
                continue
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "").strip()
            if not name or not content:
                invalid.append(name or "未命名条目")
                continue
            valid.append(item)

        existing = [str(i["name"]).strip() for i in valid if self._lorebook.get_entry(str(i["name"]).strip())]
        if overwrite:
            if existing:
                for name in existing:
                    self._sessions.remove_everywhere(name)
                self._lorebook.remove_entries(existing)
            to_add = valid
        else:
            existing_set = set(existing)
            to_add = [i for i in valid if str(i["name"]).strip() not in existing_set]

        imported: list[str] = []
        if to_add:
            try:
                imported = self._lorebook.add_entries(to_add)
            except Exception as e:
                logger.error(f"[worldbook-web] 导入写入失败: {e}")
                return error_response(f"导入失败: {e}")

        result = {
            "total": len(raw_entries),
            "imported": imported,
            "skipped": [] if overwrite else existing,
            "invalid": invalid,
        }
        logger.info(
            f"[worldbook-web] 导入完成（{'覆盖' if overwrite else '跳过'}同名）: "
            f"新增 {len(imported)} 条，跳过 {len(result['skipped'])} 条，"
            f"无效 {len(invalid)} 条"
        )
        return json_response(result)
