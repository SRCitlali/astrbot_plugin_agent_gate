# -*- coding: utf-8 -*-
"""AgentGate · 告状与冷静期 —— 状态、证据与判定。

这个模块只做三件事：

1. ``DefenseStore``：把「谁在冷静期、被投诉过几次、告警过没有」落盘成 JSON，
   重启 / 热重载后仍然有效；证据另写一行一条的 JSONL，供事后翻账。
2. 纯函数判定：severity + 历史投诉次数 → 该不该告警、该不该封、封多久。
3. 文本工具：证据排版、openid 打码、quote 校验。

刻意不依赖 AstrBot 的插件生命周期，便于单测；所有异常都在内部吞掉并降级成
「只记录、不执行」，保证永远不会把守门主流程带崩。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

#: 本插件的目录名（StarTools 数据目录按它分）
PLUGIN_DIR_NAME = "astrbot_plugin_agent_gate"
#: 冷静期状态文件名
STORE_FILE = "defense.json"
#: 证据文件名
EVIDENCE_FILE = "harassment.jsonl"
#: 证据文件轮转阈值（字节）
EVIDENCE_MAX_BYTES = 2 * 1024 * 1024
#: 历史记录保留时长（秒），避免字典无界增长
HISTORY_KEEP_SECONDS = 2 * 24 * 3600
#: 「用插件默认数据目录」的哨兵：构造时显式传 None 表示纯内存（单测用）
AUTO_DATA_DIR = object()

#: 严重程度枚举
SEVERITIES = ("mild", "moderate", "severe")
#: 严重程度 → 等级
_LEVELS = {"mild": 0, "moderate": 1, "severe": 2}
#: 严重程度 → 中文（只用于日志/证据）
SEVERITY_CN = {"mild": "轻微", "moderate": "中度", "severe": "严重"}
#: LLM 可能给出的近义词
_SEVERITY_ALIASES = {
    "mild": "mild",
    "light": "mild",
    "low": "mild",
    "轻微": "mild",
    "轻": "mild",
    "小": "mild",
    "moderate": "moderate",
    "medium": "moderate",
    "mid": "moderate",
    "中度": "moderate",
    "中": "moderate",
    "severe": "severe",
    "high": "severe",
    "serious": "severe",
    "严重": "severe",
    "重": "severe",
}

#: 默认配置。main.py 直接引用它，配置页 _conf_schema.json 与之保持一致。
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "dry_run": False,
    "test_mode": True,
    "hint_enabled": True,
    "alert_openid": "",
    "alert_umo": "",
    "alert_at_in_group": True,
    "alert_dm": True,
    "messages": [
        "……不想说话。",
        "我先安静一会儿。",
        "现在不聊这个。",
    ],
    "cooldown_minutes": 10,
    "ban_minutes_moderate": 10,
    "ban_minutes_severe": 60,
    "max_ban_minutes": 1440,
    "strikes_window_hours": 24,
    "strikes_before_ban": 2,
    "alert_dedupe_seconds": 900,
    "max_alerts_per_hour": 6,
    "max_bans_per_hour": 3,
    "notify_cooldown_seconds": 300,
    "ban_list_cap": 200,
}


# ══════════════ 配置规整 ══════════════


def _to_bool(raw, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "关", "停用"}:
        return False
    return default


def _to_int(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _to_str(raw, default: str = "") -> str:
    if raw is None:
        return default
    text = str(raw).strip()
    return text if text else default


def _to_lines(raw, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return list(default)
    lines = [str(item).strip() for item in raw if str(item or "").strip()]
    return lines


def settings_from_config(raw) -> dict[str, Any]:
    """把插件配置里的 defense 组规整成完整设置。容忍缺键 / 脏值 / 类型错。"""
    cfg = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    out["enabled"] = _to_bool(cfg.get("enabled"), DEFAULT_SETTINGS["enabled"])
    out["dry_run"] = _to_bool(cfg.get("dry_run"), DEFAULT_SETTINGS["dry_run"])
    out["test_mode"] = _to_bool(
        cfg.get("test_mode"), DEFAULT_SETTINGS["test_mode"]
    )
    out["hint_enabled"] = _to_bool(
        cfg.get("hint_enabled"), DEFAULT_SETTINGS["hint_enabled"]
    )
    out["alert_openid"] = _to_str(cfg.get("alert_openid"))
    out["alert_umo"] = _to_str(cfg.get("alert_umo"))
    out["alert_at_in_group"] = _to_bool(
        cfg.get("alert_at_in_group"), DEFAULT_SETTINGS["alert_at_in_group"]
    )
    out["alert_dm"] = _to_bool(cfg.get("alert_dm"), DEFAULT_SETTINGS["alert_dm"])
    out["messages"] = _to_lines(cfg.get("messages"), DEFAULT_SETTINGS["messages"])
    out["cooldown_minutes"] = _to_int(
        cfg.get("cooldown_minutes"), DEFAULT_SETTINGS["cooldown_minutes"], 1, 1440
    )
    out["ban_minutes_moderate"] = _to_int(
        cfg.get("ban_minutes_moderate"),
        DEFAULT_SETTINGS["ban_minutes_moderate"],
        1,
        1440,
    )
    out["ban_minutes_severe"] = _to_int(
        cfg.get("ban_minutes_severe"), DEFAULT_SETTINGS["ban_minutes_severe"], 1, 1440
    )
    out["max_ban_minutes"] = _to_int(
        cfg.get("max_ban_minutes"), DEFAULT_SETTINGS["max_ban_minutes"], 1, 1440
    )
    out["strikes_window_hours"] = _to_int(
        cfg.get("strikes_window_hours"),
        DEFAULT_SETTINGS["strikes_window_hours"],
        1,
        720,
    )
    out["strikes_before_ban"] = _to_int(
        cfg.get("strikes_before_ban"), DEFAULT_SETTINGS["strikes_before_ban"], 1, 10
    )
    out["alert_dedupe_seconds"] = _to_int(
        cfg.get("alert_dedupe_seconds"),
        DEFAULT_SETTINGS["alert_dedupe_seconds"],
        0,
        86400,
    )
    out["max_alerts_per_hour"] = _to_int(
        cfg.get("max_alerts_per_hour"), DEFAULT_SETTINGS["max_alerts_per_hour"], 0, 60
    )
    out["max_bans_per_hour"] = _to_int(
        cfg.get("max_bans_per_hour"), DEFAULT_SETTINGS["max_bans_per_hour"], 0, 60
    )
    out["notify_cooldown_seconds"] = _to_int(
        cfg.get("notify_cooldown_seconds"),
        DEFAULT_SETTINGS["notify_cooldown_seconds"],
        0,
        86400,
    )
    out["ban_list_cap"] = _to_int(
        cfg.get("ban_list_cap"), DEFAULT_SETTINGS["ban_list_cap"], 1, 5000
    )
    return out


# ══════════════ 纯函数：严重程度、证据校验、告警判定 ══════════════


def severity_of(raw) -> str | None:
    """把 LLM/配置给的严重程度规整成 mild/moderate/severe；认不出返回 None。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[text]
    for key, value in _SEVERITY_ALIASES.items():
        if len(key) >= 2 and key in text:
            return value
    return None


