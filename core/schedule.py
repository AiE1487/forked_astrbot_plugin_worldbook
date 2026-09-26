# core/schedule.py
"""
结构化定时模型

组成：
- ScheduleConfig   : 条目 schedule 字段的强类型视图（mode/times/weekdays/日期范围/节假日过滤）
- HolidayProvider  : 中国法定节假日/工作日判断（chinese-calendar 离线优先 + timor.tech 在线兜底 + 磁盘缓存）
- parse_cron_to_schedule : 旧 5 段 cron 表达式 → 结构化配置（尽力转换，转不了的保留为 mode=cron + expr）
- next_fire_times  : 计算未来 N 个触发时刻（WebUI「下一次执行日期」与可视化预览）
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import ConfigNode

# ===== 常量 =====

SCHEDULE_MODES = {"none", "daily", "weekly", "cron"}
DAY_FILTERS = {"all", "workday", "holiday"}

# 本插件星期约定：1=周一 ... 7=周日（与 ISO 一致）
WEEKDAY_NAMES = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}

# APScheduler 星期约定：0=周一 ... 6=周日
_APS_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# cron 星期名 → 本插件约定（crontab: 0/7=周日）
_CRON_DOW_NAMES = {"sun": 7, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# timor.tech 返回的 type: 0=工作日 1=周末 2=节假日 3=调休上班
_TIMOR_WORKDAY_TYPES = {0, 3}


# ===== 基础解析工具 =====


def parse_time_hhmm(text: Any) -> tuple[int, int] | None:
    """解析 HH:MM，返回 (小时, 分钟)；非法返回 None"""
    parts = str(text).strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def parse_date_iso(text: Any) -> str:
    """解析日期为 ISO 字符串（YYYY-MM-DD），兼容非零填充写法；非法返回空串"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    candidate = raw.replace("/", "-")
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    parts = candidate.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
        except ValueError:
            return ""
    return ""


