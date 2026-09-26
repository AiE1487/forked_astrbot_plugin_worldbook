# plugin.py

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star import Star
from astrbot.core.star.context import Context
from astrbot.core.star.filter.permission import PermissionType

from .core.config import PluginConfig
from .core.editor import LoreEditor
from .core.entry import LoreEntry
from .core.lorebook import Lorebook
from .core.scheduler import LoreCronScheduler
from .core.session import SessionCache
from .core.wildcard import WildcardResolver


# user_input 注入模式的固定包裹：
# 让模型明确区分"系统注入的参考资料"与"用户本人的发言"，格式固定以便模型稳定理解
USER_INPUT_TAG_OPEN = "<世界书设定参考>"
USER_INPUT_TAG_NOTE = (
    "以下为系统注入的世界书设定，仅作为本轮回复的参考，不是用户发言。"
)
USER_INPUT_TAG_CLOSE = "</世界书设定参考>"

# 历史消息中残留注入块的清理模式（只匹配本插件写入的完整标签对）
_WB_BLOCK_RE = re.compile(
    r"\s*<世界书设定参考>.*?</世界书设定参考>\s*", re.DOTALL
)


class WorldBookPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.cfg = PluginConfig(config)
        self.lorebook = Lorebook(self.cfg)
        self.sessions = SessionCache(self.cfg)
        self.style = None
        self.cron = LoreCronScheduler(self.lorebook, self.sessions)
        self.wildcards = WildcardResolver()
        self.web = None

    # ================= 生命周期 =================

    async def initialize(self):
        """加载插件时调用"""
        await self.lorebook.initialize()
        self.cron.start()

        try:
            import pillowmd

            self.style = pillowmd.LoadMarkdownStyles(self.cfg.style_dir)
        except Exception as e:
            logger.error(f"无法加载pillowmd样式：{e}")

        self.editor = LoreEditor(self.cfg, self.lorebook, self.sessions, self.style)

        # WebUI 管理页（需要 AstrBot >= v4.24.1 的 Plugin Pages 能力）
        try:
            from .core.web import WorldbookWeb

            self.web = WorldbookWeb(
                self.context, self.cfg, self.lorebook, self.sessions
            )
            self.web.register()
        except Exception as e:
            self.web = None
            logger.warning(f"[worldbook] WebUI 注册失败（需要 AstrBot v4.24.1+）: {e}")

    async def terminate(self):
        """插件卸载时调用"""
        self.cron.shutdown()

    # ================= 全局态命令 =================

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("查看条目")
    async def view_entry(self, event: AstrMessageEvent, arg: str | None = None):
        """查看条目（全部 / 启用 / 禁用 / 单个）"""
        async for msg in self.editor.view_entry(event, arg):
            await event.send(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("添加条目")
    async def add_entry(self, event: AstrMessageEvent, name: str):
        """添加条目 <名称> <内容>"""
        async for msg in self.editor.add_entry(event, name):
            await event.send(msg)

    @filter.llm_tool(name="worldbook_add_entry")
    async def llm_add_entry(
        self,
        event: AstrMessageEvent,
        name: str,
        content: str,
        keywords: str = "",
    ) -> str:
        """Add a worldbook entry.

        This can be used for lightweight memory, reusable rules, project or
        character context, user preferences, and compact summaries.

        Args:
            name(string): Short unique entry name, no more than 10 characters.
            content(string): Entry content to inject when activated.
            keywords(string): Optional trigger keywords or regex patterns separated by
                commas, spaces, or new lines. Defaults to name.

        Returns:
            A plain text result describing whether the entry was added.
        """
        name = str(name).strip()
        content = str(content).strip()
        keywords = str(keywords or "").strip()

        if not name:
            return "Worldbook entry add failed: name is required."
        if len(name) > 10:
            return (
                "Worldbook entry add failed: name must be no more than 10 characters."
            )
        if not content:
            return "Worldbook entry add failed: content is required."
        if self.lorebook.get_entry(name):
            return f"Worldbook entry add failed: entry already exists: {name}"

        trigger_keywords: list[str] = []
        raw_keywords = (
            keywords.replace("\uff0c", ",")
            .replace("\n", ",")
            .replace(" ", ",")
            .split(",")
        )
        for keyword in raw_keywords:
            keyword = keyword.strip()
            if keyword and keyword not in trigger_keywords:
                trigger_keywords.append(keyword)
            if len(trigger_keywords) >= 8:
                break
        if not trigger_keywords:
            trigger_keywords = [name]

        data = {
            "name": name,
            "keywords": trigger_keywords,
            "content": content,
        }

        try:
            names = self.lorebook.add_entries([data])
            if not names:
                return f"Worldbook entry add failed: entry already exists: {name}"
            return f"Worldbook entry added: {', '.join(names)}"
        except Exception as e:
            logger.error(f"worldbook_add_entry failed: {e}")
            return f"Worldbook entry add failed: {e}"

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删除条目")
    async def delete_entry(self, event: AstrMessageEvent):
        """删除条目 <名称1> <名称2>"""
        async for msg in self.editor.delete_entry(event):
            await event.send(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("设置触发词")
    async def set_keywords(self, event: AstrMessageEvent):
        """设置触发词 <关键词|正则表达式>"""
        async for msg in self.editor.set_keywords(event):
            await event.send(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("设置优先级")
    async def set_priority(self, event: AstrMessageEvent):
        """设置优先级 <数字>"""
        async for msg in self.editor.set_priority(event):
            await event.send(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("重命名条目")
    async def rename_entry(self, event: AstrMessageEvent):
        """重命名条目 <旧名称> <新名称>"""
        async for msg in self.editor.rename_entry(event):
            await event.send(msg)

    # ================= 会话态命令 =================

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("启用条目")
    async def enable_entry(self, event: AstrMessageEvent):
        """启用条目 <名称1> <名称2>"""
        async for msg in self.editor.enable_entry(event):
            await event.send(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("禁用条目")
    async def disable_entry(self, event: AstrMessageEvent):
        """禁用条目 <名称1> <名称2>"""
        async for msg in self.editor.disable_entry(event):
            await event.send(msg)

    @filter.command("条目状态")
    async def entries_state(self, event: AstrMessageEvent):
        """查看当前会话的条目状态"""
        async for msg in self.editor.entries_state(event):
            await event.send(msg)

    @filter.command("清除条目", alias={"清空条目"})
    async def clear_entries(self, event: AstrMessageEvent):
        """清除当前会话的某个条目，默认清除全部"""
        async for msg in self.editor.clear_entries(event):
            await event.send(msg)

    # ================= 核心机制 =================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        LLM 请求主入口

        执行顺序：
        1. 清理历史中残留的注入块
        2. 判决并挂载符合条件的条目（含会话级触发冷却过滤）
        3. 将会话中的条目按各自注入位置分组，按触发顺序统一追加到所属部分末尾
        """
        # Step 0：清理历史中残留的注入块（仅影响本次请求，不改动磁盘历史）
        self._clean_history_blocks(req)

        msg = event.message_str
        if not msg:
            return

        umo = event.unified_msg_origin

        # Step 1：判决 + 挂载
        self._decide_entries(event, msg, umo)

        # Step 2：使用会话中的条目
        self._consume_entries(event, req, umo)

    @staticmethod
    def _build_extra_part(text: str):
        """
        构造本轮注入的内容块（extra_user_content_parts 通道）

        - 优先 TextPart.mark_as_temp()：随本轮用户消息一并发给模型（位于用户
          发言之后 = 请求末尾，system_prompt 与历史前缀保持稳定，前缀缓存友好），
          且宿主保存会话历史时会剔除该块（_no_save），不污染聊天历史
        - 旧宿主无 TextPart 时回退为 dict（与 v2.3.0 行为一致）
        """
        try:
            from astrbot.core.agent.message import TextPart

            return TextPart(text=text).mark_as_temp()
        except Exception:
            return {"type": "text", "text": text}

    def _clean_history_blocks(self, req: ProviderRequest) -> None:
        """
        清理会话历史中残留的注入块

        早期版本将注入内容追加进 req.prompt，宿主会把它落盘到会话历史。
        这里在每次请求前剔除历史 user 消息中的注入块，保证模型看到的
        历史干净稳定（不改动磁盘上的历史记录）。
        """
        contexts = getattr(req, "contexts", None) or []
        for msg in contexts:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if USER_INPUT_TAG_OPEN in content:
                    # 占位空格，避免剔除后 content 为空导致部分模型接口报错
                    msg["content"] = _WB_BLOCK_RE.sub("", content).strip() or " "
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and USER_INPUT_TAG_OPEN in part["text"]
                    ):
                        part["text"] = (
                            _WB_BLOCK_RE.sub("", part["text"]).strip() or " "
                        )

    def _decide_entries(self, event, msg: str, umo: str) -> None:
        """
        判决与挂载阶段

        职责：
        - 遍历所有可用的 LoreEntry
        - 通过 LoreEntry.check_activate 做统一判决
        - 通过会话级触发冷却过滤
        - 消费定时激活窗口（在冷却检查之后）
        - 将通过判决的条目写入 Session
        """

        gid = event.get_group_id()
        uid = event.get_sender_id()
        is_admin = event.is_admin()

        candidates: list[LoreEntry] = []

        for e in self.lorebook.entries:
            # 所有是否“允许进入会话”的判断
            # 必须统一由 LoreEntry.check_activate 给出
            if not e.check_activate(
                text=msg,
                user_id=uid,
                group_id=gid,
                session_id=umo,
                is_admin=is_admin,
            ):
                continue

            # 会话级触发冷却：激活后冷却期内不再激活
            cooldown = e.cooldown_seconds
            if cooldown > 0:
                remaining = self.sessions.cooldown_remaining(umo, e.name, cooldown)
                if remaining > 0:
                    logger.debug(
                        f"[条目:{e.name}] 触发冷却中，剩余 {int(remaining)} 秒，跳过激活"
                    )
                    continue

            # 定时窗口消费必须在冷却检查之后：
            # 若先消费再被冷却拦下，定时条目当天/该时段将无法再次触发
            e.consume_cron_window(text=msg)

            candidates.append(e)

        if not candidates:
            return

        # 将通过判决的条目写入 Session
        self.sessions.attach(umo, candidates)

    def _consume_entries(
        self, event: AstrMessageEvent, req: ProviderRequest, umo: str
    ) -> None:
        """
        使用阶段

        职责：
        - 读取当前会话中已有的条目（按触发顺序，先激活的在前）
        - 进行使用阶段 scope / enabled / active 判定
        - 按触发顺序裁剪注入数量
        - 按条目各自的注入位置分组，统一追加到所属部分（system_prompt / 本轮用户输入）的末尾
        - 记录一次使用消耗
        """

        uid = event.get_sender_id()
        gid = event.get_group_id()
        is_admin = event.is_admin()

        # Step 0：取出会话中仍然处于 active 状态的条目（已按触发顺序排列）
        session_entries = self.sessions.get_sorted_active(umo)
        if not session_entries:
            return

        # Step 1：使用阶段 scope gate
        scoped_entries: list[LoreEntry] = []
        for e in session_entries:
            if e.allow_consume(
                user_id=uid,
                group_id=gid,
                session_id=umo,
                is_admin=is_admin,
            ):
                scoped_entries.append(e)
            else:
                logger.debug(f"[条目:{e.name}] 使用阶段 scope 不满足，已跳过")

        if not scoped_entries:
            return

        # Step 2：注入数量限制（仅影响本次请求，按触发顺序取前 N）
        max_count = self.cfg.max_inject_count
        inject_entries = scoped_entries

        if max_count > 0 and len(inject_entries) > max_count:
            dropped = inject_entries[max_count:]
            logger.debug(
                f"超出最大允许注入数 {max_count}，"
                f"已忽略 [{', '.join(e.name for e in dropped)}]"
            )
            inject_entries = inject_entries[:max_count]

        if not inject_entries:
            return

        logger.debug(f"当前会话实际注入条目：{[e.name for e in inject_entries]}")

        # Step 3：按条目各自的注入位置分组渲染（组内保持触发顺序）
        global_position = str(self.cfg.inject_position or "user_input")
        groups: dict[str, list[str]] = {"system_prompt": [], "user_input": []}

        for entry in inject_entries:
            title = f"## [{entry.name}]"
            rendered = self.wildcards.render(entry, event)
            section = f"{title}\n{rendered}"
            position = entry.resolved_inject_position(global_position)
            groups[position].append(section)

            # 一次注入视为一次使用
            entry.on_consume()

        # Step 4：system_prompt 组 —— 统一追加到系统提示词末尾
        if groups["system_prompt"]:
            block = "\n\n".join(groups["system_prompt"])
            req.system_prompt += "\n\n" + block + "\n\n"

        # Step 5：user_input 组 —— 统一追加到本轮用户输入末尾
        if groups["user_input"]:
            block = "\n\n".join(groups["user_input"])
            envelope = (
                "\n\n"
                + USER_INPUT_TAG_OPEN
                + "\n"
                + USER_INPUT_TAG_NOTE
                + "\n\n"
                + block
                + "\n"
                + USER_INPUT_TAG_CLOSE
            )

            extra_parts = getattr(req, "extra_user_content_parts", None)
            if extra_parts is not None:
                # 宿主的本轮临时内容通道：随本轮用户消息一并发给模型，但不落盘到
                # 会话历史（mark_as_temp），历史与缓存前缀保持稳定
                extra_parts.append(self._build_extra_part(envelope))
                logger.debug("世界书注入通道：extra_user_content_parts（不落盘）")
            else:
                # 旧版本宿主无此字段，回退为追加用户输入（会随会话历史保存）
                req.prompt += envelope
                logger.debug("世界书注入通道：req.prompt 追加（随会话历史保存）")