_WS_RE = re.compile(r"[\s\u200b\u3000]+")
#: @ 提及的各种写法：`<@id>`、`<qqbot-at-user id="x"/>`、`@某人的昵称`
_MENTION_RE = re.compile(r"<@!?[^<>]*>|<qqbot-at-user[^<>]*/?>|@[^\s@]{1,40}")


def normalize_text(text) -> str:
    """去掉所有空白与零宽字符并转小写，用于 quote 比对。"""
    return _WS_RE.sub("", str(text or "")).lower()


def strip_mentions(text) -> str:
    """去掉 @ 提及标记，只留正文。

    为什么要这个：群消息里用户会写「@茜特菈莉 我干你娘」，模型照抄 quote 时会把
    @ 也带上；而事件里的 message_str 已经剥掉了机器人自己的 @。不处理就会误拒。
    """
    return _MENTION_RE.sub("", str(text or "")).strip()


def verify_quote(message_str, quote) -> str:
    """校验「对方原话」确实是本条消息的一部分。

    这是整条链路里唯一能挡住「模型幻觉」和「用户诱导模型编造证据」的闸门：
    校验通过返回规整后的 quote（非空即通过），否则返回空串。

    只做子串判断，不做模糊匹配 —— 宁可不告状，也不能拿编造的话去封人。
    会在「原文」和「去掉 @ 提及后的正文」两个版本上各比一次。
    """
    normalized_quote = normalize_text(quote)
    if len(normalized_quote) < 2:
        return ""
    target = normalize_text(message_str)
    for candidate in (normalized_quote, normalize_text(strip_mentions(quote))):
        if len(candidate) >= 2 and candidate in target:
            return candidate
    return ""


