# core/scheduler.py
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .schedule import (
    _APS_DOW,
    HolidayProvider,
    build_cron_trigger,
    describe_schedule,
    parse_time_hhmm,
)

if TYPE_CHECKING:
    from .entry import LoreEntry
    from .lorebook import Lorebook
    from .session import SessionCache


class LoreCronScheduler:
    """
    LoreEntry 定时激活调度器（结构化 schedule / 高级 cron → 触发条目）

    - daily/weekly：按每个触发时刻注册一条任务，星期在 trigger 上过滤
    - cron       ：高级模式，沿用 5 段 cron 表达式
    - 日期范围 / 节假日过滤不进 trigger，在触发时刻校验（天然支持调休）
    """

    def __init__(self, lorebook: Lorebook, sessions: SessionCache):
        # 只依赖两个“纯业务对象”，不依赖 plugin / event
        self._lorebook = lorebook
        self._sessions = sessions

        # 节假日 / 工作日判断（离线包 + 在线兜底）
        self._holidays = HolidayProvider(lorebook.cfg.data_dir)

        # APScheduler 本体
        self._scheduler = AsyncIOScheduler()
        self._started = False

        # 订阅 Lorebook 的变更事件
        self._lorebook.on_changed.append(self.reload)

    # ========== 生命周期 ==========

    def start(self) -> None:
        """
        启动调度器：
        - 注册所有合法定时任务
        - 启动 AsyncIOScheduler

        只允许启动一次
        """
        if self._started:
            return

        self._register_all()
        self._scheduler.start()
        self._started = True
        logger.debug("[schedule] scheduler started")

    def shutdown(self) -> None:
        """
        停止调度器（插件卸载 / 进程退出时调用）
        """
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        logger.debug("[schedule] scheduler stopped")

    def reload(self) -> None:
        """
        重新加载所有定时任务

        使用场景：
        - 新增 / 删除条目
        - 修改条目的 schedule / cron / enabled 字段
        """
        if not self._started:
            return

        self._scheduler.remove_all_jobs()
        self._register_all()
        logger.debug("[schedule] scheduler reloaded")

    # ========== 内部实现 ==========

    def _register_all(self) -> None:
        """
        遍历所有 entry，注册其定时任务
        """
        for entry in self._lorebook.list_entries():
            if entry.schedule_enabled:
                self._try_register_entry(entry)

    def _try_register_entry(self, entry: LoreEntry) -> None:
        """
        尝试为单个 entry 注册定时任务
        """
        schedule = entry.schedule

        # 高级模式：手写 5 段 cron
        if schedule.mode == "cron":
            try:
                trigger = build_cron_trigger(entry.cron)
            except Exception as e:
                logger.warning(
                    f"[schedule] 条目 {entry.name} cron 无效，已忽略: "
                    f"{entry.cron} ({e})"
                )
                return
            self._add_job(entry.name, f"loreentry:{entry.name}", trigger)
            logger.debug(f"[schedule] 已注册定时条目: {entry.name} (cron {entry.cron})")
            return

        # 结构化模式：daily / weekly
        day_of_week = None
        if schedule.mode == "weekly":
            day_of_week = ",".join(
                _APS_DOW[w - 1] for w in sorted(set(schedule.weekdays))
            )

        for token in sorted(set(schedule.times)):
            parsed = parse_time_hhmm(token)
            if parsed is None:
                continue
            hour, minute = parsed
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
            job_id = f"loreentry:{entry.name}:{hour:02d}:{minute:02d}"
            self._add_job(entry.name, job_id, trigger)

        logger.debug(
            f"[schedule] 已注册定时条目: {entry.name} "
            f"({describe_schedule(schedule, entry.cron)})"
        )

    def _add_job(self, entry_name: str, job_id: str, trigger) -> None:
        # job_id 唯一，确保 reload / 覆盖是幂等的
        self._scheduler.add_job(
            self._on_trigger,
            trigger=trigger,
            args=[entry_name],
            id=job_id,
            replace_existing=True,
        )

    async def _on_trigger(self, entry_name: str) -> None:
        entry = self._lorebook.get_entry(entry_name)
        if not entry or not entry.enabled:
            return

        # 触发时刻校验：日期范围 / 节假日过滤
        # （weekly 的星期约束已由 trigger 承担，这里不再重复）
        schedule = entry.schedule
        if schedule.mode != "none":
            today = datetime.now().date()
            if not await schedule.matches_date(
                today, self._holidays, check_weekday=False
            ):
                logger.debug(
                    f"[schedule] 条目 {entry.name} 今日不满足日期/节假日条件，跳过触发"
                )
                return

        # 只通知 entry：定时已触发
        entry.on_cron_triggered()