def normalize_schedule_dict(raw: Any) -> dict[str, Any]:
    """
    把任意来源的 schedule 数据规范化为合法 dict（就地数据也用它清洗）

    兼容面板把 schedule 传成字符串（JSON）的情况。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raw = {}
        else:
            try:
                raw = json.loads(text)
            except Exception:
                logger.warning("[schedule] schedule 字段不是合法 JSON，已重置")
                raw = {}
    if not isinstance(raw, dict):
        raw = {}

    mode = str(raw.get("mode") or "none").strip().lower()
    if mode not in SCHEDULE_MODES:
        mode = "none"

    times: list[str] = []
    for item in raw.get("times") or []:
        parsed = parse_time_hhmm(item)
        if parsed is None:
            logger.debug(f"[schedule] 忽略非法时刻: {item!r}")
            continue
        hh, mm = parsed
        token = f"{hh:02d}:{mm:02d}"
        if token not in times:
            times.append(token)
    times = times[:48]

    weekdays: list[int] = []
    for item in raw.get("weekdays") or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 7 and value not in weekdays:
            weekdays.append(value)
    weekdays.sort()

    def _norm_time(value: Any) -> str:
        parsed = parse_time_hhmm(value)
        if parsed is None:
            return ""
        hh, mm = parsed
        return f"{hh:02d}:{mm:02d}"

    day_filter = str(raw.get("day_filter") or "all").strip().lower()
    if day_filter not in DAY_FILTERS:
        day_filter = "all"

    return {
        "mode": mode,
        "times": times,
        "weekdays": weekdays,
        "start_date": parse_date_iso(raw.get("start_date")),
        "end_date": parse_date_iso(raw.get("end_date")),
        "day_filter": day_filter,
        "all_day": bool(raw.get("all_day", False)),
        "time_start": _norm_time(raw.get("time_start")),
        "time_end": _norm_time(raw.get("time_end")),
        # mode=cron 时的原始 5 段 cron 表达式（旧版独立 cron 字段的迁移归属）
        "expr": str(raw.get("expr") or "").strip(),
    }


def is_empty_schedule(raw: Any) -> bool:
    """schedule 是否为空（未配置）"""
    if raw is None:
        return True
    if isinstance(raw, str):
        return not raw.strip()
    if isinstance(raw, dict):
        return not raw
    return False


# ===== cron 相关（从 scheduler.py 迁移，供调度器与预览共用） =====


def _normalize_weekday_field(field: str) -> str:
    """
    Convert standard crontab weekdays (0/7=Sun, 1=Mon, ..., 6=Sat)
    to APScheduler weekdays (0=Mon, ..., 6=Sun).
    """

    def normalize_token(token: str) -> str:
        token = token.strip().lower()
        if not token:
            return token
        if token in {"*", "sun", "mon", "tue", "wed", "thu", "fri", "sat"}:
            return token
        if token.isdigit():
            value = int(token)
            if not 0 <= value <= 7:
                raise ValueError(f"invalid weekday: {token}")
            return "6" if value in {0, 7} else str(value - 1)
        raise ValueError(f"invalid weekday: {token}")

    def normalize_part(part: str) -> str:
        base, *step = part.split("/", maxsplit=1)
        if "-" in base:
            start, end = base.split("-", maxsplit=1)
            base = f"{normalize_token(start)}-{normalize_token(end)}"
        else:
            base = normalize_token(base)

        if not step:
            return base
        return f"{base}/{step[0].strip()}"

    return ",".join(normalize_part(part) for part in field.split(","))


def build_cron_trigger(cron_expr: str) -> CronTrigger:
    minute, hour, day, month, weekday = str(cron_expr).split()
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_normalize_weekday_field(weekday),
    )


def _cron_field_values(
    field: str, names: dict[str, int], low: int, high: int
) -> list[int] | None:
    """
    解析 cron 数字列表字段（支持逗号、区间、跨区间回绕），返回去重排序的值列表。
    含 */步长 等无法展开为有限时刻列表的写法时返回 None。
    """
    values: list[int] = []
    for part in str(field).strip().split(","):
        part = part.strip().lower()
        if not part:
            return None
        base, _, step = part.partition("/")
        if step.strip() not in ("", "1"):
            return None
        base = base.strip()
        if base in ("*", "?"):
            values.extend(range(low, high + 1))
            continue
        if "-" in base:
            a, _, b = base.partition("-")
            va = int(a) if a.strip().isdigit() else names.get(a.strip())
            vb = int(b) if b.strip().isdigit() else names.get(b.strip())
            if va is None or vb is None:
                return None
            if va <= vb:
                values.extend(range(va, vb + 1))
            else:  # 跨回绕区间，如 5-1（周五到周一）
                values.extend(range(va, high + 1))
                values.extend(range(low, vb + 1))
            continue
        value = int(base) if base.isdigit() else names.get(base)
        if value is None or not low <= value <= high:
            return None
        values.append(value)
    return sorted(set(values))


def parse_cron_to_schedule(cron_expr: str) -> dict[str, Any]:
    """
    把 5 段 cron 表达式尽力转换为结构化 schedule。

    转换不了的（指定 日/月、步长、过复杂的组合）保留为 mode=cron，表达式存入 expr。
    返回值经过 normalize_schedule_dict 补全全部字段。
    """
    result: dict[str, Any] = {"mode": "cron", "expr": str(cron_expr or "").strip()}
    fields = str(cron_expr or "").split()
    if len(fields) == 5:
        minute_field, hour_field, dom_field, month_field, dow_field = fields
        try:
            if dom_field in ("*", "?") and month_field in ("*", "?"):
                minutes = _cron_field_values(minute_field, {}, 0, 59)
                hours = _cron_field_values(hour_field, {}, 0, 23)
                dows = _cron_field_values(dow_field, _CRON_DOW_NAMES, 0, 7)
                if minutes and hours and dows is not None and len(minutes) * len(hours) <= 24:
                    times = sorted(f"{h:02d}:{m:02d}" for h in hours for m in minutes)
                    weekdays = sorted({7 if v in (0, 7) else v for v in dows})
                    if not weekdays or set(weekdays) == set(range(1, 8)):
                        result = {"mode": "daily", "times": times}
                    else:
                        result = {"mode": "weekly", "times": times, "weekdays": weekdays}
        except Exception:
            result = {"mode": "cron", "expr": str(cron_expr or "").strip()}
    return normalize_schedule_dict(result)


# ===== 节假日 / 工作日判断 =====

try:  # 可选依赖：离线中国法定节假日数据（含调休）
    from chinese_calendar import is_workday as _cn_is_workday

    _chinese_calendar_available = True
except Exception:  # pragma: no cover - 宿主未安装时走在线/兜底
    _cn_is_workday = None
    _chinese_calendar_available = False


class HolidayProvider:
    """
    中国法定节假日 / 工作日判断

    优先级：
    1. chinese-calendar 离线数据（含法定调休）
    2. timor.tech 在线接口（结果落盘缓存到数据目录）
    3. 按自然周估算（周一至周五=工作日）并记录 warning
    """

    API_URL = "https://timor.tech/api/holiday/info/{ymd}"

    def __init__(self, data_dir: Path):
        self._cache_path = Path(data_dir) / "holiday_cache.json"
        # "YYYY-MM-DD" -> is_workday（仅缓存在线查询结果，离线数据不落盘）
        self._cache: dict[str, bool] = {}
        self._warned_dates: set[str] = set()
        self._load_cache()

    # ---- 磁盘缓存 ----

    def _load_cache(self) -> None:
        try:
            if self._cache_path.exists():
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache = {
                        str(k): bool(v)
                        for k, v in data.items()
                        if isinstance(v, bool)
                    }
        except Exception as e:
            logger.warning(f"[schedule] 节假日缓存加载失败: {e}")

    def _persist_cache(self) -> None:
        try:
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[schedule] 节假日缓存保存失败: {e}")

    # ---- 对外接口 ----

    def offline_workday(self, d: date) -> bool | None:
        """离线判断是否工作日；超出数据覆盖范围或依赖不可用时返回 None"""
        if not _chinese_calendar_available:
            return None
        try:
            return bool(_cn_is_workday(d))
        except NotImplementedError:
            return None
        except Exception as e:
            logger.debug(f"[schedule] chinese-calendar 判断失败 {d}: {e}")
            return None

    async def is_workday(self, d: date) -> bool:
        key = d.isoformat()
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        offline = self.offline_workday(d)
        if offline is not None:
            return offline

        online = await self._fetch_online(d)
        if online is not None:
            self._cache[key] = online
            self._persist_cache()
            return online

        fallback = d.weekday() < 5
        if key not in self._warned_dates:
            self._warned_dates.add(key)
            logger.warning(
                f"[schedule] 节假日数据不可用（离线包未覆盖且在线查询失败），"
                f"{key} 按自然周估算（周一至周五=工作日）"
            )
        return fallback

    async def is_holiday(self, d: date) -> bool:
        """是否休息日（法定节假日 / 调休放假 / 周末）"""
        return not await self.is_workday(d)

    async def _fetch_online(self, d: date) -> bool | None:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    self.API_URL.format(ymd=d.strftime("%Y-%m-%d"))
                ) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
            type_info = (payload or {}).get("date", {}).get("type", {})
            return int(type_info.get("type")) in _TIMOR_WORKDAY_TYPES
        except Exception as e:
            logger.debug(f"[schedule] 节假日在线查询失败 {d}: {e}")
            return None

    @property
    def coverage_year(self) -> int | None:
        """chinese-calendar 离线数据覆盖到的年份；依赖不可用时返回 None"""
        if not _chinese_calendar_available:
            return None
        year = date.today().year
        covered = year
        for probe in range(year, year + 6):
            try:
                _cn_is_workday(date(probe, 12, 31))
                covered = probe
            except Exception:
                break
        return covered


# ===== 条目 schedule 字段视图 =====


class ScheduleConfig(ConfigNode):
    """
    条目 schedule 字段的强类型视图

    - mode        : none / daily / weekly / cron（cron=旧数据兼容模式，表达式存 expr）
    - expr        : mode=cron 时的原始 5 段 cron 表达式（旧版独立 cron 字段已并入此处）
    - times       : 触发时刻列表 HH:MM（daily/weekly 的「时刻触发」方式）
    - weekdays    : 星期列表，1=周一 ... 7=周日（weekly）
    - start_date  : 起始日期 YYYY-MM-DD，空=不限（所有模式通用）
    - end_date    : 结束日期 YYYY-MM-DD，空=不限
    - day_filter  : all / workday / holiday（节假日过滤，所有模式通用）
    - all_day     : 全天触发（当天 00:00 起整天可激活）
    - time_start/time_end : 触发时间段 HH:MM（支持跨天，如 22:00~02:00）
    """

    mode: str
    expr: str
    times: list[str]
    weekdays: list[int]
    start_date: str
    end_date: str
    day_filter: str
    all_day: bool
    time_start: str
    time_end: str

    # ---- 触发方式 ----

    def trigger_span(self) -> str:
        """
        daily/weekly 的触发方式：all_day=全天 / range=时间段 / moment=时刻列表

        优先级：全天 > 时间段 > 时刻列表；时间段要求起止有效且不同。
        """
        if self.mode not in ("daily", "weekly"):
            return "moment"
        if self.all_day:
            return "all_day"
        if (
            self.time_start
            and self.time_end
            and self.time_start != self.time_end
        ):
            return "range"
        return "moment"

    def fire_times_of_day(self) -> list[str]:
        """当天实际的触发时刻（全天=00:00；时间段=开始时刻；时刻=times）"""
        span = self.trigger_span()
        if span == "all_day":
            return ["00:00"]
        if span == "range":
            return [self.time_start]
        return sorted(set(self.times))

    def activation_window_seconds(self, fired_at: float) -> float | None:
        """
        定时开窗的有效时长（秒）；None 表示沿用条目 duration

        - 全天：至当日 24 点（次日零点）
        - 时间段：结束 - 开始（跨天自动 +24 小时）
        - 时刻：None（沿用条目 duration）
        """
        span = self.trigger_span()
        if span == "all_day":
            fired = datetime.fromtimestamp(fired_at)
            next_midnight = datetime.combine(
                fired.date() + timedelta(days=1), dtime(0, 0)
            )
            return max(60.0, (next_midnight - fired).total_seconds())
        if span == "range":
            sh, sm = parse_time_hhmm(self.time_start) or (0, 0)
            eh, em = parse_time_hhmm(self.time_end) or (0, 0)
            minutes = (eh * 60 + em) - (sh * 60 + sm)
            if minutes <= 0:
                minutes += 24 * 60  # 跨天时间段，如 22:00~02:00
            return float(minutes * 60)
        return None

    def in_span_now(self, now: datetime | None = None) -> bool:
        """当前时刻是否处于触发方式覆盖的时间范围内（全天恒真，仅时间段做时刻判断）"""
        span = self.trigger_span()
        if span == "all_day":
            return True
        if span != "range":
            return False

        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        sh, sm = parse_time_hhmm(self.time_start) or (0, 0)
        eh, em = parse_time_hhmm(self.time_end) or (0, 0)
        start, end = sh * 60 + sm, eh * 60 + em
        if start < end:
            return start <= cur < end
        # 跨天时间段：如 22:00~02:00
        return cur >= start or cur < end

    def has_trigger(self) -> bool:
        """结构化配置本身是否可触发（不含 cron 模式，cron 由条目的 enabled_cron 判断）"""
        if self.mode not in ("daily", "weekly"):
            return False
        if self.mode == "weekly" and not self.weekdays:
            return False
        if self.trigger_span() in ("all_day", "range"):
            return True
        return bool(self.times)

    # ---- 日期校验 ----

    def _match_range_weekday(self, d: date, *, check_weekday: bool) -> bool:
        """日期范围 + 星期约束（ISO 字符串可直接字典序比较）"""
        ds = d.isoformat()
        if self.start_date and ds < self.start_date:
            return False
        if self.end_date and ds > self.end_date:
            return False
        if check_weekday and self.mode == "weekly" and self.weekdays:
            if d.isoweekday() not in self.weekdays:
                return False
        return True

    async def matches_date(
        self,
        d: date,
        holiday: "HolidayProvider",
        *,
        check_weekday: bool = True,
    ) -> bool:
        """
        指定日期是否满足触发条件（日期范围 + 星期 + 节假日过滤）

        - weekly 模式默认校验星期；cron 模式星期由表达式负责，调用方传 check_weekday=False
        """
        if not self._match_range_weekday(d, check_weekday=check_weekday):
            return False

        # 节假日 / 工作日过滤
        if self.day_filter == "workday":
            return await holiday.is_workday(d)
        if self.day_filter == "holiday":
            return await holiday.is_holiday(d)
        return True

    def matches_date_sync(
        self,
        d: date,
        holiday: "HolidayProvider",
        *,
        check_weekday: bool = True,
    ) -> bool:
        """
        同步版日期校验（启动追赶用）

        节假日仅用离线数据，超出覆盖范围按自然周估算（周一至周五=工作日）。
        """
        if not self._match_range_weekday(d, check_weekday=check_weekday):
            return False

        if self.day_filter in ("workday", "holiday"):
            offline = holiday.offline_workday(d)
            want_workday = self.day_filter == "workday"
            if offline is None:
                offline = d.weekday() < 5
            return offline == want_workday
        return True


def describe_schedule(schedule: ScheduleConfig) -> str:
    """把 schedule 转为人类可读描述（展示 / WebUI 用）"""
    mode = schedule.mode
    if mode == "cron":
        text = f"cron {schedule.expr}" if schedule.expr else ""
    elif mode in ("daily", "weekly"):
        span = schedule.trigger_span()
        if span == "all_day":
            span_text = "全天"
        elif span == "range":
            span_text = f"{schedule.time_start}~{schedule.time_end}"
        else:
            span_text = "、".join(schedule.times)
        if not span_text:
            return ""

        if mode == "daily":
            text = f"每天 {span_text}"
        else:
            if not schedule.weekdays:
                return ""
            names = "、".join(WEEKDAY_NAMES.get(w, str(w)) for w in schedule.weekdays)
            text = f"每周{names} {span_text}"
    else:
        return ""

    extras: list[str] = []
    if schedule.start_date:
        extras.append(f"{schedule.start_date} 起")
    if schedule.end_date:
        extras.append(f"{schedule.end_date} 止")
    if schedule.day_filter == "workday":
        extras.append("仅工作日")
    elif schedule.day_filter == "holiday":
        extras.append("仅节假日")
    if extras:
        text += "（" + "，".join(extras) + "）"
    return text


async def next_fire_times(
    schedule: ScheduleConfig,
    *,
    count: int = 3,
    holiday: HolidayProvider,
    now: datetime | None = None,
) -> list[datetime]:
    """
    计算未来 N 个触发时刻（本地时区，不含已过去的时刻）

    - daily/weekly：逐日校验日期范围/星期/节假日过滤后拼接触发时刻
      （全天=00:00；时间段=开始时刻；时刻=times 列表）
    - cron        ：用 APScheduler CronTrigger 逐个推算，并应用日期范围/节假日过滤
    """
    now = now or datetime.now()
    horizon = now + timedelta(days=400)
    results: list[datetime] = []

    if schedule.mode == "cron":
        try:
            trigger = build_cron_trigger(schedule.expr)
        except Exception:
            return []
        cursor = now
        for _ in range(2000):
            try:
                nxt = trigger.get_next_fire_time(None, cursor)
            except Exception:
                break
            if nxt is None:
                break
            if nxt.tzinfo is not None:
                nxt = nxt.astimezone().replace(tzinfo=None)
            if nxt > horizon:
                break
            if await schedule.matches_date(nxt.date(), holiday, check_weekday=False):
                results.append(nxt)
                if len(results) >= count:
                    break
            cursor = nxt + timedelta(minutes=1)
        return results

    times = sorted({t for t in schedule.fire_times_of_day() if parse_time_hhmm(t)})
    if not times:
        return []

    for offset in range(0, 401):
        day = (now + timedelta(days=offset)).date()
        if not await schedule.matches_date(day, holiday):
            continue
        for token in times:
            hh, mm = parse_time_hhmm(token) or (0, 0)
            moment = datetime.combine(day, dtime(hh, mm))
            if moment <= now:
                continue
            results.append(moment)
            if len(results) >= count:
                return results
    return results