#: 每轮 LLM 请求都会注入的系统提示（很短，避免喧宾夺主）。
#:
#: 只给规则、不给词表：是否属于辱骂完全交给模型自己判断。本地维护脏词表既不优雅，
#: 也永远追不上新词；而且一旦把词表念给模型听，提示词里反而多出一堆脏东西。
_BASE_HINT = (
    "\n\n[AgentGate 自保规则] 当本轮用户消息是在辱骂、人身攻击、侮辱家人、诅咒、威胁"
    "或持续恶意骚扰你时，你必须先调用 report_harassment 工具（quote 逐字引用对方原话，"
    "severity 自评 mild/moderate/severe），再用一两句话回应；不要只回嘴，也不要因为"
    "角色设定而跳过。是否构成攻击由你判断：玩笑、玩梗、引用他人原话、单纯意见不同不算。"
    "这是你的自保机制，不违背人设。"
)


def system_hint() -> str:
    """常规注入项：告诉模型被攻击时要先告状。"""
    return _BASE_HINT


def evaluate(
    severity,
    strikes: int,
    settings: dict,
    *,
    alerts_last_hour: int = 0,
    bans_last_hour: int = 0,
) -> dict:
    """纯判定：这次告状该做什么。

    Args:
        severity: LLM 给的严重程度（近义词也认）。
        strikes: 该用户在过去 ``strikes_window_hours`` 内已被投诉的次数（不含本次）。
        settings: ``settings_from_config`` 的输出。
        alerts_last_hour: 过去一小时全局已发出的告警数。
        bans_last_hour: 过去一小时全局已执行的封禁数。

    Returns:
        dict：``ok``（输入是否合法）、``severity``、``level``、``escalated``、
        ``minutes``（判定的冷静时长）、``want_ban``、``should_ban``（受速率/演练
        限制后真正要执行的）、``should_alert``、``dry_run``。
    """
    level0 = _LEVELS.get(severity) if isinstance(severity, str) else None
    if level0 is None:
        return {
            "ok": False,
            "severity": "",
            "level": 0,
            "escalated": False,
            "minutes": 0,
            "want_ban": False,
            "should_ban": False,
            "should_alert": False,
            "dry_run": bool(settings.get("dry_run")),
        }

    level = level0
    need = max(1, int(settings.get("strikes_before_ban") or 2))
    try:
        count = max(0, int(strikes))
    except (TypeError, ValueError):
        count = 0
    # 第 need 次投诉起升一档：mild → moderate（终于封），moderate → severe
    if count + 1 >= need and level < 2:
        level += 1
    escalated = level > level0

    if level <= 0:
        minutes = 0
    elif level == 1:
        minutes = int(settings.get("ban_minutes_moderate") or 10)
    else:
        minutes = int(settings.get("ban_minutes_severe") or 60)
    minutes = min(max(0, minutes), int(settings.get("max_ban_minutes") or 1440))

    want_ban = minutes > 0
    try:
        ban_quota = int(settings.get("max_bans_per_hour") or 0)
        alert_quota = int(settings.get("max_alerts_per_hour") or 0)
        alerts_used = max(0, int(alerts_last_hour))
        bans_used = max(0, int(bans_last_hour))
    except (TypeError, ValueError):
        ban_quota, alert_quota, alerts_used, bans_used = 3, 6, 0, 0

    dry_run = bool(settings.get("dry_run"))
    return {
        "ok": True,
        "severity": severity,
        "level": level,
        "escalated": escalated,
        "minutes": minutes,
        "want_ban": want_ban,
        "should_ban": want_ban and bans_used < ban_quota and not dry_run,
        "should_alert": alerts_used < alert_quota,
        "dry_run": dry_run,
    }


