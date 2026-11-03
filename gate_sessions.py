# -*- coding: utf-8 -*-
"""AgentGate · 能力之门 —— 会话总览（插件页面用）。

这个模块只做一件事：**把机器人见过的会话整理成一张表**，供插件页面显示，
并让页面能直接在表上增删 AgentGate 的白名单。

为什么白名单管理会放在 AgentGate 里：AgentGate 的白名单本来就是
「用户 openid / 群 openid / 会话 ID 写在同一张表里，命中任意一个即放行」，
和这张表的语义完全一致。

关于数据来源，有两个硬约束（都在服务器上实测过）：

  1. **QQ 官方在单聊事件里不下发昵称**（`author.username` 恒为空字符串，
     官方三个示例都是 ""）。同一个人的 openid 在群聊和单聊里是同一串，
     所以这里攒一张 `openid → 昵称` 表，遇到私聊就把名字"借"过来。
  2. 群名 / 成员数 / 机器人角色 / 接收设置 只能实时调 QQ 官方接口拿：
     `GET /v2/groups/{group_openid}/info` 和 `/bot_state`。

关于多机器人（同一个 AstrBot 同时挂几个 QQ 官方机器人）：

  * openid 是**按机器人隔离**的 —— 同一个群 / 同一个人，在不同机器人下是不同
    的一串 ID。所以「openid 属于哪个机器人」必须跟着记录一起存（`platform_id`），
    查群信息时只能用**这个机器人自己的令牌**，查不了就如实说原因，绝不借用别的实例。
  * 机器人可能被停用 / 删除，但它的会话不能凭空消失；这里维护一张
    `bot_registry`（见过的机器人）并且把这类会话标成 `bot_unavailable`。

页面取数**不能**用 fetch，也不能优先用 bridge，见 `write_data_js()` 的注释。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

#: QQ 官方平台适配器的名字
QQ_PLATFORM = "qq_official"
#: 所有走 QQ 官方接口的适配器类型（webhook 版是同一个官方接口）
QQ_ADAPTERS = frozenset({"qq_official", "qq_official_webhook"})
#: 本插件的目录名（StarTools 的数据目录按它分）
PLUGIN_DIR_NAME = "astrbot_plugin_agent_gate"
#: 会话登记表最多留多少个
STORE_MAX = 200
#: 登记表文件名
STORE_FILE = "sessions.json"
#: 登记表结构版本（v3 起带机器人注册表）
STORE_VERSION = 3
#: 群信息缓存时间（秒）
GROUP_META_TTL = 60
#: 群信息读取失败后的缓存时间（秒）—— 失败也要缓存，但别缓存一小时
META_FAIL_TTL = 15
#: 单次群信息接口超时（秒）
META_TIMEOUT = 8
#: 并发拉群信息的上限
META_CONCURRENCY = 4

#: 群信息错误的分类
ERROR_KINDS = ("not_in_group", "bot_auth", "bot_unavailable", "unknown")

#: bot_state.recv_msg_setting → 人话
RECV_CN = {
    "all": "全量消息",
    "only_mention": "仅 @ 消息",
    "mention_and_context": "@ 消息 + 近 10 条上下文",
}
#: bot_state.member_role → 人话
ROLE_CN = {"member": "普通成员", "admin": "管理员", "owner": "群主"}

PATH_GROUP_INFO = "/v2/groups/{group_openid}/info"
PATH_BOT_STATE = "/v2/groups/{group_openid}/bot_state"


class GateAPIError(Exception):
    """调用 QQ 官方群接口失败。"""


def _parse_umo(umo: str) -> tuple[str, str, str]:
    """把 `平台实例ID:消息类型:会话ID` 拆成三段（只切前两刀）。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) < 3:
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "", "")
    return parts[0], parts[1], parts[2]


def is_qq_adapter(name: Any) -> bool:
    """适配器类型是不是 QQ 官方机器人（含 webhook 版）。"""
    return str(name or "").strip().lower() in QQ_ADAPTERS


