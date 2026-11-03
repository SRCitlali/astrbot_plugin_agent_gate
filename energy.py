# -*- coding: utf-8 -*-
"""AgentGate 精力账本。

算法与数值**照搬** `astrbot_plugin_angel_heart/core/debounce_manager.py` 的精力部分：
常量、单会话状态结构、惰性恢复、按最终消息链的有效字符数扣能、下限夹紧。

与天使之心的差异只有三处，都是为落位到 AgentGate 而做的适配：

1. **记账范围**：账本本身不判身份，只负责算。由调用方（main.py）通过
   `agentgate_energy_eligible` 标记决定某条消息要不要参与结算。AgentGate 只让
   「白名单外会话」参与，白名单内与豁免的管理员不读不写精力。
2. **无锁**：天使之心的 `charge_reply_energy` 在 asyncio 锁内做，是因为它的临界区
   里含 await；这里 `recover()` / `charge()` 全程没有 await，单线程 asyncio 下
   天然原子，去掉锁也让单测可以直接同步调用。
3. **幂等标记由调用方持有**：天使之心把 `angelheart_energy_charged` 写在 event 上，
   这里保持同样的做法，但由 main.py 的钩子读写，账本保持纯函数式。

精力是**内存态、不持久化**：插件重载或机器人重启后重置为 `initial_energy`，
与天使之心一致（见天使之心 `debounce_manager.py` 中 `ChatEnergyState` 的说明）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger(__name__)


# —— 默认值，全部照搬天使之心 core/debounce_manager.py ——

MAXIMUM_ENERGY = 100.0
MINIMUM_ENERGY = -100.0
INITIAL_ENERGY = 100.0
ENERGY_RECOVERY_PER_SECOND = 0.6
BASE_REPLY_ENERGY_COST = 14.0
ENERGY_COST_PER_CHARACTER = 0.12


@dataclass
class ChatEnergyState:
    """单会话运行时精力；不持久化，插件重启后重新初始化。"""

    energy: float = INITIAL_ENERGY
    updated_at: float = field(default_factory=time.time)


class EnergyLedger:
    """精力账本。

    Args:
        settings: 返回精力配置字典的无参可调用对象（通常是插件读配置的方法）。
            为 None 时全部使用上面的照搬默认值，方便单测。
    """

    def __init__(
        self,
        settings: Optional[Callable[[], Mapping[str, Any]]] = None,
    ) -> None:
        self._settings = settings
        self._states: Dict[str, ChatEnergyState] = {}

    # ── 参数读取（每次现读，容忍运行期改配置 / 缺键 / 类型不对） ──

    def _num(self, key: str, default: float) -> float:
        if self._settings is None:
            return float(default)
        try:
            raw = self._settings().get(key, default)
        except Exception:
            return float(default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(default)

    def initial_energy(self) -> float:
        return self._num("initial_energy", INITIAL_ENERGY)

    def maximum_energy(self) -> float:
        return self._num("max_energy", MAXIMUM_ENERGY)

    def minimum_energy(self) -> float:
        return self._num("min_energy", MINIMUM_ENERGY)

    def recovery_per_second(self) -> float:
        return self._num("recovery_per_second", ENERGY_RECOVERY_PER_SECOND)

    def base_reply_cost(self) -> float:
        return self._num("base_reply_cost", BASE_REPLY_ENERGY_COST)

    def cost_per_character(self) -> float:
        return self._num("reply_cost_per_character", ENERGY_COST_PER_CHARACTER)

    # ── 状态 ──

    def _state(self, chat_id: Any) -> ChatEnergyState:
        key = str(chat_id or "")
        state = self._states.get(key)
        if state is None:
            state = ChatEnergyState(energy=self.initial_energy())
            self._states[key] = state
            logger.debug("AgentGate[%s]: 精力初始化 energy=%.2f", key, state.energy)
        return state

    def get(self, chat_id: Any) -> float:
        """读取当前精力（不触发恢复，但会建立状态）。"""
        return self._state(chat_id).energy

    def peek(self, chat_id: Any) -> float:
        """只读估算：既不建立状态也不改时间戳。

        没有状态时返回 `initial_energy`；有状态时按经过时间估算当前值。
        供 `is_allowed()` 这类不希望产生副作用的查询使用。
        """
        key = str(chat_id or "")
        state = self._states.get(key)
        if state is None:
            return self.initial_energy()
        elapsed = max(0.0, time.time() - state.updated_at)
        return min(
            self.maximum_energy(),
            state.energy + elapsed * self.recovery_per_second(),
        )

    def has_state(self, chat_id: Any) -> bool:
        """该会话是否已经建立过精力状态（用于"白名单内不记账"的断言）。"""
        return str(chat_id or "") in self._states

    def recover(self, chat_id: Any) -> ChatEnergyState:
        """按经过时间惰性恢复一次，返回恢复后的状态。

        对应天使之心的 `_recover_energy_before_patrol`：只在放行判定前调用，
        不是后台定时器。本次恢复量由调用方用 `before = get()` /
        `after = recover().energy` 自己算（天使之心也是这么做的）。
        """
        state = self._state(chat_id)
        now = time.time()
        elapsed = max(0.0, now - state.updated_at)
        state.energy = min(
            self.maximum_energy(),
            state.energy + elapsed * self.recovery_per_second(),
        )
        state.updated_at = now
        return state

    def charge(
        self, chat_id: Any, message_chain: Iterable[Any]
    ) -> Optional[Tuple[float, float, int, float]]:
        """按最终消息链扣能一次。

        Returns:
            `(before, after, characters, cost)`；`message_chain` 为空时返回 None。
        """
        if not message_chain:
            return None

        character_count = self.effective_character_count(message_chain)
        cost = self.base_reply_cost() + character_count * self.cost_per_character()

        state = self._state(chat_id)
        before = state.energy
        state.energy = max(self.minimum_energy(), state.energy - cost)
        state.updated_at = time.time()
        return before, state.energy, character_count, cost

    def clear(self) -> None:
        """清空全部精力状态（插件重载 / 卸载时调用）。"""
        if self._states:
            logger.info("AgentGate: 清空 %d 个会话的精力状态", len(self._states))
        self._states.clear()

    def snapshot(self) -> Dict[str, float]:
        return {key: state.energy for key, state in self._states.items()}

    @staticmethod
    def effective_character_count(message_chain: Iterable[Any]) -> int:
        """有效字符数，照搬天使之心的 `_effective_character_count`。

        依次取组件的 `.text`，取不到再取 `.data["text"]`，拼起来 strip 后计长度。
        """
        text_parts = []
        for component in message_chain:
            text = getattr(component, "text", None)
            if text is not None:
                text_parts.append(str(text))
                continue
            data = getattr(component, "data", None)
            if isinstance(data, dict):
                text = data.get("text", "")
                if text:
                    text_parts.append(str(text))
        return len("".join(text_parts).strip())