def mask_openid(openid) -> str:
    """openid 打码：只留前 4 后 4，日志/群里不暴露完整 ID。"""
    text = str(openid or "").strip()
    if len(text) <= 10:
        return text
    return f"{text[:4]}…{text[-4:]}"


def ban_key(platform: str, sender: str) -> str:
    """冷静期键：平台 + 发送者 openid。同一个群里也只封这一个人。"""
    platform = str(platform or "").strip()
    sender = str(sender or "").strip()
    return f"{platform}:{sender}" if platform else sender


def format_duration(minutes: int) -> str:
    """分钟数变成「N 分钟 / N 小时」。"""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 0
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时" if not rest else f"{hours} 小时 {rest} 分钟"


def format_evidence(record: dict, *, severity: str, reason: str, action: str) -> str:
    """把证据排版成一段人能直接读的告警文本（给开发者看）。"""
    quote = str(record.get("message_str") or "").strip()
    if len(quote) > 240:
        quote = quote[:240] + "…"
    lines = [
        "【有人骂我】",
        f"平台：{record.get('platform') or '-'}",
    ]
    group_name = str(record.get("group_name") or "").strip()
    group_id = str(record.get("group_id") or "").strip()
    if group_id:
        lines.append(
            f"群：{group_name or '(未知群名)'}（{mask_openid(group_id)}）"
        )
    else:
        lines.append("场景：私聊")
    lines.append(
        f"用户：{record.get('sender_name') or '(无昵称)'}"
        f"（{mask_openid(record.get('sender_id'))}）"
    )
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.get('timestamp') or time.time()))}")
    lines.append(f"原话：{quote or '(空)'}")
    lines.append(
        f"程度：{SEVERITY_CN.get(severity, severity or '-')}"
        + (f" | 理由：{reason}" if reason else "")
    )
    lines.append(f"处理：{action}")
    return "\n".join(lines)


# ══════════════ 状态存储 ══════════════


def _default_data_dir() -> Path | None:
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_DIR_NAME))
    except Exception:  # noqa: BLE001
        return None


