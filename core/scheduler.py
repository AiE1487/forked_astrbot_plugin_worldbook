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
    LoreEntry 定时激活调度器（结构化 schedule / 旧 cron 兼容模式 → 触发条目）

    - daily/weekly：按每个触发时刻注册一条任务，星期在 trigger 上过滤
    - cron       ：旧数据兼容模式，沿用 5 段 cron 表达式（存于 schedule.expr）
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
        # 当前已注册定时任务的条目名（reload 时用于识别新增条目）
        self._registered_names: set[str] = set()

        # 订阅 Lorebook 的变更事件
        self._lorebook.on_changed.append(self.reload)

    # ========== 生命周期 ==========

    def start(self) -> None:
        """
        启动调度器：
        - 注册所有合法定时任务
        - 启动 AsyncIOScheduler
        - 追赶：全天/时间段条目若当前正处于当日窗口内（如机器人中途上线），补开激活窗口

        只允许启动一次
        """
        if self._started:
            return

        self._register_all()
        self._scheduler.start()
        self._started = True
        self._catch_up()
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

        新增的全天/时间段条目若当前处于窗口内，会立即补开激活窗口。
        """
        if not self._started:
            return

        previous = set(self._registered_names)
        self._scheduler.remove_all_jobs()
        self._register_all()
        new_names = self._registered_names - previous
        if new_names:
            self._catch_up(new_names)
        logger.debug("[schedule] scheduler reloaded")

    # ========== 内部实现 ==========

    def _register_all(self) -> None:
        """
        遍历所有 entry，注册其定时任务
        """
        self._registered_names = set()
        for entry in self._lorebook.list_entries():
            if entry.schedule_enabled:
                self._registered_names.add(entry.name)
                self._try_register_entry(entry)

    def _catch_up(self, names: set[str] | None = None) -> None:
        """
        追赶检查：全天/时间段条目若「今天满足日期条件且当前正处于窗口内」，
        补开一次激活窗口（机器人中途上线 / WebUI 新增条目的场景）。

        - 时刻模式不追赶（错过的时刻由 APScheduler misfire 策略处理）
        - 节假日过滤用离线数据同步判断，超出覆盖范围按自然周估算
        """
        now = datetime.now()
        today = now.date()
        for entry in self._lorebook.list_entries():
            if names is not None and entry.name not in names:
                continue
            if not entry.schedule_enabled:
                continue
            schedule = entry.schedule
            if schedule.mode not in ("daily", "weekly"):
                continue
            if schedule.trigger_span() not in ("all_day", "range"):
                continue
            # weekly 的星期约束在追赶时也需校验（追赶绕过了 trigger）
            if not schedule.matches_date_sync(today, self._holidays):
                continue
            if not schedule.in_span_now(now):
                continue
            entry.on_cron_triggered()
            logger.debug(f"[schedule] 条目 {entry.name} 处于当日窗口内，已补开激活窗口")

    def _try_register_entry(self, entry: LoreEntry) -> None:
        """
        尝试为单个 entry 注册定时任务
        """
        schedule = entry.schedule

        # 兼容模式：旧数据迁移保留的 cron 表达式（存于 schedule.expr）
        if schedule.mode == "cron":
            try:
                trigger = build_cron_trigger(schedule.expr)
            except Exception as e:
                logger.warning(
                    f"[schedule] 条目 {entry.name} cron 无效，已忽略: "
                    f"{schedule.expr} ({e})"
                )
                return
            self._add_job(entry.name, f"loreentry:{entry.name}", trigger)
            logger.debug(f"[schedule] 已注册定时条目: {entry.name} (cron {schedule.expr})")
            return

        # 结构化模式：daily / weekly（全天=00:00；时间段=开始时刻；时刻=times 列表）
        day_of_week = None
        if schedule.mode == "weekly":
            day_of_week = ",".join(
                _APS_DOW[w - 1] for w in sorted(set(schedule.weekdays))
            )

        span = schedule.trigger_span()
        grace = 3600 if span in ("all_day", "range") else None

        for token in sorted(set(schedule.fire_times_of_day())):
            parsed = parse_time_hhmm(token)
            if parsed is None:
                continue
            hour, minute = parsed
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
            job_id = f"loreentry:{entry.name}:{hour:02d}:{minute:02d}"
            self._add_job(entry.name, job_id, trigger, misfire_grace_time=grace)

        logger.debug(
            f"[schedule] 已注册定时条目: {entry.name} "
            f"({describe_schedule(schedule)})"
        )

    def _add_job(
        self,
        entry_name: str,
        job_id: str,
        trigger,
        misfire_grace_time: int | None = None,
    ) -> None:
        # job_id 唯一，确保 reload / 覆盖是幂等的
        kwargs: dict = {"replace_existing": True}
        if misfire_grace_time is not None:
            # 全天/时间段窗口长，短暂停机跨越触发点时仍补开窗口
            kwargs["misfire_grace_time"] = misfire_grace_time
        self._scheduler.add_job(
            self._on_trigger,
            trigger=trigger,
            args=[entry_name],
            id=job_id,
            **kwargs,
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
