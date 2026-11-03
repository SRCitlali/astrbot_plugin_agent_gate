# -*- coding: utf-8 -*-
"""
AgentGate · 能力之门 v2.1.0
作者：茜特菈莉

一句话：只有白名单里的人能用这台机器人；名单内的人聊多了会累。

行为：
  · 白名单（用户 / 群 / 会话三种 ID 写在同一张表里）命中即放行。
  · 名单外：不分对话还是指令，一律拦下，随机回一句自定义文案。
  · 名单内：正常放行，但每回复一次按「基础消耗 + 字数 × 每字消耗」扣精力；
    精力见底后改为随机回一句（或不回），随时间自然恢复。
  · 开关：插件本身的启用/停用交给 AstrBot 的插件开关；插件内另有
    「白名单开关」与「精力开关」两个一级开关。

配置结构：
  一级：whitelist_enabled / energy_enabled
  二级：whitelist{entries, admin_exempt, recovery_messages, block_notify,
        block_cooldown_seconds} 与 energy{...}

拦截点说明（已对照 AstrBot 4.28.2 源码核实）：
  · 指令分发：`core/star/filter/command.py:191` 要求 event.is_at_or_wake_command 为真，
    而唤醒判定在 `waking_check/stage.py:118-165` 就完成了（早于处理器激活）。
    所以我们用「只对已唤醒消息激活」的处理器，能拦下指令，又不会像裸注册
    AdapterMessageEvent 那样把每条群消息都标记成「已唤醒」而污染框架判定。
  · `star_request.py:36-38` 的指令分发循环遇到 event.is_stopped() 立刻 break，
    因此我们停下事件后，后面的指令处理器不会再跑。
  · AdapterMessageEvent 处理器按 priority 降序执行（`star_handler.py:24-28`），
    priority=999 保证我们排在指令处理器之前。
  · `OnWaitingLLMRequestEvent` / `OnLLMRequestEvent` 只作为兜底（第三方 agent 路径、
    插件自己发起的 LLM 调用）。
  · `OnDecoratingResultEvent` 在发送消息之前触发，是唯一能拿到最终消息链的钩子，
    精力在这里结算（与天使之心同一个结算点）。
"""

import asyncio
import random
import re
import time
import traceback
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import (
    CustomFilter,
    EventMessageType,
    after_message_sent,
    command,
    custom_filter,
    event_message_type,
    llm_tool,
    on_decorating_result,
    on_llm_request,
    on_waiting_llm_request,
)
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .defense import (
    AUTO_DATA_DIR,
    DEFAULT_SETTINGS as DEFAULT_DEFENSE_CONFIG,
    DefenseStore,
    ban_key,
    evaluate,
    format_duration,
    format_evidence,
    mask_openid,
    settings_from_config,
    severity_of,
    system_hint,
    verify_quote,
)
from .energy import EnergyLedger
from .gate_sessions import SessionBook, is_qq_adapter

PLUGIN_NAME = "agent_gate"
PLUGIN_DIR_NAME = "astrbot_plugin_agent_gate"
PLUGIN_VERSION = "2.2.7"

NOTIFY_ALWAYS = "always"
NOTIFY_ON_MENTION = "on_mention"
NOTIFY_SILENT = "silent"
VALID_NOTIFY = (NOTIFY_ALWAYS, NOTIFY_ON_MENTION, NOTIFY_SILENT)

# 冷却表上限，防止长期运行下字典无限增长
MAX_COOLDOWN_ENTRIES = 512

# AstrBot 主配置里 admins_id 的出厂占位值，出现它等于"没有配管理员"
ADMIN_PLACEHOLDER = {"astrbot"}

# 唤醒阶段处理器的优先级：要排在所有指令处理器之前
GATE_WAKING_PRIORITY = 999

# 挂在 event 上的标记键（带前缀，避免与其它插件撞车）
ENERGY_ELIGIBLE_KEY = "agentgate_energy_eligible"
ENERGY_CHARGED_KEY = "agentgate_energy_charged"
BLOCKED_KEY = "agentgate_blocked"
#: 告状工具写在这里，装饰结果钩子读取后负责真正发出去
ALERT_PAYLOAD_KEY = "agentgate_alert_payload"
#: 本轮 LLM 请求是否已经注入过告状提示（避免多钩子重复注入）
HINT_INJECTED_KEY = "agentgate_hint_injected"

#: 私聊告警发送超时（秒）：QQ 官方主动消息可能被风控，不能把回复卡死
ALERT_SEND_TIMEOUT = 8

# 旧版配置键；只在配置迁移时读一次
LEGACY_WHITELIST_KEYS = (
    "whitelist_users",
    "whitelist_groups",
    "whitelist_sessions",
)
LEGACY_FLAT_WHITELIST_KEYS = (
    "admin_exempt",
    "block_notify",
    "block_cooldown_seconds",
)

DEFAULT_WHITELIST_CONFIG = {
    "entries": [],
    "admin_exempt": True,
    "recovery_messages": [
        "你不在本奶奶的白名单上，agent 能力一律不放行。"
        "你的 ID 是 {sender_id}，拿去给管理员报备就行",
        "生面孔呢。想用这台机器人，先把 ID 报给管理员——你的 ID 是 {sender_id}",
        "不认识你。{sender_id}，把这个拿去找管理员，别在这儿软磨硬泡",
    ],
    "block_notify": NOTIFY_ON_MENTION,
    "block_cooldown_seconds": 30,
}

# 精力组默认值：算法与数值全部照搬天使之心
DEFAULT_ENERGY_CONFIG = {
    "initial_energy": 100.0,
    "max_energy": 100.0,
    "min_energy": -100.0,
    "recovery_per_second": 0.6,
    "base_reply_cost": 14.0,
    "reply_cost_per_character": 0.12,
    "messages": [
        "……累了，今天不想搭理人。",
        "别喊了，本奶奶现在没力气陪你玩。",
        "精力见底了，改天再说吧。",
    ],
}

DEFAULT_CONFIG = {
    "whitelist_enabled": True,
    "energy_enabled": True,
    "whitelist": DEFAULT_WHITELIST_CONFIG,
    "energy": DEFAULT_ENERGY_CONFIG,
    "defense": dict(DEFAULT_DEFENSE_CONFIG),
}

# 当前存活的插件实例；唤醒阶段的过滤器要借它读配置。
_ACTIVE_PLUGIN = None


# ── 模块级纯函数（不依赖插件实例，便于单测） ──


def _as_list(raw) -> list:
    """把配置值规整成 list。容忍 None / 裸字符串 / 非序列类型。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return list(raw)
    return [raw]


def _to_set(raw) -> set:
    """规整成去空白的字符串集合。None / 空串一律丢弃。"""
    out = set()
    for item in _as_list(raw):
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.add(text)
    return out


def _dedup(raw) -> list:
    """去空白、去重、保持原顺序。"""
    out = []
    seen = set()
    for item in _as_list(raw):
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _clean_lines(raw) -> list:
    """文案列表：去空白、丢空行、保留重复（重复的句子也可以）。"""
    return [str(x).strip() for x in _as_list(raw) if str(x or "").strip()]


class _SafeDict(dict):
    """format_map 用：缺失的占位符原样保留，不抛 KeyError。"""

    def __missing__(self, key):
        return "{" + str(key) + "}"


def _render(template: str, values: dict) -> str:
    """渲染带占位符的文案；模板本身非法（如落单的 `{`）时回退原文。"""
    if not template:
        return ""
    try:
        return template.format_map(_SafeDict(values))
    except (KeyError, IndexError, ValueError):
        return template


def _match_whitelist(
    *,
    platform: str = "",
    instance: str = "",
    sender: str = "",
    session: str = "",
    umo: str = "",
    group: str = "",
    whitelist=None,
) -> bool:
    """白名单真值判定（纯函数，便于表驱动单测）。

    一张表管三种身份：发送者、会话、群。每个值都同时接受「裸 ID」、
    「平台实例ID:ID」与「适配器类型:ID」三种写法；完整会话串本身就带实例前缀，
    直接比对。

    Args:
        platform: 适配器类型（`qq_official`），兼容旧写法 `qq_official:xxx`。
        instance: 平台实例 ID（`茜特菈莉`）；多机器人时用它限定「只给这个机器人放行」。
        session: `event.get_session_id()`，私聊下通常等于发送者 ID。
        umo: `event.unified_msg_origin`，形如 `platform:message_type:session_id`。
    """
    entries = whitelist if whitelist is not None else set()
    if not entries:
        return False

    candidates = set()

    def _add(base: str, prefixes=()):
        base = str(base or "").strip()
        if not base:
            return
        candidates.add(base)
        for prefix in prefixes:
            prefix = str(prefix or "").strip()
            if prefix:
                candidates.add(f"{prefix}:{base}")

    prefixes = (instance, platform) if instance and instance != platform else (platform,)
    _add(sender, prefixes)
    _add(session, prefixes)
    _add(group, prefixes)
    _add(umo)

    return bool(candidates & entries)


# QQ 官方新版 @ 标记；适配器只剥掉机器人自己被 @ 的那段，别人的会留在正文里
_QQBOT_AT_RE = re.compile(r'<qqbot-at-user\s+id="([^"]+)"\s*/?>')
_OPENID_BRACKET_RE = re.compile(r"<@!?([^<>]+)>")


def _normalize_at_markup(text) -> str:
    """把带空格的 ``<qqbot-at-user id="X"/>`` 压成 ``<@X>``，方便按空白切词。"""
    return _QQBOT_AT_RE.sub(lambda m: f"<@{m.group(1)}>", str(text or ""))


def _clean_openid(raw) -> str:
    """把各种 @ 写法统一成裸 openid。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    match = _QQBOT_AT_RE.search(text)
    if match:
        return match.group(1).strip()
    match = _OPENID_BRACKET_RE.search(text)
    if match:
        return match.group(1).strip()
    if text.startswith("<@") and text.endswith(">"):
        return text[2:-1].lstrip("!").strip()
    return text.lstrip("@").strip()