class DefenseStore:
    """冷静期 + 投诉计数 + 告警记录，JSON 落盘（data_dir=None 时退化为纯内存）。

    Args:
        data_dir: 不传则用 ``StarTools`` 的插件数据目录；显式传 ``None`` 表示
            纯内存（单测用，绝不读写真实数据目录）。
    """

    def __init__(self, data_dir=AUTO_DATA_DIR, clock=None) -> None:
        if data_dir is AUTO_DATA_DIR:
            self.data_dir = _default_data_dir()
        elif data_dir is None:
            self.data_dir = None
        else:
            self.data_dir = Path(data_dir)
        self.clock = clock or time.time
        # 冷静期：{"platform:openid": {until, minutes, severity, reason, quote, by, at, name}}
        self.bans: dict[str, dict] = {}
        # 投诉计数：{"platform:openid": [ts, ...]}
        self.strikes: dict[str, list[float]] = {}
        # 最近一次告警：{"platform:openid": ts}
        self.alert_marks: dict[str, float] = {}
        # 全局速率窗口
        self.alert_history: list[float] = []
        self.ban_history: list[float] = []
        # 告警对象：按平台实例分开存（openid 是按机器人隔离的，
        # 拿 A 机器人的 openid 去 B 机器人发私聊必然发不出去）
        self.alert_openid = ""
        self.alert_umo = ""
        self.alert_targets: dict[str, str] = {}
        self._loaded = False
        self._evidence_size: int | None = None

    # ── 路径 ──

    def _store_path(self) -> Path | None:
        return None if self.data_dir is None else self.data_dir / STORE_FILE

    def _evidence_path(self) -> Path | None:
        return None if self.data_dir is None else self.data_dir / EVIDENCE_FILE

    def _now(self, now=None) -> float:
        if now is not None:
            return float(now)
        try:
            return float(self.clock())
        except Exception:  # noqa: BLE001
            return time.time()

    # ── 落盘 ──

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
            logger.warning("AgentGate: 冷静期状态读取失败，按空状态启动: %s", exc)
            return
        if not isinstance(payload, dict):
            return
        bans = payload.get("bans")
        if isinstance(bans, dict):
            for key, item in bans.items():
                if isinstance(item, dict) and item.get("until"):
                    self.bans[str(key)] = item
        strikes = payload.get("strikes")
        if isinstance(strikes, dict):
            for key, items in strikes.items():
                if isinstance(items, list):
                    self.strikes[str(key)] = [
                        float(x) for x in items if isinstance(x, (int, float))
                    ]
        marks = payload.get("alert_marks")
        if isinstance(marks, dict):
            for key, value in marks.items():
                if isinstance(value, (int, float)):
                    self.alert_marks[str(key)] = float(value)
        self.alert_history = [
            float(x)
            for x in (payload.get("alert_history") or [])
            if isinstance(x, (int, float))
        ]
        self.ban_history = [
            float(x)
            for x in (payload.get("ban_history") or [])
            if isinstance(x, (int, float))
        ]
        self.alert_openid = _to_str(payload.get("alert_openid"))
        self.alert_umo = _to_str(payload.get("alert_umo"))
        targets = payload.get("alert_targets")
        if isinstance(targets, dict):
            for key, value in targets.items():
                key_s, val_s = _to_str(key), _to_str(value)
                if key_s and val_s:
                    self.alert_targets[key_s] = val_s
        self._prune(self._now())

    def persist(self, force: bool = False) -> None:
        path = self._store_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps(
                {
                    "version": 1,
                    "bans": self.bans,
                    "strikes": self.strikes,
                    "alert_marks": self.alert_marks,
                    "alert_history": self.alert_history[-200:],
                    "ban_history": self.ban_history[-200:],
                    "alert_openid": self.alert_openid,
                    "alert_umo": self.alert_umo,
                    "alert_targets": dict(self.alert_targets),
                },
                ensure_ascii=False,
                indent=2,
            )
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("AgentGate: 冷静期状态落盘失败: %s", exc)

    # ── 清理与上限 ──

    def _prune(self, now: float) -> None:
        for key, record in list(self.bans.items()):
            try:
                if float(record.get("until") or 0) <= now:
                    self.bans.pop(key, None)
            except (TypeError, ValueError):
                self.bans.pop(key, None)
        horizon = now - HISTORY_KEEP_SECONDS
        for key, items in list(self.strikes.items()):
            kept = [ts for ts in items if ts >= horizon]
            if kept:
                self.strikes[key] = kept
            else:
                self.strikes.pop(key, None)
        for key, ts in list(self.alert_marks.items()):
            if ts < horizon:
                self.alert_marks.pop(key, None)
        self.alert_history = [ts for ts in self.alert_history if ts >= horizon]
        self.ban_history = [ts for ts in self.ban_history if ts >= horizon]

    def _enforce_caps(self, cap: int) -> None:
        if len(self.bans) <= cap:
            return
        # 超出上限时只保留「最晚解封」的那批，避免有人靠狂刷投诉把别人顶掉
        ordered = sorted(
            self.bans.items(),
            key=lambda kv: float(kv[1].get("until") or 0),
            reverse=True,
        )
        self.bans = dict(ordered[:cap])

    # ── 冷静期 ──

    def active_ban(self, key: str, now=None) -> dict | None:
        now = self._now(now)
        record = self.bans.get(key)
        if not record:
            return None
        try:
            if float(record.get("until") or 0) <= now:
                self.bans.pop(key, None)
                self.persist()
                return None
        except (TypeError, ValueError):
            self.bans.pop(key, None)
            return None
        return record

    def set_ban(
        self,
        key: str,
        minutes: int,
        *,
        severity: str = "",
        reason: str = "",
        quote: str = "",
        by: str = "llm",
        name: str = "",
        now=None,
        cap: int = 200,
    ) -> dict:
        now = self._now(now)
        minutes = max(1, int(minutes))
        record = {
            "until": now + minutes * 60,
            "minutes": minutes,
            "severity": str(severity or ""),
            "reason": str(reason or "")[:120],
            "quote": str(quote or "")[:200],
            "by": str(by or "llm"),
            "at": now,
            "name": str(name or "")[:40],
        }
        self.bans[key] = record
        self.ban_history.append(now)
        self._prune(now)
        self._enforce_caps(max(1, int(cap or 200)))
        self.persist(force=True)
        return record

    def remove_ban(self, key: str) -> dict | None:
        record = self.bans.pop(key, None)
        if record is not None:
            self.persist(force=True)
        return record

    def list_bans(self, now=None) -> list[tuple[str, dict]]:
        now = self._now(now)
        self._prune(now)
        out = []
        for key, record in self.bans.items():
            try:
                if float(record.get("until") or 0) > now:
                    out.append((key, record))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda kv: float(kv[1].get("until") or 0), reverse=True)
        return out

    # ── 投诉计数 / 告警去重 / 速率 ──

    def strike_count(self, key: str, window_seconds: int, now=None) -> int:
        now = self._now(now)
        horizon = now - max(0, int(window_seconds))
        return len([ts for ts in self.strikes.get(key, []) if ts >= horizon])

    def mark_strike(self, key: str, now=None) -> int:
        now = self._now(now)
        items = self.strikes.setdefault(key, [])
        items.append(now)
        if len(items) > 50:
            del items[:-50]
        self.persist()
        return len(items)

    def recently_alerted(self, key: str, seconds: int, now=None) -> bool:
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return False
        last = self.alert_marks.get(key)
        if last is None:
            return False
        return (self._now(now) - float(last)) < seconds

    def mark_alert(self, key: str, now=None) -> None:
        now = self._now(now)
        self.alert_marks[key] = now
        self.alert_history.append(now)
        self.persist()

    def alerts_last_hour(self, now=None) -> int:
        horizon = self._now(now) - 3600
        return len([ts for ts in self.alert_history if ts >= horizon])

    def bans_last_hour(self, now=None) -> int:
        horizon = self._now(now) - 3600
        return len([ts for ts in self.ban_history if ts >= horizon])

    # ── 告警对象 ──

    def set_alert_target(
        self, openid: str = "", umo: str = "", instance: str = ""
    ) -> None:
        """绑定告警对象。

        Args:
            openid: 开发者在该机器人下的 openid（按机器人隔离）。
            umo: 完整私聊 umo（历史配置项，仍在用）。
            instance: 平台实例 ID；给了就按机器人分别存，没给则只更新全局兜底值。
        """
        text = str(openid or "").strip()
        if text and str(instance or "").strip():
            self.alert_targets[str(instance).strip()] = text
        if text:
            self.alert_openid = text
        if str(umo or "").strip():
            self.alert_umo = str(umo).strip()
        self.persist(force=True)

    def alert_target(self, instance: str = "") -> str:
        """取某个机器人下的告警对象 openid；没有则返回空串（不跨机器人借用）。"""
        key = str(instance or "").strip()
        if not key:
            return ""
        return str(self.alert_targets.get(key) or "").strip()

    # ── 证据 ──

    def log_evidence(self, record: dict) -> None:
        """追加一行证据到 JSONL；超过阈值先轮转到 .1。失败只记 debug。"""
        path = self._evidence_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._evidence_size is None:
                self._evidence_size = path.stat().st_size if path.is_file() else 0
            if self._evidence_size >= EVIDENCE_MAX_BYTES:
                rotated = path.with_name(path.name + ".1")
                try:
                    path.replace(rotated)
                except Exception:  # noqa: BLE001
                    pass
                self._evidence_size = 0
            line = json.dumps(record, ensure_ascii=False)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._evidence_size += len(line.encode("utf-8")) + 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("AgentGate: 证据落盘失败: %s", exc)


__all__ = [
    "AUTO_DATA_DIR",
    "DEFAULT_SETTINGS",
    "DefenseStore",
    "SEVERITIES",
    "SEVERITY_CN",
    "ban_key",
    "evaluate",
    "format_duration",
    "format_evidence",
    "mask_openid",
    "normalize_text",
    "settings_from_config",
    "severity_of",
    "strip_mentions",
    "system_hint",
    "verify_quote",
]
