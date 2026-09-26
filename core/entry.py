# core/entry.py
from __future__ import annotations

import random
import re
import time
from typing import Any

from astrbot.api import logger

from .config import ConfigNode
from .schedule import (
    ScheduleConfig,
    describe_schedule,
    is_empty_schedule,
    normalize_schedule_dict,
    parse_cron_to_schedule,
)

# 注入位置：default 表示跟随全局 inject_position 配置
INJECT_POSITIONS = ("default", "system_prompt", "user_input")


class LoreEntry(ConfigNode):
    """
    LoreEntry 模型: 描述一个世界书的条目。
    """

    # ===== 配置字段 =====
    name: str
    enabled: bool
    priority: int
    scope: list[str]
    keywords: list[str]
    probability: float
    content: str
    duration: int
    times: int
    inject_position: str
    cooldown: int
    schedule: ScheduleConfig

    def __init__(self, data: dict):
        # 兼容旧版配置：缺省字段兜底为与 _conf_schema 一致的默认值
        data.setdefault("enabled", True)
        data.setdefault("priority", 50)
        data.setdefault("keywords", [])
        data.setdefault("scope", [])
        data.setdefault("duration", 180)
        data.setdefault("times", 5)
        data.setdefault("probability", 1.0)
        data.setdefault("inject_position", "default")
        data.setdefault("cooldown", 0)

        # 旧版独立 cron 字段已废弃：转换为结构化 schedule 后从数据中移除
        legacy_cron = str(data.pop("cron", "") or "").strip()
        sched = normalize_schedule_dict(data.get("schedule"))
        if legacy_cron and sched.get("mode") in ("", None, "none"):
            # schedule 未配置实际触发时，旧 cron 表达式仍有效 → 迁移转换
            data["schedule"] = parse_cron_to_schedule(legacy_cron)
        else:
            # schedule 已配置 cron 模式但缺表达式时，回填旧 cron
            if sched.get("mode") == "cron" and not sched.get("expr") and legacy_cron:
                sched["expr"] = legacy_cron
            data["schedule"] = sched

        # 模板标记：AstrBot 配置面板的 template_list 依赖条目数据上的
        # template 字段路由渲染表单；v2.6.0 起模板概念废弃，
        # 统一恒为 default（单一模板，用户不可见，仅作面板渲染路由）
        data["template"] = "default"
        data.pop("__template_key", None)

        super().__init__(data)

        # 本条目的激活时间，也是条目进入激发态的标志
        self._activated_at = None

        # 注入次数, 大于等于 times 时本条目生效周期结束
        self._inject_count = 0

        # cron 触发时间
        self._cron_fired_at: float | None = None

        # 定时激活窗口终点（全天/时间段模式由定时配置决定，时刻模式为 None 走 duration）
        self._cron_window_end: float | None = None

        # 编译并缓存正则
        self._compiled_patterns: list[re.Pattern] = []
        self._compile_patterns()

    @property
    def template(self) -> str:
        """已废弃：v2.6.0 起不再区分模板，恒为 default（仅供旧数据展示兜底）"""
        return "default"

    def to_dict(self) -> dict[str, Any]:
        """
        LoreEntry -> lorefile dict
        """
        return {
            "template": "default",
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "scope": list(self.scope),
            "keywords": list(self.keywords),
            "probability": self.probability,
            "content": self.content,
            "duration": self.duration,
            "times": self.times,
            "inject_position": self.inject_position,
            "cooldown": self.cooldown,
            "schedule": self.raw_schedule(),
        }

    def raw_schedule(self) -> dict[str, Any]:
        """schedule 底层 dict 的拷贝（用于序列化）"""
        raw = self._data.get("schedule")
        return dict(raw) if isinstance(raw, dict) else {}

    def set_schedule(self, value: Any) -> None:
        """
        整体替换 schedule 配置（规范化后写回底层 dict，保持 dict 身份不变）
        """
        normalized = normalize_schedule_dict(value)
        raw = self._data.get("schedule")
        if not isinstance(raw, dict):
            raw = {}
            self._data["schedule"] = raw
        raw.clear()
        raw.update(normalized)
        # 子视图缓存失效，下次访问按新数据重建
        self._children.pop("schedule", None)

    def add_scope(self, scope: str) -> bool:
        """Add a scope if it does not already exist."""
        scopes = list(self.scope)
        if scope in scopes:
            return False

        scopes.append(scope)
        self.scope = scopes
        return True

    def remove_scope(self, scope: str) -> bool:
        """Remove a scope if it exists."""
        scopes = list(self.scope)
        if scope not in scopes:
            return False

        scopes.remove(scope)
        self.scope = scopes
        return True

    def set_keywords(self, keywords: list[str]) -> None:
        """Replace keywords and rebuild compiled regex patterns."""
        self.keywords = list(keywords)
        self._compile_patterns()

    def set_priority(self, priority: int) -> None:
        """Update entry priority."""
        self.priority = priority

    # ==================================================
    # 编译正则
    # ==================================================

    def _compile_patterns(self) -> None:
        """编译正则"""
        self._compiled_patterns.clear()
        self.keywords = [k for k in (self.keywords or []) if k.strip()]

        for pattern in self.keywords:
            try:
                self._compiled_patterns.append(re.compile(pattern))
            except re.error as e:
                logger.warning(f"[条目:{self.name}] 正则编译失败: {pattern} ({e})")

    def _match_keywords(self, text: str) -> bool:
        """是否命中任一关键词正则"""
        for p in self._compiled_patterns:
            if p.search(text):
                return True
        return False

    # ==================================================
    # 基础状态
    # ==================================================

    @property
    def active(self) -> bool:
        """条目是否正处于激活态"""
        if self._activated_at is None:
            return False

        now = time.time()

        # 判断是否过期
        if self.duration > 0 and now > self._activated_at + self.duration:
            logger.debug(f"[条目:{self.name}]  已过期")
            return False

        # 判断是否耗尽次数
        if self.times > 0 and self._inject_count >= self.times:
            logger.debug(
                f"[条目:{self.name}]  已耗尽次数({self._inject_count}/{self.times})"
            )
            return False

        return True

    @property
    def remaining_time(self) -> float:
        """剩余有效时间（秒）"""
        if self._activated_at is None:
            return 0

        # 永久有效
        if self.duration <= 0:
            return float("inf")

        now = time.time()
        end_time = self._activated_at + self.duration
        return max(0, end_time - now)

    @property
    def remaining_times(self) -> int | float:
        """剩余可用次数"""
        if self._activated_at is None:
            return 0

        # 次数无限
        if self.times <= 0:
            return float("inf")

        return max(0, self.times - self._inject_count)

    @property
    def enabled_keywords(self) -> bool:
        if not self._compiled_patterns:
            return False
        return True

    @property
    def enabled_cron(self) -> bool:
        """旧数据兼容：schedule 处于 cron 模式时，表达式是否为合法 5 段格式"""
        if not self.enabled:
            return False
        if self.schedule.mode != "cron":
            return False
        return len(str(self.schedule.expr).split()) == 5

    @property
    def schedule_enabled(self) -> bool:
        """是否启用了定时触发"""
        if not self.enabled:
            return False
        mode = self.schedule.mode
        if mode == "cron":
            return self.enabled_cron
        return self.schedule.has_trigger()

    @property
    def cooldown_seconds(self) -> int:
        """触发冷却秒数（0 表示不冷却）"""
        try:
            return max(0, int(self.cooldown or 0))
        except (TypeError, ValueError):
            return 0

    def resolved_inject_position(self, global_position: str = "user_input") -> str:
        """
        解析最终注入位置

        - inject_position == "default" 时跟随全局配置
        - 非法值回退为 user_input
        """
        position = str(self.inject_position or "default").strip()
        if position == "default":
            position = global_position or "user_input"
        if position not in ("system_prompt", "user_input"):
            logger.warning(
                f"[条目:{self.name}] 未知的注入位置 {position!r}，已回退为 user_input"
            )
            position = "user_input"
        return position

    @property
    def in_cron_window(self) -> bool:
        """
        是否处于定时激活窗口内（结构化 schedule 与高级 cron 共用）
        """
        if not self.schedule_enabled:
            return False
        if self._cron_fired_at is None:
            return False

        # 全天/时间段模式：窗口终点由定时配置决定（与 duration 无关）
        if self._cron_window_end is not None:
            return time.time() <= self._cron_window_end

        # duration <= 0 表示永久窗口
        if self.duration <= 0:
            return True

        return time.time() <= self._cron_fired_at + self.duration

    # ==================================================
    # 激活决策
    # ==================================================

    def _allow_scope(
        self,
        *,
        user_id: str,
        group_id: str,
        session_id: str,
        is_admin: bool,
    ) -> bool:
        """scope 权限大门"""
        if not self.scope:
            return True

        for s in self.scope:
            if s == "admin" and is_admin:
                return True
            if s == user_id:
                return True
            if s == group_id:
                return True
            if s == session_id:
                return True
        return False

    def _has_text_token(self, text: str | None) -> bool:
        """是否具备文本激活资格"""
        if not text:
            return False
        if not self.enabled_keywords:
            return False
        return self._match_keywords(text)

    def _satisfy_probability(self) -> bool:
        """概率是否满足"""
        p = self.probability
        if p >= 1.0:
            return True
        if p <= 0.0:
            return False
        prob = random.random()
        if prob < p:
            logger.debug(f"[{self.name}] 概率激活成功: {prob} < {p}")
            return True
        return False

    def check_activate(
        self,
        *,
        text: str,
        user_id: str,
        group_id: str,
        session_id: str,
        is_admin: bool,
    ) -> bool:
        """
        统一激活判决, 在监听LLM消息时调用
            - 用于判定条目是否允许“进入 Session”
            - 不代表本次请求一定会注入
            - 只做判断，不修改任何状态；定时窗口由 consume_cron_window 显式消费
        """

        # Gate 1: 总开关
        if not self.enabled:
            return False

        # Gate 2: scope 权限大门
        if not self._allow_scope(
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
            is_admin=is_admin,
        ):
            return False

        # Gate 3: 激活方式（满足其一即可）
        text_hit = self._has_text_token(text)
        cron_hit = self.in_cron_window
        if not text_hit and not cron_hit:
            return False

        # Gate 4: 激活的概率
        if not self._satisfy_probability():
            return False

        return True

    def consume_cron_window(self, text: str = "") -> None:
        """
        消费定时激活窗口（cron 资格只消费一次）

        由调用方在条目通过全部门槛（含触发冷却）并确定进入会话后调用。
        旧版在 check_activate 内消费窗口，若条目随后被会话冷却拦下，
        窗口已被清空，导致定时条目当天/该时段再也无法触发（v2.6.0 修复）。

        - 文本触发与定时同时命中时保留窗口（文本激活本身不依赖窗口）
        """
        if text and self._has_text_token(text):
            return
        self._cron_fired_at = None
        self._cron_window_end = None

    def allow_consume(
        self,
        *,
        user_id: str,
        group_id: str,
        session_id: str,
        is_admin: bool,
    ) -> bool:
        """
        使用阶段 scope 判定
            - 仅检查 scope + enabled + active
            - 不包含关键词 / 概率 / cron
        """
        if not self.enabled:
            return False

        if not self.active:
            return False

        return self._allow_scope(
            user_id=user_id,
            group_id=group_id,
            session_id=session_id,
            is_admin=is_admin,
        )

    # ==================================================
    # 生命周期钩子
    # ==================================================

    def enter_session(self) -> None:
        """
        进入会话的唯一入口（激活发生点）
        """
        # 记录激活时间, 进入触发态
        self._activated_at = time.time()

    def on_consume(self) -> None:
        """
        记录一次使用（注入消耗）

        说明:
        - 每次注入后调用（无论注入 system_prompt 还是用户输入）
        - 仅影响运行期次数统计
        """
        self._inject_count += 1

    def on_cron_triggered(self) -> None:
        """
        被定时任务触发（结构化 schedule / 旧 cron 兼容模式），打开一次全局激活窗口

        - 全天/时间段模式：窗口终点由定时配置决定（全天=当日 24 点，时间段=起点+跨度）
        - 时刻模式：窗口沿用条目 duration
        """
        self._cron_fired_at = time.time()
        window = self.schedule.activation_window_seconds(self._cron_fired_at)
        self._cron_window_end = (
            self._cron_fired_at + window if window is not None else None
        )
        logger.debug(f"[schedule] 条目 {self.name} 定时已触发，等待消息激活")

    # ==================================================
    # 展示
    # ==================================================

    @staticmethod
    def format_duration(seconds: float) -> str:
        if seconds <= 0:
            return "0秒"

        seconds = int(seconds)

        # 10 分钟以内，直接用秒
        if seconds <= 600:
            return f"{seconds}秒"

        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        if days > 0:
            return f"{days}天{hours}小时" if hours else f"{days}天"
        if hours > 0:
            return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
        return f"{minutes}分"

    def display(self) -> str:
        """以 Markdown 表格形式展示条目配置与运行状态"""

        # ===== 基础状态 =====
        if not self.enabled:
            status_text = "禁用"
        elif self.active:
            status_text = "生效中"
        else:
            status_text = "待触发"

        # ===== 激活范围（scope）=====
        if not self.scope:
            scope_text = "所有会话"
        elif self.scope == ["admin"]:
            scope_text = "仅管理员"
        else:
            scope_text = ", ".join("管理员" if s == "admin" else s for s in self.scope)

        # ===== 生命周期 =====
        if self.duration == 0:
            duration_text = "永久"
        elif self.active:
            duration_text = f"剩余 {self.format_duration(self.duration)} 秒"
        else:
            duration_text = f"{self.duration} 秒"

        times_text = "不限次数" if self.times == 0 else f"{self.times} 次"
        probability_text = f"{int(self.probability * 100)}%"

        lines = [f"### 【{self.name}】"]

        # ===== 触发关键词（有就展示）=====
        if self.keywords:
            keywords_text = "  |  ".join(self.keywords)
            lines.append(f"- 正则触发:  {keywords_text}")

        # ===== 定时规则（有就展示）=====
        schedule_text = describe_schedule(self.schedule)
        if schedule_text:
            lines.append(f"- 定时触发:  {schedule_text}")

        # ===== 注入位置 / 触发冷却 =====
        position_text = {
            "system_prompt": "System Prompt 末尾",
            "user_input": "用户消息末尾",
        }.get(self.inject_position, "跟随全局")
        lines.append(f"- 注入位置:  {position_text}")

        if self.cooldown_seconds > 0:
            lines.append(
                f"- 触发冷却:  {self.format_duration(self.cooldown_seconds)}（激活后冷却期内不再激活）"
            )

        lines.extend(
            [
                "| 状态 | 优先级 | 生效范围 | 生效时长 | 生效次数 | 生效概率 |",
                "| ---- | ------ | -------- | -------- | -------- | -------- |",
                f"| {status_text} | {self.priority} | {scope_text} | {duration_text} | {times_text} | {probability_text} |",
            ]
        )
        # ===== 内容 =====
        lines.extend(
            [
                "```",
                self.content.strip(),
                "```",
            ]
        )

        return "\n".join(lines)

    def display_remaining(self):
        """显示条目当前剩余时间和次数"""
        parts = []

        if self.duration > 0:
            parts.append(f"剩{self.format_duration(self.remaining_time)}")
        else:
            parts.append("∞")

        if self.times > 0:
            parts.append(f"{self.remaining_times}次")
        else:
            parts.append("∞")

        return f"{self.name}({'、'.join(parts)})" if parts else self.name