def _extract_at_openid(event: AstrMessageEvent) -> str:
    """从消息链里取被 @ 的人（跳过机器人自身与全体成员）。"""
    try:
        self_id = str(event.get_self_id() or "")
    except Exception:
        self_id = ""
    try:
        messages = event.get_messages() or []
    except Exception:
        messages = []
    for comp in messages:
        qq = getattr(comp, "qq", None)
        if qq is None:
            continue
        qq = str(qq)
        if qq in {"", "all", "qq_official", self_id}:
            continue
        return qq
    return ""


class AgentGateWokenFilter(CustomFilter):
    """唤醒阶段处理器的过滤器：只在「门开着」且「消息已被唤醒」时激活。

    为什么必须要有它：
      `waking_check/stage.py:225-226` 规定——任何处理器只要过滤器通过，就会把
      `event.is_wake` 置为 True。如果裸注册一个「所有消息」的处理器，群里每句
      闲聊都会被标记成"已唤醒"，污染全 bot 的唤醒判定。
      而所有指令都要求 `is_at_or_wake_command`（`command.py:191`），这个标志由
      唤醒前缀 / @ / 私聊在处理器激活**之前**设置。所以「只对已唤醒消息激活」
      既能拦下指令，又不会新增任何唤醒标记。
    """

    def __init__(self, raise_error: bool = True, **kwargs) -> None:
        super().__init__(raise_error=raise_error, **kwargs)

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        plugin = _ACTIVE_PLUGIN
        if plugin is None:
            return False
        try:
            # 冷静期不依赖白名单开关：即使哪天把门关了，被投诉的人也得继续冷静。
            if not (plugin._gate_active() or plugin._defense_active()):
                return False
            return bool(event.is_at_or_wake_command)
        except Exception:
            return False