def classify_meta_error(text: Any) -> str:
    """把 QQ 接口的原始报错归成几类，供页面说人话。

    顺序有讲究：先认「非群成员」——它同时带「鉴权失败」字样，
    如果先判 token 会把它误报成令牌问题。
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    if (
        "非群成员" in raw
        or "not in group" in raw
        or "not a member" in raw
        or ("group_openid" in raw and "无效" in raw)
    ):
        return "not_in_group"
    if (
        "access_token" in raw
        or ("token" in raw and "invalid" in raw)
        or "401" in raw
        or "unauthorized" in raw
        or "鉴权失败" in raw
    ):
        return "bot_auth"
    if "botpy" in raw or "平台适配器" in raw or "找不到该平台" in raw:
        return "bot_unavailable"
    return "unknown"


def meta_error_text(kind: str, label: str) -> str:
    """错误分类 → 页面正文（原始报错另放 error_detail，不占正文）。"""
    name = str(label or "").strip() or "该机器人"
    if kind == "not_in_group":
        return f"机器人「{name}」不在该群（可能已退群/被踢），读不到群名与角色"
    if kind == "bot_auth":
        return f"机器人「{name}」的令牌无效或已过期，请检查机器人配置"
    if kind == "bot_unavailable":
        return f"机器人「{name}」尚未就绪，或该适配器不支持群信息查询"
    return "读群信息失败（原始报错见悬停提示 / 诊断面板）"


def _ago_text(ts: Any) -> str:
    """时间戳 → 「3 分钟前」；没记录过返回 —。"""
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    delta = max(0, int(time.time() - value))
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


class QQGroupAPI:
    """复用 qq_official 适配器里 botpy 的 BotHttp，不额外配 appid/secret。"""

    def __init__(self, http: Any) -> None:
        self._http = http

    async def _get(self, path: str, group_openid: str) -> dict[str, Any]:
        try:
            from botpy.http import Route
        except Exception as exc:  # pragma: no cover
            raise GateAPIError("未找到 botpy，无法调用 QQ 官方接口") from exc
        route = Route("GET", path, group_openid=group_openid)
        try:
            data = await self._http.request(route)
        except Exception as exc:
            raise GateAPIError(f"GET {path} 调用失败：{exc}") from exc
        return data if isinstance(data, dict) else {}

    async def get_group_info(self, group_openid: str) -> dict[str, Any]:
        return await self._get(PATH_GROUP_INFO, group_openid)

    async def get_bot_state(self, group_openid: str) -> dict[str, Any]:
        return await self._get(PATH_BOT_STATE, group_openid)


class SessionBook:
    """机器人见过的会话总览 + 页面数据文件。"""

    def __init__(self, plugin: Any, plugin_dir: Path) -> None:
        self.plugin = plugin
        self.plugin_dir = Path(plugin_dir)
        self.sessions: dict[str, dict[str, Any]] = {}
        #: 见过的 QQ 官方机器人：{platform_id: {"adapter": 类型, "last_seen": ts}}
        self.bot_registry: dict[str, dict[str, Any]] = {}
        self.openid_names: dict[str, str] = {}
        self._group_meta: dict[str, tuple[float, dict[str, Any]]] = {}
        self._loaded = False
        self._db_seeded = False
        self._last_persist = 0.0
        self._last_write = 0.0
        self._task: asyncio.Task[None] | None = None

    # ══════════════ 落盘 ══════════════

    def _store_path(self) -> Path | None:
        try:
            from astrbot.api.star import StarTools

            # ⚠️ 不能直接写 self.plugin.PLUGIN_DIR_NAME：AgentGate 的
            # PLUGIN_DIR_NAME 是**模块级**常量，不是类属性，取它会 AttributeError
            # 被这里吞掉 → _store_path() 返回 None → 登记表永远不落盘，
            # 每次重启丢掉累积的昵称和消息数。所以要有模块级兜底。
            name = str(getattr(self.plugin, "PLUGIN_DIR_NAME", "") or PLUGIN_DIR_NAME)
            base = Path(StarTools.get_data_dir(name))
            return base / STORE_FILE
        except Exception:  # noqa: BLE001
            return None

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._store_path()
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"AgentGate 会话总览: 登记表读取失败: {exc}")
            return
        for item in payload.get("sessions") or []:
            if isinstance(item, dict) and item.get("umo"):
                self.sessions[str(item["umo"])] = item
        names = payload.get("openid_names")
        if isinstance(names, dict):
            for key, value in names.items():
                key_s, val_s = str(key or "").strip(), str(value or "").strip()
                if key_s and val_s:
                    self.openid_names[key_s] = val_s
        for item in self.sessions.values():
            sid = str(item.get("sender_id") or "").strip()
            nickname = str(item.get("sender_name") or "").strip()
            if sid and nickname:
                self.openid_names.setdefault(sid, nickname)
        registry = payload.get("bot_registry")
        if isinstance(registry, dict):
            for key, value in registry.items():
                pid = str(key or "").strip()
                if not pid:
                    continue
                item = value if isinstance(value, dict) else {}
                entry: dict[str, Any] = {"adapter": str(item.get("adapter") or "").strip()}
                try:
                    entry["last_seen"] = float(item.get("last_seen") or 0)
                except (TypeError, ValueError):
                    entry["last_seen"] = 0.0
                self.bot_registry[pid] = entry
        # v2 迁移：老登记表没有 bot_registry，用会话记录里的适配器类型反推
        for record in self.sessions.values():
            pid = str(record.get("platform_id") or "").strip()
            adapter = str(record.get("adapter") or record.get("platform_name") or "").strip()
            if pid and is_qq_adapter(adapter):
                self.bot_registry.setdefault(pid, {"adapter": adapter, "last_seen": 0.0})

    def persist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_persist < 30:
            return
        self._last_persist = now
        path = self._store_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps(
                {
                    "version": STORE_VERSION,
                    "sessions": list(self.sessions.values()),
                    "bot_registry": self.bot_registry,
                    "openid_names": self.openid_names,
                },
                ensure_ascii=False,
                indent=2,
            )
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"AgentGate 会话总览: 登记表落盘失败: {exc}")

    # ══════════════ 机器人（平台实例）══════════════

    def _registry_note(self, platform_id: str, adapter: str = "") -> None:
        """记一笔见过的机器人；重启后靠它认出「停用机器人的历史会话」。"""
        pid = str(platform_id or "").strip()
        if not pid:
            return
        entry = self.bot_registry.setdefault(pid, {})
        entry["label"] = pid
        if str(adapter or "").strip():
            entry["adapter"] = str(adapter).strip()
        entry["last_seen"] = time.time()

    def _live_adapter(self, platform_id: str) -> Any:
        """当前在线的平台适配器实例；不是 QQ 官方适配器就返回 None。"""
        pid = str(platform_id or "").strip()
        if not pid:
            return None
        try:
            adapter = self.plugin.context.get_platform_inst(pid)
        except Exception:  # noqa: BLE001
            return None
        if adapter is None:
            return None
        try:
            name = str(getattr(adapter.meta(), "name", "") or "")
        except Exception:  # noqa: BLE001
            return None
        return adapter if is_qq_adapter(name) else None

    def _known_bots(self) -> dict[str, str]:
        """{platform_id: 适配器类型}：在线实例 + 注册表里见过的（含已停用）。"""
        bots: dict[str, str] = {}
        try:
            insts = (
                getattr(
                    getattr(self.plugin.context, "platform_manager", None),
                    "platform_insts",
                    None,
                )
                or []
            )
        except Exception:  # noqa: BLE001
            insts = []
        for inst in insts:
            try:
                meta = inst.meta()
                name = str(getattr(meta, "name", "") or "")
                pid = str(getattr(meta, "id", "") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if pid and is_qq_adapter(name):
                bots[pid] = name
        for pid, item in self.bot_registry.items():
            adapter = ""
            if isinstance(item, dict):
                adapter = str(item.get("adapter") or "").strip()
            if pid and pid not in bots and is_qq_adapter(adapter):
                bots[pid] = adapter
        return bots

    def _sync_bots(self) -> None:
        """把当前在线的 QQ 机器人写进注册表（不用等第一条消息）。"""
        try:
            insts = getattr(
                getattr(self.plugin.context, "platform_manager", None),
                "platform_insts",
                None,
            ) or []
        except Exception:  # noqa: BLE001
            return
        for inst in insts:
            try:
                meta = inst.meta()
                name = str(getattr(meta, "name", "") or "")
                pid = str(getattr(meta, "id", "") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if pid and is_qq_adapter(name):
                self._registry_note(pid, name)

    def _bot_state(self, platform_id: str) -> dict[str, Any]:
        """某条会话所属机器人的状态。只看它自己，绝不回退到别的实例。"""
        pid = str(platform_id or "").strip()
        adapter = self._live_adapter(pid)
        if adapter is not None:
            try:
                name = str(adapter.meta().name or "")
            except Exception:  # noqa: BLE001
                name = QQ_PLATFORM
            return {"platform_id": pid, "enabled": True, "adapter": name, "label": pid}
        entry = self.bot_registry.get(pid) or {}
        adapter = str(entry.get("adapter") or "") if isinstance(entry, dict) else ""
        return {
            "platform_id": pid,
            "enabled": False,
            "adapter": adapter,
            "label": pid or "未知机器人",
        }

    # ══════════════ 记录 ══════════════

    def remember(self, event: Any) -> None:
        """每条入站消息记一笔（不回复、不打断任何流程）。"""
        try:
            adapter = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return
        if not is_qq_adapter(adapter):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        platform_id, message_type, session_id = _parse_umo(umo)
        if not session_id:
            return
        group_id = str(event.get_group_id() or "").strip()
        record = self.sessions.get(umo) or {}
        record.update(
            {
                "umo": umo,
                "platform_id": platform_id,
                "platform_name": adapter,
                "adapter": adapter,
                "message_type": message_type,
                "session_id": session_id,
                "group_openid": group_id,
                "user_openid": "" if group_id else session_id,
                "sender_id": str(event.get_sender_id() or ""),
                "sender_name": str(event.get_sender_name() or ""),
                "last_seen": time.time(),
            }
        )
        try:
            record["msg_count"] = int(record.get("msg_count") or 0) + 1
        except (TypeError, ValueError):
            record["msg_count"] = 1
        self.sessions[umo] = record
        self._registry_note(platform_id, adapter)

        sender_id = str(record.get("sender_id") or "").strip()
        sender_name = str(record.get("sender_name") or "").strip()
        if sender_id and sender_name:
            self.openid_names[sender_id] = sender_name
            if len(self.openid_names) > STORE_MAX * 2:
                for key in list(self.openid_names)[:STORE_MAX]:
                    self.openid_names.pop(key, None)

        if len(self.sessions) > STORE_MAX:
            oldest = sorted(
                self.sessions.items(), key=lambda kv: kv[1].get("last_seen") or 0
            )
            for key, _ in oldest[: len(self.sessions) - STORE_MAX]:
                self.sessions.pop(key, None)

        self.persist()

    def seed_workspaces(self) -> None:
        """从 AstrBot 的工作区目录补出历史私聊会话。

        agent 运行器会为每个会话建 `data/workspaces/_<消息类型>_<会话ID>`；
        私聊没有别的历史来源（DB 里没有单聊记录），只能靠这个。
        """
        try:
            # 注意：不要用 get_astrbot_data_path()，从插件里调它返回的是
            # 插件自己的数据目录；workspaces 有专门的 helper。
            from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path

            root = Path(get_astrbot_workspaces_path())
        except Exception:  # noqa: BLE001
            return
        if not root.is_dir():
            return

        platform_id = ""
        bots = self._known_bots()
        if len(bots) == 1:
            platform_id = next(iter(bots))
        elif len(bots) > 1:
            # 工作区目录名里没有「哪个机器人」，多机器人时只能猜 —— 不猜。
            # 私聊会话本来就能从数据库按 platform_id 精确补出来。
            logger.info(
                "AgentGate 会话总览: 检测到 %d 个 QQ 机器人，跳过工作区猜测（私聊改由数据库补出）",
                len(bots),
            )
            return
        if not platform_id:
            return

        added = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        for item in entries:
            if not item.is_dir() or not item.name.startswith("_"):
                continue
            kind, _, sid = item.name[1:].partition("_")
            if kind != "FriendMessage" or not sid:
                continue
            umo = f"{platform_id}:FriendMessage:{sid}"
            if umo in self.sessions:
                continue
            self.sessions[umo] = {
                "umo": umo,
                "platform_id": platform_id,
                "platform_name": QQ_PLATFORM,
                "adapter": QQ_PLATFORM,
                "message_type": "FriendMessage",
                "session_id": sid,
                "group_openid": "",
                "user_openid": sid,
                "sender_id": sid,
                "sender_name": "",
                "last_seen": 0.0,
                "msg_count": 0,
                "from_workspace": True,
            }
            added += 1
        if added:
            logger.info(f"AgentGate 会话总览: 从工作区补出 {added} 个历史私聊会话")

    # ══════════════ 群信息 ══════════════

    def _api_for(self, platform_id: str) -> QQGroupAPI:
        """只按给定的平台实例取适配器；找不到就直接失败，绝不换一个实例。"""
        adapter = self.plugin.context.get_platform_inst(platform_id)
        if adapter is None:
            raise GateAPIError("找不到该平台实例")
        if not is_qq_adapter(getattr(adapter.meta(), "name", "")):
            raise GateAPIError("该平台不是 QQ 官方机器人")
        http = getattr(getattr(adapter, "client", None), "http", None)
        if http is None:
            raise GateAPIError("平台适配器尚未就绪")
        return QQGroupAPI(http)

    async def group_meta(self, platform_id: str, group_openid: str) -> dict[str, Any]:
        key = f"{platform_id}:{group_openid}"
        now = time.time()
        cached = self._group_meta.get(key)
        if cached:
            ttl = GROUP_META_TTL
            prev = cached[1]
            if str(prev.get("info_error") or "") or str(prev.get("state_error") or ""):
                ttl = META_FAIL_TTL
            if now - cached[0] < ttl:
                return cached[1]
        result: dict[str, Any] = {"info": {}, "state": {}, "info_error": "", "state_error": ""}
        try:
            api = self._api_for(platform_id)
        except Exception as exc:  # noqa: BLE001
            result["info_error"] = str(exc)
            result["state_error"] = str(exc)
            self._group_meta[key] = (now, result)
            return result
        try:
            info = await api.get_group_info(group_openid)
            if isinstance(info, dict):
                result["info"] = info
        except Exception as exc:  # noqa: BLE001
            result["info_error"] = str(exc)
        try:
            state = await api.get_bot_state(group_openid)
            if isinstance(state, dict):
                result["state"] = state
        except Exception as exc:  # noqa: BLE001
            result["state_error"] = str(exc)
        self._group_meta[key] = (now, result)
        return result

    # ══════════════ 组装 ══════════════

    async def _seed_from_db(self) -> bool:
        """把框架里的历史会话补进登记表；平台适配器没就绪时返回 False 下次再试。"""
        try:
            db = self.plugin.context.get_db()
        except Exception:  # noqa: BLE001
            return True
        conversations: list[Any] = []
        try:
            for page in (1, 2, 3):
                chunk = await db.get_all_conversations(page=page, page_size=200)
                if not chunk:
                    break
                conversations.extend(chunk)
                if len(chunk) < 200:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"AgentGate 会话总览: 读取历史会话失败: {exc}")
            return True
        if not conversations:
            return True
        known = self._known_bots()
        added = 0
        for conv in conversations:
            umo = str(getattr(conv, "user_id", "") or "")
            if not umo:
                continue
            if umo in self.sessions:
                added += 1
                continue
            platform_id, message_type, session_id = _parse_umo(umo)
            if not session_id:
                continue
            adapter = known.get(platform_id, "")
            if not is_qq_adapter(adapter):
                # 认不出是哪个 QQ 机器人（可能从没在本插件运行时启用过）——
                # 宁可不显示，也不把会话挂到别的机器人名下。
                continue
            is_group = "group" in message_type.lower()
            self.sessions[umo] = {
                "umo": umo,
                "platform_id": platform_id,
                "platform_name": adapter,
                "adapter": adapter,
                "message_type": message_type,
                "session_id": session_id,
                "group_openid": session_id if is_group else "",
                "user_openid": "" if is_group else session_id,
                "sender_id": "",
                "sender_name": "",
                "last_seen": 0.0,
                "msg_count": 0,
                "from_db": True,
            }
            added += 1
        return bool(added)

    async def ensure(self) -> None:
        self._sync_bots()
        if not self._loaded:
            self.load()
            self.seed_workspaces()
        if not self._db_seeded:
            self._db_seeded = await self._seed_from_db()

    async def _collect(self) -> list[dict[str, Any]]:
        whitelist = set(self.plugin.whitelist_ids())
        known = self._known_bots()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in list(self.sessions.values()):
            platform_id = str(record.get("platform_id") or "")
            adapter = (
                known.get(platform_id)
                or str(record.get("adapter") or record.get("platform_name") or "")
            )
            if not is_qq_adapter(adapter):
                continue
            session_id = str(record.get("session_id") or "")
            if not session_id:
                continue
            group_openid = str(record.get("group_openid") or "")
            is_group = bool(group_openid)
            openid = group_openid or session_id
            seen.add(openid)
            rows.append(
                {
                    "umo": str(record.get("umo") or ""),
                    "kind": "group" if is_group else "private",
                    "platform_id": platform_id,
                    "bot_label": platform_id or "未知机器人",
                    "bot_enabled": self._live_adapter(platform_id) is not None,
                    "openid": openid,
                    "group_openid": group_openid,
                    "last_seen": float(record.get("last_seen") or 0),
                    "msg_count": int(record.get("msg_count") or 0),
                    "whitelisted": openid in whitelist or session_id in whitelist,
                }
            )
        # 白名单里但没登记过的 ID 也要显示，否则删不掉
        for wid in whitelist:
            if wid in seen:
                continue
            rows.append(
                {
                    "umo": "",
                    "kind": "unknown",
                    "platform_id": "",
                    "openid": wid,
                    "group_openid": "",
                    "last_seen": 0.0,
                    "msg_count": 0,
                    "whitelisted": True,
                    "orphan": True,
                }
            )
        return rows

    async def _enrich(self, rows: list[dict[str, Any]]) -> None:
        groups = [r for r in rows if r["kind"] == "group"]
        if not groups:
            return
        sem = asyncio.Semaphore(META_CONCURRENCY)

        async def fill(row: dict[str, Any]) -> None:
            label = str(row.get("bot_label") or "")
            if not row.get("bot_enabled"):
                # 机器人停用 / 删除：**不查**。绝不借用别的机器人实例（令牌会对不上）
                row["error_kind"] = "bot_unavailable"
                row["error"] = (
                    f"该会话属于机器人「{label}」，但它现在没有启用或已被删除，"
                    "读不到群信息（不会用别的机器人顶替）"
                )
                return
            async with sem:
                try:
                    meta = await asyncio.wait_for(
                        self.group_meta(row["platform_id"], row["group_openid"]),
                        timeout=META_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    row["error_kind"] = "unknown"
                    row["error"] = "读取群信息超时（QQ 接口较慢，稍后重试）"
                    return
                except Exception as exc:  # noqa: BLE001
                    kind = classify_meta_error(exc) or "unknown"
                    row["error_kind"] = kind
                    row["error"] = meta_error_text(kind, label)
                    row["error_detail"] = str(exc)
                    return
            info = meta.get("info") or {}
            state = meta.get("state") or {}
            row["name"] = str(info.get("group_name") or "").strip()
            row["member_count"] = info.get("group_member_num")
            row["recv_setting"] = str(state.get("recv_msg_setting") or "")
            row["role"] = str(state.get("member_role") or "")
            row["proactive"] = state.get("allow_proactive_msg")
            detail = "；".join(
                str(meta.get(key) or "").strip()
                for key in ("info_error", "state_error")
                if str(meta.get(key) or "").strip()
            )
            if detail:
                kind = classify_meta_error(detail) or "unknown"
                row["error_kind"] = kind
                row["error"] = meta_error_text(kind, label)
                row["error_detail"] = detail
            else:
                row["error_kind"] = ""
                row["error"] = ""
                row["error_detail"] = ""

        await asyncio.gather(*(fill(r) for r in groups))

    @staticmethod
    def _sort_key(row: dict[str, Any]) -> tuple:
        # 同一机器人的会话排在一起；停用机器人的整组沉到最后
        bot_rank = 0 if row.get("bot_enabled") else 1
        rank = {"group": 0, "unknown": 1, "private": 2}.get(row["kind"], 3)
        count = row.get("member_count")
        count = int(count) if isinstance(count, int) else -1
        return (
            bot_rank,
            str(row.get("bot_label") or ""),
            rank,
            -count,
            -float(row.get("last_seen") or 0),
            str(row.get("name") or ""),
        )

    @staticmethod
    def _fallback_name(row: dict[str, Any]) -> str:
        if row["kind"] == "private":
            return "私聊会话"
        if row["kind"] == "unknown":
            return "未登记的 ID"
        label = str(row.get("bot_label") or "未知机器人")
        if row.get("error_kind") == "bot_unavailable":
            return f"（机器人「{label}」未启用）"
        if row.get("error_kind") == "not_in_group":
            return "（机器人已不在该群）"
        return "（群名读取失败）"

    def _bots_payload(self) -> list[dict[str, Any]]:
        """机器人清单：在线的 + 注册表里见过的（后者 enabled=False）。"""
        bots: list[dict[str, Any]] = []
        for pid, adapter in sorted(self._known_bots().items()):
            bots.append(
                {
                    "platform_id": pid,
                    "label": pid,
                    "enabled": self._live_adapter(pid) is not None,
                    "adapter": adapter,
                    "sessions": 0,
                    "groups": 0,
                }
            )
        return bots

    async def build_payload(self) -> dict[str, Any]:
        await self.ensure()
        rows = await self._collect()
        try:
            await self._enrich(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"AgentGate 会话总览: 群信息补全失败（不影响列表）: {exc}")

        names = dict(self.openid_names)
        for record in self.sessions.values():
            sid = str(record.get("sender_id") or "").strip()
            nickname = str(record.get("sender_name") or "").strip()
            if sid and nickname:
                names.setdefault(sid, nickname)

        rows.sort(key=self._sort_key)
        sessions: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            is_group = row["kind"] == "group"
            name = row.get("name") or self._fallback_name(row)
            from_hint = False
            if not is_group:
                hint = names.get(str(row.get("openid") or ""))
                if hint:
                    name = hint
                    from_hint = True
            sessions.append(
                {
                    "index": index,
                    "kind": row["kind"],
                    "openid": row["openid"],
                    "platform_id": str(row.get("platform_id") or ""),
                    "bot_label": str(row.get("bot_label") or "未知机器人"),
                    "bot_enabled": bool(row.get("bot_enabled")),
                    "name": name,
                    "name_hint": "群里见过的昵称" if from_hint else "",
                    "recv_text": RECV_CN.get(str(row.get("recv_setting") or "").lower(), "—")
                    if is_group
                    else "—",
                    "role_text": ROLE_CN.get(str(row.get("role") or "").lower(), "—")
                    if is_group
                    else "—",
                    "proactive_text": ("—" if not is_group else
                                       ("已开启" if row.get("proactive") else "已关闭"))
                    if row.get("proactive") is not None or not is_group
                    else "—",
                    "member_count": row.get("member_count") if is_group else None,
                    "msg_count": int(row.get("msg_count") or 0),
                    "last_seen_text": _ago_text(row.get("last_seen")),
                    "whitelisted": bool(row.get("whitelisted")),
                    "error": row.get("error") or "",
                    "error_kind": row.get("error_kind") or "",
                    "error_detail": row.get("error_detail") or "",
                }
            )

        bots = self._bots_payload()
        per_bot: dict[str, int] = {}
        for item in sessions:
            pid = str(item.get("platform_id") or "")
            if pid:
                per_bot[pid] = per_bot.get(pid, 0) + 1
        for bot in bots:
            pid = bot["platform_id"]
            bot["sessions"] = per_bot.get(pid, 0)
            bot["groups"] = sum(
                1
                for item in sessions
                if item["platform_id"] == pid and item["kind"] == "group"
            )

        whitelist = self.plugin.whitelist_ids()
        by_openid: dict[str, dict[str, Any]] = {}
        for item in sessions:
            if item["kind"] in {"group", "private"}:
                by_openid.setdefault(str(item["openid"]), item)
        whitelist_detail: list[dict[str, Any]] = []
        for wid in whitelist:
            row = by_openid.get(str(wid))
            if row is None:
                whitelist_detail.append(
                    {"id": wid, "kind": "orphan", "bot_label": "", "name": ""}
                )
            else:
                whitelist_detail.append(
                    {
                        "id": wid,
                        "kind": row["kind"],
                        "bot_label": row["bot_label"],
                        "name": row["name"],
                    }
                )

        return {
            "sessions": sessions,
            "bots": bots,
            "whitelist": whitelist,
            "whitelist_detail": whitelist_detail,
            "summary": {
                "total": len(sessions),
                "whitelisted": sum(1 for s in sessions if s["whitelisted"]),
                "bots": len(bots),
                "bots_enabled": sum(1 for b in bots if b["enabled"]),
                "per_bot": per_bot,
            },
            "generated_at": int(time.time()),
        }

    # ══════════════ 页面数据文件 ══════════════

    def data_js_path(self) -> Path:
        return self.plugin_dir / "pages" / "whitelist" / "data.js"

    async def write_data_js(self) -> None:
        """把数据写成页面同目录下的静态 data.js。

        页面**不能**调 `context.register_web_api()` 注册的接口：那些路由挂在
        `/api/v1/plugins/extensions/...`，不属于 `PluginPageAuth.is_protected_path()`
        （只认 `/api/plugin/page/...`），所以 URL 里的 asset_token 会被中间件无视；
        而 Plugin Page 的 iframe 带 sandbox（opaque origin），跨源请求又带不上
        Dashboard 的登录 Cookie —— 结果必然 401 Missing API key。

        而 `/api/plugin/page/content/<插件>/<页面>/<文件>` 是受保护的，框架还会自动
        把页面里资源 URL 补上 asset_token。所以数据写成页面目录下的静态文件，
        页面用 `<script src="./data.js">` 读 —— 走的是框架自己的通道。
        """
        try:
            payload = await self.build_payload()
            body = (
                "// 由 astrbot_plugin_agent_gate 自动生成，不要手改\n"
                "window.__GATE_PAYLOAD__ = "
                + json.dumps(payload, ensure_ascii=False)
                + ";\n"
            )
            path = self.data_js_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
            logger.debug(
                "AgentGate 会话总览: 已刷新 data.js，%d 行",
                len(payload.get("sessions") or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"AgentGate 会话总览: 刷新 data.js 失败: {exc}")

    def schedule_write(self, min_interval: float = 15.0) -> None:
        now = time.time()
        if now - self._last_write < min_interval:
            return
        self._last_write = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.write_data_js())

    def start_background(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            for delay in (6, 18, 40):
                await asyncio.sleep(delay)
                await self.write_data_js()
            while True:
                await asyncio.sleep(45)
                await self.write_data_js()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"AgentGate 会话总览: 后台刷新退出: {exc}")

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.persist(force=True)
