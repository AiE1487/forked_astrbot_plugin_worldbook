# AGENTS.md

AstrBot 插件 `astrbot_plugin_worldbook`（世界书插件）：关键词/定时触发的提示词注入引擎，命中条件时把条目内容注入 `system_prompt`。本目录是从 GitHub fork 下载的源码副本（无 `.git`，不做任何 git 操作）。

## 目录结构

- `main.py` — 插件入口 `WorldBookPlugin(Star)`：注册中文聊天命令、LLM 工具 `worldbook_add_entry`、`on_llm_request` 钩子（判决 → 挂载 → 注入）。只做转发，业务逻辑在 `core/`。
- `core/` — 全部业务逻辑：
  - `config.py` — `ConfigNode`（dict → 强类型对象，schema 来自类型注解）与 `PluginConfig`（路径、`entry_storage` 持久化）
  - `entry.py` — `LoreEntry` 条目模型与激活判决：`can_activate` / `allow_consume` / `on_consume`
  - `template.py` — 条目模板枚举（default/common/resident/chance/schedule/user/group）及各模板字段默认值
  - `lorebook.py` — 业务层：`entry_map` 注册表，负责条目与持久化配置的同步
  - `lorefile.py` — 世界书 JSON/YAML 文件读写
  - `session.py` — `SessionCache`：按 umo（unified_msg_origin）的会话级激活条目缓存
  - `scheduler.py` — APScheduler cron 定时触发
  - `share.py` — 导入/导出（对齐酒馆 SillyTavern Lorebook 格式）
  - `wildcard.py` — `{user_id}` / `{user_name}` / `{time}` 通配符渲染
  - `editor.py` — 各命令的具体实现 + pillowmd 样式回复
- `default_lorebook.yaml` — 内置默认世界书（`entry_storage` 为空时自动加载），也是条目 YAML 格式的范本
- `pillowmd_style/` — pillowmd 消息渲染样式资源，勿动结构
- `_conf_schema.json` — AstrBot 配置面板 schema（`max_inject_count` / `allow_same_priority` / `entry_storage`）
- `metadata.yaml` — 插件名/版本号；`CHANGELOG.md` — 版本历史

## 架构规则

- 条目持久化在 AstrBot 配置的 `entry_storage`（list[dict]）里。改条目必须走 `core/lorebook.py` 的方法（如 `add_entries`）保持 `entry_map` 与 `entry_storage` 同步，不要直接改 config。
- 条目能否进入会话只由 `LoreEntry.can_activate` 判决，能否被注入只由 `allow_consume` 判决（均在 `main.py` 调用）。新增判定逻辑写在 `core/entry.py`，不要内联到 `main.py`。
- 新增配置项必须同时改 `_conf_schema.json` 与 `core/config.py` 中 `PluginConfig` 的类型注解（`ConfigNode` 用类型注解当 schema，缺字段只 warning 且不写回）。
- 新增命令：实现放 `core/editor.py`，`main.py` 只加装饰器转发；管理员命令加 `@filter.permission_type(PermissionType.ADMIN)`。
- LLM 工具的 docstring 就是给模型看的接口说明，参数格式 `name(string): 描述`，改函数时保持该格式。
- 依赖：`requirements.txt` 只写 AstrBot 未内置的包（目前仅 `pillowmd`）；`apscheduler`/`aiohttp`/`yaml` 由 AstrBot 宿主提供，不要重复声明。
- cron 星期字段：crontab 惯例 0/7=周日，`scheduler.py` 会归一化为 APScheduler 的 0=周一，改动时保持该换算。
- 日志统一 `from astrbot.api import logger`，不要用 print。

## 验证方式

本机没有 AstrBot 宿主环境，无法导入或运行插件（`astrbot` 模块不存在），至少做语法检查：

```bash
python -m py_compile main.py core/*.py
```

功能验证一律由用户上传打包文件到其 AstrBot 平台进行（见下）。

## 打包交付（必须遵守）

用户明确要求：**任何代码改动完成后，必须打包成 zip 交给用户，由用户上传到 AstrBot 运行平台测试**，不要假设能本地验证。

1. 更新 `metadata.yaml` 的 `version`（保持 `vX.Y.Z` 格式）并在 `CHANGELOG.md` 顶部新增对应小节。
2. 打包：zip 内顶层目录必须叫 `astrbot_plugin_worldbook/`（与 metadata 的 `name` 一致，工作区目录名 `forked_astrbot_plugin_worldbook-main` 不是合法包名，不能直接压缩本目录）。包含 `main.py`、`metadata.yaml`、`requirements.txt`、`_conf_schema.json`、`default_lorebook.yaml`、`core/`、`pillowmd_style/`、`README.md`、`LICENSE`、`CHANGELOG.md`、`logo.png`；排除 `__pycache__/`、`*.pyc`、`.v2c/`、`data/`、`dist/`。
3. 本机没有 `zip`/`7z`，用 Python 打包，产物输出到 `dist/`：

```bash
VERSION=v2.2.9   # 与 metadata.yaml 保持一致
python - <<'EOF'
import shutil, tempfile, os
version = os.environ.get("VERSION", "dev")
tmp = tempfile.mkdtemp()
dst = os.path.join(tmp, "astrbot_plugin_worldbook")
shutil.copytree(".", dst, ignore=shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".v2c", ".git", "data", "dist", "AGENTS.md"))
os.makedirs("dist", exist_ok=True)
shutil.make_archive(f"dist/astrbot_plugin_worldbook-{version}", "zip", tmp, "astrbot_plugin_worldbook")
print(f"dist/astrbot_plugin_worldbook-{version}.zip")
EOF
```

## 参考文档

- `README.md` — 用户视角的命令列表与条目字段说明
- `default_lorebook.yaml` — 条目 YAML 格式与各模板用法范本
- `CHANGELOG.md` — 历史版本的改动记录
