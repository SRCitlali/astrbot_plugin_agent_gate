# -*- coding: utf-8 -*-
"""测试公共环境：把插件目录挂到 sys.path，并提供事件桩与插件构造器。

文件名不以 `test_` 开头，所以不会被收集。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_PLUGINS_DIR = _PLUGIN_DIR.parent

for _p in (str(_PLUGINS_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from astrbot.core.message.message_event_result import MessageEventResult  # noqa: E402

from astrbot_plugin_agent_gate import main as ag  # noqa: E402
from astrbot_plugin_agent_gate.energy import EnergyLedger  # noqa: E402


def plain_chain(text: str):
    """用框架自己的 MessageEventResult 造一条真实消息链。"""
    return MessageEventResult().message(text).chain


class DictComponent:
    """只有 `.data["text"]` 的组件，用来覆盖字符数统计的第二分支。"""

    def __init__(self, text: str):
        self.data = {"text": text}


class StubGroup:
    def __init__(self, group_id: str = "", group_name: str = ""):
        self.group_id = group_id
        self.group_name = group_name


class StubSender:
    def __init__(self, user_id: str = "", nickname: str = ""):
        self.user_id = user_id
        self.nickname = nickname


class StubMessageObj:
    """最小的 ``AstrBotMessage`` 替身：告状取证会读这几个字段。"""

    def __init__(
        self,
        *,
        message_id: str = "MID",
        timestamp: float = 0.0,
        group_id: str = "",
        group_name: str = "",
        sender_id: str = "S1",
        sender_name: str = "",
        chain=None,
    ):
        self.message_id = message_id
        self.timestamp = timestamp
        self.group = StubGroup(group_id, group_name) if group_id else None
        self.sender = StubSender(sender_id, sender_name)
        self.message = list(chain or [])


class StubEvent:
    """只实现 AgentGate 真正会用到的那几个方法。"""

    def __init__(
        self,
        *,
        umo: str = "qq_official:FriendMessage:ABC",
        platform: str = "qq_official",
        sender: str = "S1",
        group: str = "",
        session: str = "ABC",
        admin: bool = False,
        wake: bool = True,
        message_str: str = "你好",
        sender_name: str = "",
        group_name: str = "",
        message_id: str = "MID",
        timestamp: float = 0.0,
        chain=None,
    ):
        self.unified_msg_origin = umo
        self.platform = platform
        self.sender = sender
        self.group = group
        self.session = session
        self.admin = admin
        self.is_at_or_wake_command = wake
        self.is_wake = wake
        self.message_str = message_str
        self.stopped = False
        self.result = None
        self.extras: dict = {}
        self.self_id = "BOT"
        self._sender_name = sender_name
        self._chain = list(chain or [])
        self.message_obj = StubMessageObj(
            message_id=message_id,
            timestamp=timestamp,
            group_id=group,
            group_name=group_name,
            sender_id=sender,
            sender_name=sender_name,
            chain=self._chain,
        )

    # —— 事件取值 ——
    def get_platform_name(self) -> str:
        return self.platform

    def get_platform_id(self) -> str:
        return self.platform

    def get_sender_id(self) -> str:
        return self.sender

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self.group

    def get_session_id(self) -> str:
        return self.session

    def get_self_id(self) -> str:
        return self.self_id

    def get_messages(self):
        return self._chain

    def get_message_str(self) -> str:
        return self.message_str

    def is_private_chat(self) -> bool:
        return not self.group

    def is_admin(self) -> bool:
        return self.admin

    # —— event extra ——
    def get_extra(self, key=None, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    # —— 结果 ——
    def make_result(self):
        return MessageEventResult()

    def plain_result(self, text):
        return MessageEventResult().message(text)

    def set_result(self, result):
        self.result = result

    def get_result(self):
        return self.result

    def clear_result(self):
        self.result = None

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True


def build_plugin(config=None):
    """构造一个不经 AstrBot 生命周期、只跑 _setup 的插件实例。

    副作用：会把自己登记为当前存活实例（唤醒阶段的过滤器要用）。
    """
    plugin = ag.AgentGatePlugin.__new__(ag.AgentGatePlugin)
    plugin.context = None
    plugin._setup(dict(config or {}))
    return plugin


def wl(*entries, **overrides) -> dict:
    """构造 whitelist 二级组的配置片段。"""
    cfg = {"entries": list(entries)}
    cfg.update(overrides)
    return {"whitelist": cfg}


def energy_config(enabled: bool = True, **overrides) -> dict:
    """构造精力开关 + energy 二级组的配置片段。"""
    cfg = {
        "initial_energy": 100.0,
        "max_energy": 100.0,
        "min_energy": -100.0,
        "recovery_per_second": 0.6,
        "base_reply_cost": 14.0,
        "reply_cost_per_character": 0.12,
        "messages": ["累了"],
    }
    cfg.update(overrides)
    return {"energy_enabled": enabled, "energy": cfg}


def defense_config(**overrides) -> dict:
    """构造 defense 二级组的配置片段。"""
    cfg = {
        "enabled": True,
        "alert_openid": "DEVOPENID123456",
        "messages": ["不想说话"],
        "cooldown_minutes": 5,
    }
    cfg.update(overrides)
    return {"defense": cfg}


def attach_memory_defense(plugin, clock=None):
    """给插件挂一个纯内存的 DefenseStore，测试不碰真实数据目录。"""
    from astrbot_plugin_agent_gate.defense import DefenseStore

    store = DefenseStore(None, clock=clock)
    store.load()
    plugin._defense = store
    return store


class FakeContext:
    """记录 send_message 调用的最小 Context 替身。"""

    def __init__(self, fail: bool = False, result: bool = True):
        self.sent: list[tuple[str, object]] = []
        self.fail = fail
        self.result = result

    async def send_message(self, session, message_chain):
        if self.fail:
            raise RuntimeError("qq_official 主动消息不支持")
        self.sent.append((session, message_chain))
        return self.result


class FakePlatformMeta:
    def __init__(self, name: str = "qq_official", platform_id: str = "Citlali"):
        self.name = name
        self.id = platform_id


class FakePlatform:
    def __init__(self, name: str = "qq_official", platform_id: str = "Citlali"):
        self._meta = FakePlatformMeta(name, platform_id)

    def meta(self):
        return self._meta


class FakePlatformManager:
    def __init__(self, insts=None):
        self.platform_insts = list(insts or [FakePlatform()])


class FakeToolSet:
    def __init__(self, names=None):
        self._names = list(names or [])

    def names(self):
        return list(self._names)


class StubProviderRequest:
    """ProviderRequest 的最小替身：注入提示的钩子只改 system_prompt。"""

    def __init__(self, system_prompt: str = "你是傲娇少女。", func_tool=None):
        self.system_prompt = system_prompt
        self.func_tool = func_tool


class FakeContextWithPlatforms(FakeContext):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.platform_manager = FakePlatformManager()


__all__ = [
    "ag",
    "EnergyLedger",
    "FakeContext",
    "FakeContextWithPlatforms",
    "FakePlatform",
    "FakePlatformManager",
    "FakeToolSet",
    "StubEvent",
    "StubMessageObj",
    "StubProviderRequest",
    "DictComponent",
    "attach_memory_defense",
    "build_plugin",
    "defense_config",
    "energy_config",
    "plain_chain",
    "wl",
]
