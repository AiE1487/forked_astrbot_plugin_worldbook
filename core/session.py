# core/session.py
from __future__ import annotations

import copy
import json
import time

from astrbot.api import logger

from .config import PluginConfig
from .entry import LoreEntry


class SessionCache:
    """会话级 Prompt 缓存"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        # umo -> active LoreEntry list（列表顺序 = 触发顺序，先激活的在前）
        self._data: dict[str, list[LoreEntry]] = {}

        # 触发冷却：umo -> {entry_name: 上次激活时间戳}
        self._cooldowns: dict[str, dict[str, float]] = {}
        self._cooldown_path = config.data_dir / "cooldowns.json"
        self._load_cooldowns()

    # ================= 查询 =================

    def get_sorted_active(self, umo: str) -> list[LoreEntry]:
        """
        获取当前会话的有效条目（按触发顺序排列，先激活的在前）
        不活跃的会被直接移除
        """
        entries = self._data.get(umo)
        if not entries:
            return []

        # 只保留活跃的
        active_entries = [e for e in entries if e.active]

        if not active_entries:
            self._data.pop(umo, None)
            return []

        # 回写：确保 data 里只有活跃的
        self._data[umo] = active_entries
        return active_entries

    def attach(self, umo: str, entries: list[LoreEntry]) -> None:
        """
        将条目挂载到会话中

        - 列表顺序即触发顺序：本次新触发的条目排在末尾（注入时排在最后）
        - 条目同名则覆盖，且触发顺序刷新（移到最后）
        - allow_same_priority=False：同 priority 只保留最新触发的条目
        """

        # 1. 取出旧条目
        old_entries = self.get_sorted_active(umo)

        # 2. 深拷贝新条目
        new_entries: list[LoreEntry] = [copy.deepcopy(e) for e in entries]
        new_names = {e.name for e in new_entries}

        # 3. 合并：旧条目保持原顺序（剔除被覆盖的），新触发的追加到末尾
        merged: list[LoreEntry] = [e for e in old_entries if e.name not in new_names]
        merged.extend(new_entries)

        # 4. 按 priority 合并（可选）：同 priority 只保留最新触发的
        if not self.cfg.allow_same_priority:
            kept: list[LoreEntry] = []
            seen_priorities: set[int] = set()
            for e in reversed(merged):
                if e.priority in seen_priorities:
                    logger.debug(
                        f"优先级[{e.priority}]冲突，仅保留最新触发的条目: {e.name}"
                    )
                    continue
                seen_priorities.add(e.priority)
                kept.append(e)
            merged = list(reversed(kept))

        # 挂载条目到会话下
        self._data[umo] = merged

        # 激活最终条目
        for e in merged:
            e.enter_session()

        # 记录本次新触发条目的激活时间（用于触发冷却）
        cooldown_names = [e.name for e in new_entries if e.cooldown_seconds > 0]
        self.mark_activated(umo, cooldown_names)

        logger.debug(f"已挂载并激活条目: {[e.name for e in merged]}")

    def remove(self, umo: str, names: list[str]) -> list[str]:
        """
        从会话中移除指定名称的条目
        返回成功移除的条目名称
        """
        entries = self._data.get(umo)
        if not entries:
            return []

        names_set = set(names)
        remain: list[LoreEntry] = []
        removed: list[str] = []

        for e in entries:
            if e.name in names_set:
                removed.append(e.name)
            else:
                remain.append(e)

        if remain:
            self._data[umo] = remain
            logger.debug(f"Removed {removed} from {umo}")
        else:
            self._data.pop(umo, None)
            logger.debug(f"Removed all entries from {umo}")

        return removed

    def remove_everywhere(self, name: str) -> int:
        """
        从所有会话中移除指定条目（删除条目后调用）
        返回移除的会话副本数量
        """
        removed = 0
        for umo in list(self._data):
            entries = self._data.get(umo, [])
            remain = [e for e in entries if e.name != name]
            if len(remain) != len(entries):
                removed += len(entries) - len(remain)
                if remain:
                    self._data[umo] = remain
                else:
                    self._data.pop(umo, None)
        return removed

    def refresh_entry(self, name: str, master: LoreEntry) -> None:
        """
        用主条目刷新所有会话中的同名激活副本（保留其运行状态）

        WebUI 修改条目参数后调用，让正在生效的副本立即使用新配置。
        """
        for umo, entries in self._data.items():
            for idx, e in enumerate(entries):
                if e.name != name:
                    continue
                fresh = copy.deepcopy(master)
                # 保留运行态：激活时间 / 注入次数 / 定时窗口
                fresh._activated_at = e._activated_at
                fresh._inject_count = e._inject_count
                fresh._cron_fired_at = e._cron_fired_at
                entries[idx] = fresh

    def clear(self, umo: str) -> None:
        """
        强制清除会话的所有prompts
        """
        self._data.pop(umo, None)
        logger.debug(f"[SessionManager] clear session {umo}")

    # ================= 触发冷却 =================

    def _load_cooldowns(self) -> None:
        try:
            if self._cooldown_path.exists():
                data = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cooldowns = {
                        str(umo): {str(n): float(ts) for n, ts in bucket.items()}
                        for umo, bucket in data.items()
                        if isinstance(bucket, dict)
                    }
        except Exception as e:
            logger.warning(f"[session] 冷却记录加载失败: {e}")

    def _save_cooldowns(self) -> None:
        try:
            self._cooldown_path.write_text(
                json.dumps(self._cooldowns, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[session] 冷却记录保存失败: {e}")

    def mark_activated(self, umo: str, names: list[str]) -> None:
        """记录条目在本会话的激活时间（用于触发冷却）"""
        names = [n for n in names if n]
        if not names:
            return
        now = time.time()
        bucket = self._cooldowns.setdefault(umo, {})
        for name in names:
            bucket[name] = now
        self._save_cooldowns()

    def cooldown_remaining(self, umo: str, name: str, cooldown: int) -> float:
        """查询剩余冷却秒数；<= 0 表示不在冷却中"""
        if cooldown <= 0:
            return 0.0
        ts = self._cooldowns.get(umo, {}).get(name)
        if ts is None:
            return 0.0
        return max(0.0, ts + cooldown - time.time())

    def in_cooldown(self, umo: str, name: str, cooldown: int) -> bool:
        """条目在本会话是否处于触发冷却中"""
        return self.cooldown_remaining(umo, name, cooldown) > 0