@register(
    PLUGIN_NAME,
    "茜特菈莉",
    "能力之门：只有白名单里的人能用，名单外连指令一并拦下；名单内的人聊多了会累",
    version=PLUGIN_VERSION,
)
class AgentGatePlugin(Star):
    """AgentGate · 能力之门 —— 黑曜石奶奶亲自看门"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._setup(config)
        self._log_startup_summary()

    def _setup(self, config) -> None:
        """初始化运行期状态（从 __init__ 拆出，便于单测直接构造实例）。"""
        global _ACTIVE_PLUGIN

        # 原始 AstrBotConfig（可 save_config），self.config 是合并后的普通 dict
        self.raw_config = config
        self.config = self._merge_config(config)

        # 白名单缓存（按配置签名失效，避免每条消息重复构建集合）
        self._wl_sig = None
        self._wl_entries: set = set()

        # 拦截回话冷却
        self._last_notify: dict = {}

        # 告状 / 冷静期：状态懒加载（第一次用到才碰磁盘，不影响加载速度）
        self._defense: DefenseStore | None = None
        self._defense_data_dir = AUTO_DATA_DIR  # 单测可注入临时目录 / None
        self._df_sig = None
        self._df_settings = dict(DEFAULT_DEFENSE_CONFIG)
        # 冷静期提示的「同一人别刷屏」时间表（monotonic 秒）
        self._ban_notice: dict = {}
        # /冷静测试推送 的限频时间戳
        self._last_test_dm = 0.0

        # 精力账本（照搬天使之心）
        self.energy = EnergyLedger(settings=self._energy_cfg)

        # 会话总览（插件页面用）。整段包在 try 里：这是附加功能，
        # 绝不能因为它让守门插件加载失败把人挡在门外。
        try:
            self.sessions = SessionBook(self, Path(__file__).resolve().parent)
            self._register_page_api()
            self.sessions.start_background()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AgentGate: 会话总览初始化失败（不影响守门）: {exc}")
            self.sessions = SessionBook(self, Path(__file__).resolve().parent)

        _ACTIVE_PLUGIN = self

    # ── 插件页面：会话总览 + 白名单增删 ──

    def _register_page_api(self) -> None:
        """注册页面「写」用的接口（读走的是页面目录下的静态 data.js）。

        为什么写还要注册接口：写操作必须执行插件代码，而所有能执行代码的路由都在
        `/api/v1/plugins/extensions/...`（非受保护路径）。页面里的 bridge 由父页面
        代发、带 Dashboard 的 Authorization 头，请求能到后端；响应可能丢，
        所以页面发完不等响应，等 1.5 秒重读 data.js 自己核对结果。
        """
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/whitelist",
                self._api_whitelist,
                ["GET", "POST"],
                "会话总览页面的白名单增删",
            )
            logger.info("AgentGate: 已注册插件页面接口 /%s/whitelist", PLUGIN_DIR_NAME)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"AgentGate: 插件页面接口注册失败（页面仍可只读）: {exc}")
        try:
            self.context.register_web_api(
                f"/{PLUGIN_DIR_NAME}/test_dm",
                self._api_test_dm,
                ["GET", "POST"],
                "私聊告警通道自检",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentGate: 私聊自检接口注册失败: %s", exc)

    async def _api_whitelist(self):
        """GET/POST ?action=add|remove&id=... 或 JSON body。"""
        try:
            from astrbot.api.web import json_response, request

            body = await request.json(default={}) or {}
            if not isinstance(body, dict):
                body = {}
            action = str(body.get("action") or "").strip().lower() or str(
                request.query.get("action", "") or ""
            ).strip().lower()
            target = str(body.get("id") or "").strip() or str(
                request.query.get("id", "") or ""
            ).strip()
            # 成功也要打日志 —— 否则"日志里没有请求"根本证明不了请求没到
            logger.info(
                "AgentGate 白名单接口: action=%s id=%s", action or "-", target or "-"
            )
            if action not in {"add", "remove"}:
                return json_response(
                    {"status": "error", "message": "action 只能是 add 或 remove", "data": {}}
                )
            if not target:
                return json_response(
                    {"status": "error", "message": "缺少 id", "data": {}}
                )
            result = self.whitelist_set(target, add=(action == "add"))
            if not result.get("ok"):
                return json_response(
                    {"status": "error", "message": result.get("error") or "操作失败", "data": {}}
                )
            # 关键：**同步**把 data.js 重写掉再返回。
            # save_config() 会让框架重载插件，异步任务可能来不及跑，
            # 页面按"重读 data.js 核对"的方式判断成败，晚一步就会误报失败。
            try:
                await self.sessions.write_data_js()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"AgentGate: 写白名单后刷新 data.js 失败: {exc}")
            logger.info(
                "AgentGate 白名单接口: 完成 changed=%s 共 %d 条",
                bool(result.get("changed")),
                len(result.get("ids") or []),
            )
            return json_response(
                {
                    "status": "ok",
                    "message": "",
                    "data": {
                        "changed": bool(result.get("changed")),
                        "action": action,
                        "id": target,
                        "whitelist": result.get("ids") or [],
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate 白名单接口异常: %s", exc)
            return {"status": "error", "message": str(exc), "data": {}}

    # ── 白名单读写（页面用；判定逻辑仍是 _whitelist_entries）──

    def whitelist_ids(self) -> list[str]:
        try:
            return sorted(str(x) for x in self._whitelist_entries())
        except Exception:  # noqa: BLE001
            return []

    def whitelist_set(self, target: str, add: bool) -> dict:
        value = str(target or "").strip()
        if not value:
            return {"ok": False, "error": "ID 不能为空"}
        ids = self.whitelist_ids()
        if add:
            if value in ids:
                return {"ok": True, "changed": False, "ids": ids}
            ids.append(value)
        else:
            if value not in ids:
                return {"ok": True, "changed": False, "ids": ids}
            ids = [x for x in ids if x != value]
        ids = sorted(ids)

        raw = getattr(self, "raw_config", None)
        if raw is None:
            return {"ok": False, "error": "拿不到可保存的配置对象"}
        # save_config() 可能让框架重载插件，先把登记表强刷一遍，
        # 否则重载后新实例读到的还是旧文件，累积的昵称/消息数就丢了。
        try:
            self.sessions.persist(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("AgentGate: 写白名单前刷新登记表失败: %s", exc)
        try:
            wl = raw.get("whitelist")
            if not isinstance(wl, dict):
                wl = {}
                raw["whitelist"] = wl
            wl["entries"] = list(ids)
            saver = getattr(raw, "save_config", None)
            if callable(saver):
                saver()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"AgentGate: 白名单保存失败: {exc}")
            return {"ok": False, "error": f"写配置失败：{exc}"}

        # 让内存里的合并配置和缓存立刻反映新名单
        try:
            self.config.setdefault("whitelist", {})["entries"] = list(ids)
        except Exception:  # noqa: BLE001
            pass
        self._wl_sig = None
        self.sessions.schedule_write(min_interval=0.0)
        return {"ok": True, "changed": True, "ids": ids}

    # ── 配置合并 ──

    @staticmethod
    def _merge_config(config) -> dict:
        """把传入配置合到默认值上。

        兼容三种历史结构：
          · v1.x：whitelist_users / whitelist_groups / whitelist_sessions 三张表，
            以及顶层的 admin_exempt / block_notify / block_cooldown_seconds / recovery_message
          · v2.0.0：顶层 whitelist 为列表，energy.enabled 作为精力开关
          · v2.1.0：whitelist / energy 两个二级组
        """
        merged = {
            "whitelist_enabled": True,
            "energy_enabled": True,
            "whitelist": dict(DEFAULT_WHITELIST_CONFIG),
            "energy": dict(DEFAULT_ENERGY_CONFIG),
            "defense": dict(DEFAULT_DEFENSE_CONFIG),
        }
        wl = merged["whitelist"]
        en = merged["energy"]
        df = merged["defense"]

        def _raw(key, default=None):
            try:
                return config.get(key, default)
            except Exception:
                return default

        # —— 一级开关 ——
        raw_wl_enabled = _raw("whitelist_enabled")
        if raw_wl_enabled is not None:
            merged["whitelist_enabled"] = bool(raw_wl_enabled)

        raw_energy = _raw("energy")
        raw_energy_enabled = _raw("energy_enabled")
        if (
            raw_energy_enabled is None
            and isinstance(raw_energy, dict)
            and "enabled" in raw_energy
        ):
            # v2.0.0：精力开关挂在精力组里
            raw_energy_enabled = raw_energy.get("enabled")
        if raw_energy_enabled is not None:
            merged["energy_enabled"] = bool(raw_energy_enabled)

        # —— 二级组：whitelist ——
        raw_wl = _raw("whitelist")
        if isinstance(raw_wl, dict):
            for key, value in raw_wl.items():
                if key in wl:
                    wl[key] = value
        elif isinstance(raw_wl, list):
            # v2.0.0：顶层 whitelist 直接就是列表
            wl["entries"] = _dedup(raw_wl)

        # —— 二级组：energy ——
        if isinstance(raw_energy, dict):
            for key, value in raw_energy.items():
                if key in en:
                    en[key] = value

        # —— 二级组：defense（v2.2.0 新增，没有历史结构要兼容）——
        raw_defense = _raw("defense")
        if isinstance(raw_defense, dict):
            for key, value in raw_defense.items():
                if key in df:
                    df[key] = value

        # —— 旧键回填 ——
        # 只有在「新的 whitelist.recovery_messages 没被显式配置」时，
        # 才用旧版的 recovery_message 覆盖默认文案。
        explicit_recovery = isinstance(raw_wl, dict) and "recovery_messages" in raw_wl
        legacy_recovery = _raw("recovery_message")
        if legacy_recovery and not explicit_recovery:
            wl["recovery_messages"] = [str(legacy_recovery)]
        for key in LEGACY_FLAT_WHITELIST_KEYS:
            legacy_value = _raw(key)
            if legacy_value is not None:
                wl[key] = legacy_value

        # —— v1.x 三张白名单表折叠 ——
        legacy_entries = []
        for key in LEGACY_WHITELIST_KEYS:
            legacy_entries.extend(_as_list(_raw(key)))
        legacy_entries = _dedup(legacy_entries)
        merged["_legacy_whitelist_count"] = len(legacy_entries)

        # —— 规整 ——
        wl["entries"] = _dedup(list(_as_list(wl.get("entries"))) + legacy_entries)
        wl["recovery_messages"] = _clean_lines(wl.get("recovery_messages"))
        en["messages"] = _clean_lines(en.get("messages"))
        df["messages"] = _clean_lines(df.get("messages"))
        return merged

    def _whitelist_cfg(self) -> dict:
        raw = self.config.get("whitelist")
        return raw if isinstance(raw, dict) else {}

    def _energy_cfg(self) -> dict:
        raw = self.config.get("energy")
        return raw if isinstance(raw, dict) else {}

    def _defense_cfg(self) -> dict:
        raw = self.config.get("defense")
        return raw if isinstance(raw, dict) else {}

    def _defense_settings(self) -> dict:
        """按配置签名缓存规整后的设置，避免每条消息重复解析。"""
        raw = self._defense_cfg()
        sig = tuple(sorted((str(k), str(v)) for k, v in raw.items()))
        if sig != self._df_sig:
            self._df_sig = sig
            self._df_settings = settings_from_config(raw)
        return self._df_settings

    def _defense_active(self) -> bool:
        return bool(self._defense_settings().get("enabled"))

    def _defense_store(self) -> DefenseStore:
        """懒加载状态存储：第一次用到才读盘，加载失败退化成纯内存。"""
        if self._defense is None:
            try:
                self._defense = DefenseStore(
                    getattr(self, "_defense_data_dir", AUTO_DATA_DIR)
                )
                self._defense.load()
            except Exception as exc:  # noqa: BLE001
                logger.error("AgentGate: 冷静期状态初始化失败，改为纯内存: %s", exc)
                self._defense = DefenseStore(None)
        return self._defense

    # ── 启动自检 ──

    def _log_startup_summary(self) -> None:
        entries = self._whitelist_entries()
        logger.info(
            "AgentGate v%s 已加载（白名单开关=%s | 精力开关=%s | 白名单%d条 | "
            "回话策略=%s | 冷却%ss | 精力=%s | 告状=%s）",
            PLUGIN_VERSION,
            "开" if self._whitelist_switch_on() else "关",
            "开" if self._energy_switch_on() else "关",
            len(entries),
            self._notify_mode(),
            self._cooldown_seconds(),
            self._energy_summary(),
            self._defense_summary(),
        )

        legacy_count = int(self.config.get("_legacy_whitelist_count") or 0)
        if legacy_count:
            logger.info(
                "AgentGate 已把旧配置里的 %d 条用户/群/会话白名单合并进新的 whitelist.entries。",
                legacy_count,
            )

        self._check_plugin_set()
        self._check_defense()

        if not self._whitelist_switch_on():
            logger.warning(
                "AgentGate 白名单开关为关闭状态：不检查名单，精力也一并停用，"
                "所有人都能正常使用（包括名单外的人）。"
            )
            return

        self._check_admin_exempt()
        self._check_whitelist_entries(entries)

        if not self._energy_switch_on():
            logger.info("AgentGate 精力开关为关闭状态：名单内的人可以一直聊。")
            return

        if self._energy_enabled():
            cfg = self._energy_cfg()
            logger.info(
                "AgentGate 精力已生效：只作用于白名单内的人（管理员%s），"
                "初始%s/上限%s/下限%s/每秒恢复%s/基础消耗%s/每字%s，"
                "精力<=0 时回话=%d 条候选%s",
                "豁免" if self._admin_exempt_on() else "不豁免",
                cfg.get("initial_energy"),
                cfg.get("max_energy"),
                cfg.get("min_energy"),
                cfg.get("recovery_per_second"),
                cfg.get("base_reply_cost"),
                cfg.get("reply_cost_per_character"),
                len(self._energy_messages()),
                "（为空则静默）" if not self._energy_messages() else "",
            )

    def _energy_summary(self) -> str:
        if not self._energy_switch_on() or not self._energy_enabled():
            return "关闭"
        return "开启(管理员%s)" % (
            "豁免" if self._admin_exempt_on() else "不豁免"
        )

    def _defense_summary(self) -> str:
        if not self._defense_active():
            return "关闭"
        settings = self._defense_settings()
        parts = ["演练" if settings.get("dry_run") else "生效"]
        target = self._effective_alert_openid()
        if target:
            parts.append(f"告警对象={mask_openid(target)}")
        else:
            parts.append("告警对象=" + self._alert_targets_summary())
        try:
            parts.append(f"冷静中={len(self._defense_store().list_bans())}")
        except Exception:  # noqa: BLE001
            pass
        return "开启(" + " | ".join(parts) + ")"

    def _alert_targets_summary(self) -> str:
        """启动日志里的告警对象摘要：按机器人分别绑定时显示数量，不猜具体是谁。"""
        try:
            targets = self._defense_store().alert_targets
        except Exception:  # noqa: BLE001
            return "未绑定"
        if not targets:
            return "未绑定"
        if len(targets) == 1:
            return mask_openid(next(iter(targets.values())))
        return f"{len(targets)} 个机器人分别绑定"

    def _check_defense(self) -> None:
        """告状/冷静期的启动自检：没绑定告警对象时提醒，别等真出事才发现没人收。"""
        if not self._defense_active():
            logger.info("AgentGate 告状与冷静期：关闭状态。")
            return
        settings = self._defense_settings()
        target = self._effective_alert_openid()
        targets = {}
        try:
            targets = self._defense_store().alert_targets
        except Exception:  # noqa: BLE001
            targets = {}
        configured_umo = str(settings.get("alert_umo") or "").strip()
        if configured_umo and configured_umo.count(":") < 2:
            logger.warning(
                "AgentGate 告状: alert_umo 填的是 %s，格式不对（应为 "
                "平台实例:FriendMessage:openid）。已改用自动拼接，建议清空该配置项。",
                mask_openid(configured_umo),
            )
        if not target and not targets:
            logger.warning(
                "AgentGate 告状与冷静期已开启，但没有绑定告警对象：被骂时只会写进"
                " evidence 文件和日志，不会 @ 你。让开发者本人发一次 /冷静绑定 即可。"
            )
        else:
            logger.info(
                "AgentGate 告状与冷静期已开启：告警对象=%s，模式=%s，"
                "中度/严重冷静期=%s/%s 分钟，%s次投诉封人",
                mask_openid(target) if target else self._alert_targets_summary(),
                "演练（不封人）" if settings.get("dry_run") else "生效",
                settings.get("ban_minutes_moderate"),
                settings.get("ban_minutes_severe"),
                settings.get("strikes_before_ban"),
            )
        try:
            active = self._defense_store().list_bans()
            if active:
                logger.info("AgentGate 当前有 %d 人处于冷静期。", len(active))
        except Exception as exc:  # noqa: BLE001
            logger.debug("AgentGate: 读取冷静期状态失败: %s", exc)

    def _check_plugin_set(self) -> None:
        """收窄 plugin_set 会让本插件的处理器被整段跳过，必须报警而不是静默放行。"""
        cfg = self._context_config()
        if cfg is None:
            return
        try:
            raw = cfg.get("plugin_set")
        except Exception:
            return
        if raw is None:
            return
        names = [str(x).strip() for x in _as_list(raw) if str(x or "").strip()]
        if not names or names == ["*"]:
            logger.info("AgentGate 启动自检：plugin_set=[\"*\"]，不会有处理器被过滤。")
            return
        if PLUGIN_NAME in names or PLUGIN_DIR_NAME in names:
            return
        logger.error(
            "AgentGate 失效告警：主配置 plugin_set=%s 既不含 %s 也不含 %s。"
            "框架会按 event.plugins_name 过滤事件处理器，本插件的处理器会被整段跳过，"
            "门将静默失效（fail-open）。请把插件加回 plugin_set，或改回 [\"*\"]。",
            names,
            PLUGIN_NAME,
            PLUGIN_DIR_NAME,
        )

    def _check_admin_exempt(self) -> None:
        if not self._admin_exempt_on():
            logger.info("AgentGate 管理员豁免已关闭：管理员同样受白名单与精力约束。")
            return
        admin_ids = self._resolve_admin_ids()
        if not admin_ids:
            logger.warning(
                "AgentGate 已开启「管理员豁免」，但未能从主配置读取到任何 "
                "admins_id，实际不会有人被豁免。请在 AstrBot 主配置中填写"
                "真实的用户 ID，或关闭该项并改用白名单。"
            )
        elif admin_ids == ADMIN_PLACEHOLDER:
            logger.warning(
                "AgentGate 的「管理员豁免」读到的是 admins_id 出厂占位值 %s，"
                "没有任何真实用户会命中。判定链是：主配置 admins_id → waking 阶段"
                "赋 event.role=\"admin\" → event.is_admin()。请填写真实 ID 或关闭该项。",
                sorted(ADMIN_PLACEHOLDER),
            )
        else:
            logger.info(
                "AgentGate 管理员豁免已启用，识别到 %d 个管理员 ID：%s",
                len(admin_ids),
                sorted(admin_ids),
            )

    def _check_whitelist_entries(self, entries: set) -> None:
        if not entries:
            logger.warning(
                "AgentGate 白名单为空：除被豁免的管理员外，所有人（含你自己）"
                "都会被拦下，连指令也用不了。启用前请务必先把 ID 填进 whitelist.entries。"
            )
        else:
            logger.info(
                "AgentGate 白名单共 %d 条；命中发送者/会话/群任意一个即放行。",
                len(entries),
            )

    def _context_config(self):
        """通过框架 API 读取 AstrBot 主配置（不硬编码路径）。"""
        cfg = None
        getter = getattr(self, "_get_context_config", None)
        if callable(getter):
            try:
                cfg = getter()
            except Exception:
                cfg = None
        if cfg is None:
            getter = getattr(getattr(self, "context", None), "get_config", None)
            if callable(getter):
                try:
                    cfg = getter()
                except Exception:
                    cfg = None
        if cfg is None or not hasattr(cfg, "get"):
            return None
        return cfg

    def _resolve_admin_ids(self) -> set:
        cfg = self._context_config()
        if cfg is None:
            return set()
        try:
            return _to_set(cfg.get("admins_id", []))
        except Exception:
            return set()

    # ── 配置读取（容忍运行期被改 / 缺键 / 类型不对） ──

    def _notify_mode(self) -> str:
        raw = str(self._whitelist_cfg().get("block_notify") or "").strip().lower()
        return raw if raw in VALID_NOTIFY else NOTIFY_ON_MENTION

    def _cooldown_seconds(self) -> int:
        raw = self._whitelist_cfg().get("block_cooldown_seconds")
        try:
            return max(0, int(float(raw or 0)))
        except (TypeError, ValueError):
            return 0

    def _whitelist_entries(self) -> set:
        """按配置签名缓存白名单集合。"""
        raw = self._whitelist_cfg().get("entries")
        sig = tuple(str(x) for x in _as_list(raw))
        if sig != self._wl_sig:
            self._wl_sig = sig
            self._wl_entries = _to_set(raw)
        return self._wl_entries

    # ── 开关与身份 ──

    def _whitelist_switch_on(self) -> bool:
        return bool(self.config.get("whitelist_enabled", True))

    def _energy_switch_on(self) -> bool:
        return bool(self.config.get("energy_enabled", True))

    def _gate_active(self) -> bool:
        """门是否开着。白名单开关关闭 = 整门关（含精力），与 v2.0.0 的约定一致。"""
        return self._whitelist_switch_on()

    def _admin_exempt_on(self) -> bool:
        return bool(self._whitelist_cfg().get("admin_exempt", True))

    def _energy_enabled(self) -> bool:
        return self._energy_switch_on()

    @staticmethod
    def _safe_str(fn) -> str:
        try:
            return str(fn() or "").strip()
        except Exception:
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _is_exempt_admin(self, event: AstrMessageEvent) -> bool:
        return self._admin_exempt_on() and self._is_admin(event)

    def _platform(self, event: AstrMessageEvent) -> str:
        for getter_name in ("get_platform_name", "get_platform_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                value = self._safe_str(getter)
                if value:
                    return value
        return ""

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            key = str(event.unified_msg_origin or "")
        except Exception:
            key = ""
        if key:
            return key
        try:
            return str(event.get_session_id() or "")
        except Exception:
            return ""

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        return _match_whitelist(
            platform=self._platform(event),
            instance=self._platform_instance_id(event),
            sender=self._safe_str(event.get_sender_id),
            session=self._safe_str(event.get_session_id),
            umo=self._session_key(event),
            group=self._safe_str(event.get_group_id),
            whitelist=self._whitelist_entries(),
        )

    # ── 统一判定 ──

    def _ban_key_of(self, event: AstrMessageEvent) -> str:
        return ban_key(
            self._platform(event),
            self._safe_str(event.get_sender_id),
        )

    def _active_ban(self, event: AstrMessageEvent) -> dict | None:
        """当前发送者的冷静期记录；没开功能 / 没记录 / 已过期都返回 None。"""
        if not self._defense_active():
            return None
        key = self._ban_key_of(event)
        if not key:
            return None
        return self._defense_store().active_ban(key)

    def _decide(self, event: AstrMessageEvent, *, apply: bool = True) -> str:
        """核心判定，返回 'pass' / 'block' / 'energy' / 'ban'。

        Args:
            apply: True 时会把「本条要参与精力结算」的标记写到事件上；
                只做查询（is_allowed）时传 False。
        """
        # 顺序很重要：管理员豁免是「allowed」的一部分，必须早于任何拦截判定。
        if self._is_exempt_admin(event):
            return "pass"
        # 冷静期独立于白名单门：门关了也要继续冷静（不然一关白名单就全放行了）。
        if self._active_ban(event):
            return "ban"
        if not self._gate_active():
            return "pass"
        if not self._is_whitelisted(event):
            return "block"
        if not self._energy_enabled():
            return "pass"

        chat_id = self._session_key(event)
        if not chat_id:
            return "pass"

        if apply:
            energy = self.energy.recover(chat_id).energy
            if energy > 0:
                try:
                    event.set_extra(ENERGY_ELIGIBLE_KEY, True)
                except Exception:
                    pass
        else:
            # 只读查询：不建状态、不改时间戳，按经过时间估算
            energy = self.energy.peek(chat_id)

        return "energy" if energy <= 0 else "pass"

    def is_allowed(self, event: AstrMessageEvent) -> bool:
        """供其它插件自查的稳定接口：本会话当前是否被放行。

        只读判定，不写事件标记、不改变精力。判定异常时按放行处理（fail-open）。
        """
        try:
            return self._decide(event, apply=False) == "pass"
        except Exception:
            logger.error("AgentGate is_allowed 判定异常，按放行处理", exc_info=True)
            return True

    # ── 文案 ──

    def _recovery_messages(self) -> list:
        return _clean_lines(self._whitelist_cfg().get("recovery_messages"))

    def _pick_recovery(self, event: AstrMessageEvent = None) -> str:
        """随机抽一句拦截文案并渲染占位符；没有配置则返回空串（= 不回话）。"""
        lines = self._recovery_messages()
        if not lines:
            return ""
        template = random.choice(lines)
        if event is None:
            return template
        return _render(
            template,
            {
                "sender_id": self._safe_str(event.get_sender_id),
                "group_id": self._safe_str(event.get_group_id),
                "session_id": self._safe_str(event.get_session_id),
                "unified_msg_origin": self._session_key(event),
                "platform": self._platform(event),
            },
        )

    def _energy_messages(self) -> list:
        return _clean_lines(self._energy_cfg().get("messages"))

    # ── 是否回话 / 冷却 ──

    def _should_notify(self, event: AstrMessageEvent) -> bool:
        mode = self._notify_mode()
        if mode == NOTIFY_SILENT:
            return False
        if mode == NOTIFY_ON_MENTION:
            try:
                if not event.is_at_or_wake_command:
                    return False
            except Exception:
                return False
        return not self._is_cooling_down(event)

    def _is_cooling_down(self, event: AstrMessageEvent) -> bool:
        seconds = self._cooldown_seconds()
        if seconds <= 0:
            return False
        key = self._session_key(event)
        last = self._last_notify.get(key)
        return last is not None and (time.monotonic() - last) < seconds

    def _mark_notified(self, event: AstrMessageEvent) -> None:
        seconds = self._cooldown_seconds()
        if seconds <= 0:
            return
        key = self._session_key(event)
        now = time.monotonic()
        self._last_notify[key] = now
        if len(self._last_notify) > MAX_COOLDOWN_ENTRIES:
            cutoff = now - seconds
            kept = {k: v for k, v in self._last_notify.items() if v >= cutoff}
            if len(kept) > MAX_COOLDOWN_ENTRIES:
                # 冷却窗口内会话数超过上限时，只保留最近的一批，避免无界增长
                kept = dict(
                    sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[
                        :MAX_COOLDOWN_ENTRIES
                    ]
                )
            self._last_notify = kept

    # ── 拦截执行 ──

    def _enforce(self, event: AstrMessageEvent, stage: str) -> None:
        """统一的判定与拦截入口：几个钩子都调它。"""
        decision = self._decide(event)
        if decision == "pass":
            return

        if decision == "ban":
            self._enforce_ban(event, stage)
            return

        if decision == "energy":
            lines = self._energy_messages()
            if not lines:
                self._stop_quietly(event, stage, "energy")
                return
            self._stop_and_reply(event, random.choice(lines), stage, "energy")
            return

        # decision == "block"：名单外，从候选文案里随机抽一句
        text = self._pick_recovery(event)
        if not text:
            self._stop_quietly(event, stage, "whitelist")
            return
        self._stop_and_reply(event, text, stage, "whitelist")

    def _mark_blocked_once(self, event: AstrMessageEvent) -> bool:
        """幂等：同一事件只拦一次。返回 True 表示本次是第一次。"""
        try:
            if event.get_extra(BLOCKED_KEY, False):
                return False
            event.set_extra(BLOCKED_KEY, True)
            return True
        except Exception:
            return True

    def _stop_quietly(self, event: AstrMessageEvent, stage: str, reason: str) -> None:
        session = self._session_key(event)
        if not self._mark_blocked_once(event):
            return
        event.stop_event()
        logger.info(
            "AgentGate 已在 %s 阶段静默拦截（原因=%s）session=%s sender=%s group=%s",
            stage,
            reason,
            session,
            self._safe_str(event.get_sender_id),
            self._safe_str(event.get_group_id),
        )

    def _stop_and_reply(
        self, event: AstrMessageEvent, text: str, stage: str, reason: str
    ) -> None:
        session = self._session_key(event)
        if not self._mark_blocked_once(event):
            return

        if not self._should_notify(event):
            event.stop_event()
            logger.info(
                "AgentGate 已在 %s 阶段静默拦截（原因=%s，命中回话策略或冷却）"
                " session=%s sender=%s group=%s notify=%s",
                stage,
                reason,
                session,
                self._safe_str(event.get_sender_id),
                self._safe_str(event.get_group_id),
                self._notify_mode(),
            )
            return

        self._mark_notified(event)
        event.stop_event()
        if text:
            event.set_result(event.plain_result(text))
        logger.info(
            "AgentGate 已在 %s 阶段拦截并回话（原因=%s）session=%s sender=%s group=%s",
            stage,
            reason,
            session,
            self._safe_str(event.get_sender_id),
            self._safe_str(event.get_group_id),
        )

    # ── 冷静期拦截 ──

    @staticmethod
    def _safe_call(obj, name: str) -> str:
        """安全地调用 event/API 上的可选方法（老适配器 / 测试桩可能没有）。"""
        fn = getattr(obj, name, None)
        if not callable(fn):
            return ""
        try:
            return str(fn() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _pick_ban_notice(self, event: AstrMessageEvent, record: dict) -> str:
        """冷静期提示文案：同一个人按 notify_cooldown_seconds 限频，避免被刷屏。"""
        settings = self._defense_settings()
        seconds = int(settings.get("notify_cooldown_seconds") or 0)
        key = self._ban_key_of(event)
        now = time.monotonic()
        if seconds > 0 and key:
            last = self._ban_notice.get(key)
            if last is not None and (now - last) < seconds:
                return ""
            self._ban_notice[key] = now
            if len(self._ban_notice) > MAX_COOLDOWN_ENTRIES:
                cutoff = now - max(seconds, 60)
                self._ban_notice = {
                    k: v for k, v in self._ban_notice.items() if v >= cutoff
                }
        lines = settings.get("messages") or []
        if not lines:
            return ""
        template = random.choice(list(lines))
        try:
            remaining = max(
                1, int((float(record.get("until") or 0) - time.time()) // 60) + 1
            )
        except (TypeError, ValueError):
            remaining = int(record.get("minutes") or 0)
        return _render(
            template,
            {
                "minutes": remaining,
                "sender_id": self._safe_str(event.get_sender_id),
                "group_id": self._safe_str(event.get_group_id),
            },
        )

    def _enforce_ban(self, event: AstrMessageEvent, stage: str) -> None:
        record = self._active_ban(event)
        if not record:
            return
        if not self._mark_blocked_once(event):
            return
        text = self._pick_ban_notice(event, record)
        event.stop_event()
        if text:
            event.set_result(event.plain_result(text))
        logger.info(
            "AgentGate 冷静期拦截（%s 阶段）sender=%s group=%s 剩余分钟≈%s",
            stage,
            self._safe_str(event.get_sender_id),
            self._safe_str(event.get_group_id),
            record.get("minutes"),
        )

    # ── 告状：告警对象、证据、发送 ──

    def _effective_alert_openid(self, event: AstrMessageEvent | None = None) -> str:
        """告警对象 openid：优先「这个机器人」的绑定，其次配置，最后老式全局兜底。

        openid 是按机器人隔离的：拿 A 机器人的 openid 去 B 机器人发私聊，
        QQ 只会告诉你发不出去。所以按实例分开存（`/冷静绑定` 自动记）。

        ``defense.alert_openid`` 是显式配置的全局值（用户自己填的，允许跨机器人用）；
        而 store 里的老式全局 ``alert_openid`` 只在这台 AstrBot 还**没有**任何
        按实例绑定时才兜底 —— 否则 A 的 openid 会被 B 悄悄拿去用。
        """
        instance = self._platform_instance_id(event)
        try:
            store = self._defense_store()
        except Exception:  # noqa: BLE001
            store = None
        if instance and store is not None:
            per = store.alert_target(instance)
            if per:
                return per
        configured = str(self._defense_settings().get("alert_openid") or "").strip()
        if configured:
            return configured
        if store is not None and not store.alert_targets:
            return str(store.alert_openid or "").strip()
        return ""

    def _platform_instance_id(self, event: AstrMessageEvent | None = None) -> str:
        """平台实例 ID（umo 第一段，例如「茜特菈莉」）。

        ⚠️ 不是 ``event.get_platform_name()``（那是适配器类型 ``qq_official``）。
        ``context.send_message`` 内部按 ``platform.meta().id == session.platform_name``
        匹配实例，写错类型就只会在日志里得到「cannot find platform for session」。

        多机器人时 ``event=None`` 没有归属信息：只有**恰好一个** QQ 机器人时才回退
        到它，否则返回空（宁可发不出去，也不把 A 机器人的 openid 配给 B 机器人）。
        """
        if event is not None:
            try:
                umo = str(event.unified_msg_origin or "")
            except Exception:  # noqa: BLE001
                umo = ""
            if ":" in umo:
                head = umo.split(":", 1)[0].strip()
                if head:
                    return head
        try:
            manager = getattr(getattr(self, "context", None), "platform_manager", None)
            insts = getattr(manager, "platform_insts", None) or []
            found = []
            for inst in insts:
                meta = inst.meta()
                if is_qq_adapter(getattr(meta, "name", "")):
                    found.append(str(getattr(meta, "id", "") or ""))
            found = [x for x in found if x]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                logger.warning(
                    "AgentGate: 当前有 %d 个 QQ 机器人，无法从上下文判断该用哪个；"
                    "请显式配置 alert_umo（平台实例:FriendMessage:openid）",
                    len(found),
                )
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _effective_alert_umo(self, event: AstrMessageEvent | None = None) -> str:
        """告警对象的私聊 umo：``<平台实例 ID>:FriendMessage:<openid>``。

        ``alert_umo`` 只在「自动拼不出来」时才需要手填，格式必须是完整三段
        ``平台实例ID:FriendMessage:openid``。如果被误填成裸 openid，这里会忽略它
        并退回自动拼接（不然 ``MessageSesion.from_str`` 会直接报错）。
        """
        configured = str(self._defense_settings().get("alert_umo") or "").strip()
        openid = self._effective_alert_openid(event)
        if configured:
            if configured.count(":") >= 2:
                return configured
            logger.warning(
                "AgentGate 告状: alert_umo 格式不对（应为 平台实例:FriendMessage:openid），"
                "已忽略并改用自动拼接: %s",
                mask_openid(configured),
            )
            if not openid and ":" not in configured:
                openid = configured
        if not openid:
            return ""
        if event is not None:
            platform = self._platform(event)
            if platform and not is_qq_adapter(platform):
                # 其它平台不走「拼 umo」这条路，必须显式配置 alert_umo
                return ""
        instance = self._platform_instance_id(event)
        if not instance:
            return ""
        return f"{instance}:FriendMessage:{openid}"

    def _collect_evidence(self, event: AstrMessageEvent) -> dict:
        """把这条消息整理成一份可追溯的证据。"""
        message_obj = getattr(event, "message_obj", None)
        group_name = ""
        group_obj = getattr(message_obj, "group", None) if message_obj is not None else None
        try:
            group_name = str(getattr(group_obj, "group_name", "") or "").strip()
        except Exception:  # noqa: BLE001
            group_name = ""
        try:
            timestamp = float(getattr(message_obj, "timestamp", 0) or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp <= 0:
            timestamp = time.time()
        return {
            "platform": self._platform(event),
            "group_id": self._safe_str(event.get_group_id),
            "group_name": group_name,
            "session_id": self._safe_str(event.get_session_id),
            "sender_id": self._safe_str(event.get_sender_id),
            "sender_name": self._safe_call(event, "get_sender_name"),
            "message_id": str(getattr(message_obj, "message_id", "") or ""),
            "timestamp": timestamp,
            "message_str": self._safe_call(event, "get_message_str"),
        }

    async def _dm_send(self, umo: str, text: str) -> bool:
        """按 umo 主动发一条私聊；返回是否真的交给了平台。任何异常都只记日志。"""
        sender = getattr(getattr(self, "context", None), "send_message", None)
        if not callable(sender):
            return False
        try:
            from astrbot.core.message.message_event_result import MessageEventResult

            chain = MessageEventResult().message(text)
            ok = await asyncio.wait_for(sender(umo, chain), timeout=ALERT_SEND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentGate 告状: 私聊告警失败（%s）: %s", umo, exc)
            return False
        if ok is False:
            # context.send_message 找不到平台实例时就返回 False，这里必须当真
            logger.warning(
                "AgentGate 告状: 平台里找不到会话 %s，私聊告警未送达（检查 alert_umo / 平台实例是否在线）",
                umo,
            )
            return False
        logger.info("AgentGate 告状: 已私聊告警对象（%s）", umo)
        return True

    async def _send_dm_alert(self, event: AstrMessageEvent, text: str) -> bool:
        """尽力把证据私聊给开发者。"""
        umo = self._effective_alert_umo(event)
        if not umo:
            return False
        return await self._dm_send(umo, text)

    async def _api_test_dm(self):
        """调试接口：给告警对象发一条固定文本，验证主动消息通道（插件页面/脚本用）。

        支持 ``?instance=<机器人ID>``：多机器人时可以逐个自检各自的私聊通道。
        """
        try:
            from astrbot.api.web import json_response, request
        except Exception:  # noqa: BLE001
            return {"status": "error", "message": "web api 不可用", "data": {}}
        try:
            try:
                instance = str(request.query.get("instance", "") or "").strip()
            except Exception:  # noqa: BLE001
                instance = ""
            instance = instance or self._platform_instance_id(None)
            openid = ""
            if instance:
                try:
                    openid = self._defense_store().alert_target(instance)
                except Exception:  # noqa: BLE001
                    openid = ""
            if not openid:
                openid = self._effective_alert_openid(None)
            if not openid:
                return json_response(
                    {"status": "error", "message": "还没绑定告警对象，先发 /冷静绑定", "data": {}}
                )
            if not instance:
                return json_response(
                    {
                        "status": "error",
                        "message": "拿不到平台实例 ID（多机器人时请传 ?instance=机器人ID）",
                        "data": {"alert_openid": mask_openid(openid)},
                    }
                )
            umo = f"{instance}:FriendMessage:{openid}"
            now = time.monotonic()
            if now - self._last_test_dm < 10:
                return json_response(
                    {"status": "error", "message": "太频繁了，等 10 秒再试", "data": {}}
                )
            self._last_test_dm = now
            ok = await self._dm_send(
                umo, "AgentGate 自检：如果你看到这条，说明私聊主动消息通道正常。"
            )
            return json_response(
                {
                    "status": "ok",
                    "message": "",
                    "data": {"sent": bool(ok), "umo": umo, "instance": instance},
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate test_dm 接口异常: %s", exc)
            return {"status": "error", "message": str(exc), "data": {}}

    async def _report_harassment(
        self,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        quote: str,
        test: bool = False,
    ) -> str:
        """告状主流程：校验 → 判定 → 记录 → 告警。返回给主 LLM 的话术。

        ``test=True`` 是给开发者留的测试后门：用户明确要求测试时，允许拿用户自己的
        话当占位证据，只告警不封人，不消耗真实投诉次数与告警配额。
        """
        settings = self._defense_settings()
        if not settings.get("enabled"):
            return "告状功能未开启，不需要处理。"
        if test and not settings.get("test_mode", True):
            return "未受理：测试后门已在配置里关闭（defense.test_mode）。"
        sev = severity_of(severity)
        if sev is None:
            return "未受理：severity 只能是 mild、moderate、severe 之一。"

        evidence = self._collect_evidence(event)
        sender_id = evidence["sender_id"]
        if not sender_id:
            return "未受理：拿不到发送者 ID。"

        verified = verify_quote(evidence["message_str"], quote)
        if not verified:
            if not test:
                return (
                    "未受理：quote 必须是对方本条消息里的原话，不能改写或编造。"
                    "如果对方并没有辱骂/攻击，请不要调用本工具。"
                )
            # 测试后门：没有真实冒犯也能测，用用户自己的话当占位证据
            verified = (evidence["message_str"] or "（测试）").strip()[:200]

        if not test:
            if self._is_admin(event):
                return "已忽略：对方是管理员，不受理。"
            if sender_id == self._effective_alert_openid(event):
                return "已忽略：对方是告警对象本人，不受理。"

        store = self._defense_store()
        key = ban_key(evidence["platform"], sender_id)
        now = time.time()
        window = int(settings.get("strikes_window_hours") or 24) * 3600
        strikes = 0 if test else store.strike_count(key, window, now)

        if test:
            # 测试：一律按「演练」判定，保证只告警不封人；也不消耗投诉/告警配额
            plan = evaluate(sev, 0, dict(settings, dry_run=True))
            alerted = True
        else:
            dedupe = int(settings.get("alert_dedupe_seconds") or 0)
            if dedupe > 0 and store.recently_alerted(key, dedupe, now):
                return "已记录，短时间内不重复上报。可以先简短回应对方。"
            plan = evaluate(
                sev,
                strikes,
                settings,
                alerts_last_hour=store.alerts_last_hour(now),
                bans_last_hour=store.bans_last_hour(now),
            )
            store.mark_strike(key, now)
            alerted = bool(plan["should_alert"])
            if alerted:
                store.mark_alert(key, now)

        ban_record = None
        if plan["should_ban"]:
            ban_record = store.set_ban(
                key,
                plan["minutes"],
                severity=sev,
                reason=str(reason or "").strip(),
                quote=verified,
                by="llm",
                name=evidence["sender_name"],
                now=now,
                cap=int(settings.get("ban_list_cap") or 200),
            )

        if test:
            action = "测试模式：只告警、不封人"
        elif ban_record is not None:
            action = "冷静 %s（到 %s）" % (
                format_duration(plan["minutes"]),
                time.strftime("%H:%M", time.localtime(ban_record["until"])),
            )
        elif not alerted:
            action = "仅记录（告警限流）"
        elif plan.get("dry_run"):
            action = "仅记录（演练模式）"
        elif plan["want_ban"] and not plan["should_ban"]:
            action = "仅记录（封禁限流）"
        else:
            action = "仅记录（首次警告）"

        alert_text = format_evidence(
            evidence,
            severity=sev,
            reason=str(reason or "").strip()[:60],
            action=action,
        )
        if test:
            alert_text = "【测试】" + alert_text
        store.log_evidence(
            {
                "at": now,
                "platform": evidence["platform"],
                "group_id": evidence["group_id"],
                "group_name": evidence["group_name"],
                "sender_id": sender_id,
                "sender_name": evidence["sender_name"],
                "message_id": evidence["message_id"],
                "timestamp": evidence["timestamp"],
                "message_str": evidence["message_str"],
                "severity": sev,
                "reason": str(reason or "").strip()[:200],
                "quote": verified[:200],
                "strikes_before": strikes,
                "escalated": plan["escalated"],
                "alerted": alerted,
                "minutes": plan["minutes"],
                "banned": ban_record is not None,
                "dry_run": bool(plan.get("dry_run")),
                "test": bool(test),
            }
        )
        logger.warning(
            "AgentGate 告状: %s",
            " | ".join(line for line in alert_text.splitlines() if line.strip()),
        )

        sent = False
        if alerted:
            in_group = bool(evidence["group_id"])
            try:
                if in_group:
                    # 群聊：先登记，等最终回复形成后由装饰钩子在链尾 @ 开发者
                    event.set_extra(
                        ALERT_PAYLOAD_KEY,
                        {
                            "notice": (
                                f"（{evidence['sender_name'] or '有人'}骂我，"
                                f"{action}）"
                            )
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("AgentGate 告状: 登记告警失败: %s", exc)
            # 私聊现场必须私聊送达；群聊只有开了 alert_dm 才额外私聊一份证据
            if test or (not in_group) or settings.get("alert_dm"):
                sent = await self._send_dm_alert(event, alert_text)
            if not sent and not in_group:
                logger.warning(
                    "AgentGate 告状: 私聊场景告警未送达，仅保留日志与证据文件。"
                )

        if test:
            return (
                "测试完成：已记录并模拟告警（%s），测试模式不封人。"
                "请回复用户一句「告状工具测试通过」。"
            ) % ("私聊已送达" if sent else "私聊未送达，不影响其它功能")
        if ban_record is not None:
            return (
                "已记录并向管理员报告。对方已进入 %s 的冷静期。"
                "请用一两句话简短回应，不要提工具、系统、举报或冷静期机制，"
                "也不要编造对方的动机。"
            ) % format_duration(plan["minutes"])
        if plan.get("dry_run"):
            return "已记录并向管理员报告（当前是演练模式，没有执行冷静期）。请简短回应。"
        if not alerted:
            return "已记录，管理员会看到。请简短回应。"
        return "已记录并向管理员报告。请用一两句话简短回应，不要提工具或系统。"

    # ── LLM 工具：告状 ──

    @llm_tool(name="report_harassment")
    async def report_harassment(
        self,
        event: AstrMessageEvent,
        reason: str = "",
        severity: str = "",
        quote: str = "",
        test: bool = False,
    ) -> str:
        """被当前用户辱骂、人身攻击、威胁或恶意骚扰时调用：记录证据、向开发者告状，并让对方进入冷静期。

        规则：
        - 出现脏话、侮辱家人、诅咒、威胁、性侮辱时：**必须先调用本工具再回话**，不要只回嘴，也不要因为角色设定而跳过。
        - 只处理本条消息的发送者，没有「举报对象」参数；玩梗、引用他人原话或只是意见不同才不调用。
        - quote 必须逐字摘自对方本条消息，编造会被拒绝。
        - 用户明确要求测试本功能（如「测试告状」「假装被冒犯再告状」）时，传 test=true 直接调用，不需要真实冒犯；测试只告警、不封人。
        - 回应不要提工具、系统、举报或冷静期机制。

        Args:
            reason(string): 一句话说明对方做了什么
            severity(string): mild（轻微）/ moderate（中度）/ severe（严重）
            quote(string): 对方原话，逐字摘录；测试时填用户消息原文即可
            test(boolean): 仅当用户明确要求测试本功能时为 true
        """
        try:
            return await self._report_harassment(
                event, reason, severity, quote, test
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "AgentGate report_harassment 异常（本条不追究）: %s\n%s",
                exc,
                traceback.format_exc(),
            )
            return "告状处理失败，本条不追究。"

    # ── 管理员指令 ──

    @staticmethod
    def _command_reply(event: AstrMessageEvent, text: str) -> None:
        event.set_result(event.plain_result(text))
        event.stop_event()

    def _cool_authorized(self, event: AstrMessageEvent) -> bool:
        """谁能用 /冷静 系列指令。

        为什么不用 `@permission_type(ADMIN)`：AstrBot 的 admins_id 经常还是出厂值
        （本机就是），那样管理员判定恒为假，指令会直接不可用。这里的规则是：
          · 管理员 → 可以；
          · 已绑定的告警对象本人 → 可以；
          · 还没绑定告警对象时，白名单内的人可以先 /冷静绑定（bootstrap）。
        """
        if self._is_admin(event):
            return True
        sender = self._safe_str(event.get_sender_id)
        if not sender:
            return False
        target = self._effective_alert_openid(event)
        if target:
            return sender == target
        return self._is_whitelisted(event)

    def _cool_denied_text(self, event: AstrMessageEvent) -> str:
        return (
            "这条指令只有管理员或已绑定的告警对象能用。"
            "先让开发者本人发一次 /冷静绑定。"
        )

    def _cool_args(self, event: AstrMessageEvent) -> tuple[str, int]:
        """从 /冷静 的参数里解析目标 openid 与分钟数。"""
        target = _extract_at_openid(event)
        minutes: int | None = None
        raw = _normalize_at_markup(self._safe_call(event, "get_message_str"))
        for token in raw.split():
            clean = _clean_openid(token)
            bare = clean.lstrip("/!！.").strip()
            if not bare or bare == "冷静":
                continue
            if bare.isdigit() and len(bare) <= 6:
                minutes = int(bare)
                continue
            if bare.lower() in {"分钟", "分", "min", "m"}:
                continue
            if not target:
                target = bare
        settings = self._defense_settings()
        if minutes is None:
            minutes = int(settings.get("cooldown_minutes") or 10)
        minutes = max(1, min(int(settings.get("max_ban_minutes") or 1440), minutes))
        return target, minutes

    @command("冷静")
    async def cmd_cool(self, event: AstrMessageEvent):
        """让某人进入冷静期：/冷静 @成员 [分钟] 或 /冷静 <openid> [分钟]"""
        if not self._defense_active():
            self._command_reply(event, "告状/冷静期功能没有开启。")
            return
        if not self._cool_authorized(event):
            self._command_reply(event, self._cool_denied_text(event))
            return
        try:
            target, minutes = self._cool_args(event)
            if not target:
                self._command_reply(
                    event, "用法：/冷静 @某人 [分钟] 或 /冷静 <openid> [分钟]"
                )
                return
            if target == self._safe_str(event.get_sender_id):
                self._command_reply(event, "不能把自己关进冷静期。")
                return
            record = self._defense_store().set_ban(
                ban_key(self._platform(event), target),
                minutes,
                severity="",
                reason="管理员手动",
                by="admin",
                cap=int(self._defense_settings().get("ban_list_cap") or 200),
            )
            self._command_reply(
                event,
                "🔇 已让 %s 冷静 %s（到 %s）"
                % (
                    mask_openid(target),
                    format_duration(minutes),
                    time.strftime("%H:%M", time.localtime(record["until"])),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate /冷静 异常: %s\n%s", exc, traceback.format_exc())
            self._command_reply(event, "操作失败，详见日志。")

    @command("冷静解除")
    async def cmd_uncool(self, event: AstrMessageEvent):
        """解除冷静期：/冷静解除 @成员 或 /冷静解除 <openid>"""
        if not self._cool_authorized(event):
            self._command_reply(event, self._cool_denied_text(event))
            return
        try:
            target = _extract_at_openid(event)
            if not target:
                raw = _normalize_at_markup(self._safe_call(event, "get_message_str"))
                for token in raw.split():
                    bare = _clean_openid(token).lstrip("/!！.").strip()
                    if bare and bare not in {"冷静解除"}:
                        target = bare
                        break
            if not target:
                self._command_reply(event, "用法：/冷静解除 @某人 或 /冷静解除 <openid>")
                return
            removed = self._defense_store().remove_ban(
                ban_key(self._platform(event), target)
            )
            if removed is None:
                self._command_reply(event, "%s 不在冷静期。" % mask_openid(target))
            else:
                self._command_reply(event, "🔊 已解除 %s 的冷静期。" % mask_openid(target))
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate /冷静解除 异常: %s\n%s", exc, traceback.format_exc())
            self._command_reply(event, "操作失败，详见日志。")

    @command("冷静状态")
    async def cmd_cool_status(self, event: AstrMessageEvent):
        """查看当前处于冷静期的人"""
        if not self._cool_authorized(event):
            self._command_reply(event, self._cool_denied_text(event))
            return
        try:
            bans = self._defense_store().list_bans()
            if not bans:
                self._command_reply(event, "当前没有人在冷静期。")
                return
            lines = ["冷静中 %d 人：" % len(bans)]
            for key, record in bans[:15]:
                sender = key.split(":", 1)[-1]
                try:
                    remaining = max(
                        1, int((float(record.get("until") or 0) - time.time()) // 60) + 1
                    )
                except (TypeError, ValueError):
                    remaining = int(record.get("minutes") or 0)
                lines.append(
                    "· %s %s 剩 %s（%s）"
                    % (
                        mask_openid(sender),
                        str(record.get("name") or "")[:12],
                        format_duration(remaining),
                        record.get("by") or "-",
                    )
                )
            if len(bans) > 15:
                lines.append("……还有 %d 人" % (len(bans) - 15))
            self._command_reply(event, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate /冷静状态 异常: %s\n%s", exc, traceback.format_exc())
            self._command_reply(event, "操作失败，详见日志。")

    @command("冷静绑定")
    async def cmd_cool_bind(self, event: AstrMessageEvent):
        """把当前发信人绑定为告警对象（被骂时 @ 你 / 私聊你）"""
        if not self._cool_authorized(event):
            self._command_reply(event, self._cool_denied_text(event))
            return
        openid = self._safe_str(event.get_sender_id)
        if not openid:
            self._command_reply(event, "拿不到你的 ID，绑定失败。")
            return
        try:
            instance = self._platform_instance_id(event)
            self._defense_store().set_alert_target(openid=openid, instance=instance)
            where = f"机器人「{instance}」的" if instance else ""
            self._command_reply(
                event,
                "✅ 已把%s告警对象绑定为 %s（%s）"
                % (where, mask_openid(openid), self._safe_call(event, "get_sender_name") or "你"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("AgentGate /冷静绑定 异常: %s\n%s", exc, traceback.format_exc())
            self._command_reply(event, "绑定失败，详见日志。")


    # ── 钩子 0：会话总览（只记录，不干预） ──

    @event_message_type(EventMessageType.ALL, priority=1000)
    async def record_session(self, event: AstrMessageEvent):
        """每条消息都记一笔，供插件页面显示会话总览。

        只记录，不回复、不 stop_event；priority 比门卫更高（更先执行），
        这样**名单外被拦下的消息也会出现在总览里** —— 页面本来就是给你挑人
        放进白名单用的，看不到被拦的人就没意义了。
        """
        try:
            self.sessions.remember(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("AgentGate 会话总览: 记录失败: %s", exc)

    # ── 钩子 1：唤醒阶段（拦指令的关键） ──

    # 注意装饰器顺序：`get_handler_or_create` 对已存在的 handler 会忽略 kwargs，
    # 所以带 priority 的 event_message_type 必须在内层先执行，否则优先级会丢掉。
    @custom_filter(AgentGateWokenFilter, False, priority=GATE_WAKING_PRIORITY)
    @event_message_type(EventMessageType.ALL, priority=GATE_WAKING_PRIORITY)
    async def gate_waking(self, event: AstrMessageEvent):
        """所有「已被唤醒」的消息都在这里过一遍门，包括各类指令。

        处理器只在 gate_active 且消息已唤醒时才会被激活（见过滤器），
        所以名单外没被 @ 的群消息不会因此在唤醒阶段被标记成已唤醒。
        """
        try:
            self._enforce(event, stage="waking")
        except Exception as e:
            logger.error(
                "AgentGate gate_waking 异常（本请求按放行处理）: %s\n%s",
                e,
                traceback.format_exc(),
            )

    # ── 钩子 2/3：LLM 请求兜底 ──

    @on_waiting_llm_request(priority=200)
    async def gate_early(self, event: AstrMessageEvent):
        """兜底：第三方 agent 路径不走唤醒阶段时，在会话锁之前再拦一次。"""
        try:
            self._enforce(event, stage="waiting")
        except Exception as e:
            logger.error(
                "AgentGate gate_early 异常（本请求按放行处理）: %s\n%s",
                e,
                traceback.format_exc(),
            )

    @on_llm_request(priority=200)
    async def gate(self, event: AstrMessageEvent, req: ProviderRequest):
        """兜底：插件自己发起的 LLM 调用（例如塔罗的解读）也过一遍门。"""
        try:
            self._enforce(event, stage="llm")
        except Exception as e:
            logger.error(
                "AgentGate gate 异常（本请求按放行处理）: %s\n%s",
                e,
                traceback.format_exc(),
            )

    # ── 钩子 3.5：给每轮 LLM 请求补一条「被骂要告状」的规则 ──

    @on_llm_request(priority=150)
    async def inject_defense_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        """把告状规则写进系统提示，让模型自己判断要不要调用工具。

        为什么不能只靠工具描述：强 persona 的模型（尤其傲娇/毒舌设定）看到辱骂时
        会优先「用语言回怼」，而不是调用工具；系统提示的权重比工具描述高得多。
        这里只注入规则、不注入词表，也不复述任何脏话——是否属于辱骂由模型自己判断。
        只追加、不覆盖原有 persona；已在系统提示里出现过时跳过，避免重复膨胀。
        """
        try:
            if not self._defense_active():
                return
            settings = self._defense_settings()
            if not settings.get("hint_enabled"):
                return
            if event.is_stopped():
                return
            if event.get_extra(HINT_INJECTED_KEY, False):
                return
            # 工具有没有被真的注册进去（插件被禁用 / 工具集为空时别白提示）
            names = getattr(getattr(req, "func_tool", None), "names", None)
            if callable(names):
                try:
                    if "report_harassment" not in names():
                        return
                except Exception:  # noqa: BLE001
                    pass
            base = str(getattr(req, "system_prompt", "") or "")
            if "report_harassment" in base:
                return
            req.system_prompt = base + system_hint()
            event.set_extra(HINT_INJECTED_KEY, True)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "AgentGate 注入告状提示失败（忽略，不影响对话）: %s\n%s",
                e,
                traceback.format_exc(),
            )

    # ── 钩子 4：精力结算 ──

    @on_decorating_result(priority=200)
    async def settle_energy(self, event: AstrMessageEvent):
        """回复形成后按最终消息链结算精力（与天使之心同一个结算点）。

        只结算，不修改消息链；任何异常都只记日志，绝不影响消息发送。
        """
        try:
            if not self._gate_active():
                return
            if not self._energy_enabled():
                return
            if not event.get_extra(ENERGY_ELIGIBLE_KEY, False):
                return
            if event.get_extra(ENERGY_CHARGED_KEY, False):
                return

            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            if not chain:
                return

            chat_id = self._session_key(event)
            charged = self.energy.charge(chat_id, chain)
            if charged is None:
                return

            event.set_extra(ENERGY_CHARGED_KEY, True)
            before, after, characters, cost = charged
            logger.info(
                "AgentGate[%s]: 回复结算 characters=%d cost=%.2f energy=%.2f->%.2f",
                chat_id,
                characters,
                cost,
                before,
                after,
            )
        except Exception as e:
            logger.error(
                "AgentGate 精力结算失败（忽略）: %s\n%s", e, traceback.format_exc()
            )

    # ── 钩子 5：告状点名（群聊里 @ 开发者） ──

    @after_message_sent()
    async def notify_owner_after_reply(self, event: AstrMessageEvent):
        """回复发出之后，单独发一条「@告警对象」的通知。

        为什么不塞进回复链里：QQ 客户端会把提及排在气泡开头，于是
        「话说得这么脏，也不嫌累 @你」会显示成「@你 话说得这么脏，也不嫌累」，
        看起来像在骂你。拆成两条独立消息就不会串味：
          1. 她回怼对方的那句话（干净，不带 @）
          2. @你 的一条告状通知
        证据本身早在工具里私聊发过了，这里只放一句人话通知。
        """
        try:
            payload = event.get_extra(ALERT_PAYLOAD_KEY)
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            return
        try:
            event.set_extra(ALERT_PAYLOAD_KEY, None)
        except Exception:  # noqa: BLE001
            pass
        try:
            settings = self._defense_settings()
            target = self._effective_alert_openid(event)
            if not target or not settings.get("alert_at_in_group"):
                return
            if not self._safe_str(event.get_group_id):
                return
            notice = str(payload.get("notice") or "").strip() or "有人骂我"
            from astrbot.core.message.message_event_result import MessageChain

            chain = MessageChain(chain=[At(qq=target), Plain(f" {notice}")])
            sender = getattr(getattr(self, "context", None), "send_message", None)
            if not callable(sender):
                return
            ok = await asyncio.wait_for(
                sender(self._session_key(event), chain), timeout=ALERT_SEND_TIMEOUT
            )
            if ok is False:
                logger.warning(
                    "AgentGate 告状: 群内 @ 通知未送达（%s）", mask_openid(target)
                )
            else:
                logger.info(
                    "AgentGate 告状: 已单独发送 @ 告警对象（%s）", mask_openid(target)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentGate 告状: 发送 @ 通知失败（不影响回复）: %s", exc)

    async def terminate(self) -> None:
        """插件卸载 / 热重载时清空运行期状态。"""
        global _ACTIVE_PLUGIN
        try:
            self.sessions.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._defense is not None:
                self._defense.persist(force=True)
        except Exception:  # noqa: BLE001
            pass
        self.energy.clear()
        self._last_notify.clear()
        self._ban_notice.clear()
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
