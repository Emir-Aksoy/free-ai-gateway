"""每个模型的当日调用统计。

除了线程安全和原子写，还补上两件原先缺失的事：
运行中跨天会自动滚动（原先只在启动时判断日期），
以及 disabled 字段真正会被写入——原先没有任何代码把它设成 True，
所以出现过某模型连续 11 次失败仍留在路由池里的情况。
"""

import threading
from datetime import datetime

from core.paths import STATE_FILE
from core.storage import ThrottledStore, read_json

# 当日累计失败达到该次数、且成功率低于阈值时，当天不再路由到它。
DISABLE_AFTER_FAILURES = 8
DISABLE_SUCCESS_RATE = 0.15


def today():
    return datetime.now().strftime("%Y-%m-%d")


class StateManager:

    def __init__(self):
        self.lock = threading.RLock()
        self.store = ThrottledStore(STATE_FILE)
        self.state = self.load()

    def default_state(self):
        return {"date": today(), "models": {}}

    def load(self):
        state = read_json(STATE_FILE)

        if not state or state.get("date") != today():
            return self.default_state()

        state.setdefault("models", {})
        return state

    def roll_if_needed(self):
        """跨天则清空当日统计。调用方已持锁。"""

        if self.state.get("date") != today():
            self.store.maybe_flush(self.state, force=True)
            self.state = self.default_state()
            return True

        return False

    def get_model(self, key):
        with self.lock:
            self.roll_if_needed()

            models = self.state["models"]

            if key not in models:
                models[key] = {
                    "calls": 0,
                    "success": 0,
                    "failed": 0,
                    "disabled": False,
                    "latency_total": 0,
                    "latency_avg": 0,
                    "tokens": 0,
                }

            models[key].setdefault("tokens", 0)
            return models[key]

    def is_disabled(self, key):
        with self.lock:
            return bool(self.get_model(key).get("disabled"))

    def record_success(self, key, latency, tokens=0):
        with self.lock:
            model = self.get_model(key)

            model["calls"] += 1
            model["success"] += 1
            model["latency_total"] = round(
                model["latency_total"] + latency, 3
            )
            model["latency_avg"] = round(
                model["latency_total"] / model["success"], 3
            )

            if tokens:
                model["tokens"] += tokens

            # 恢复了就解禁，免费额度按天重置，中途恢复是常事
            model["disabled"] = False

            self.store.mark_dirty()
            self.store.maybe_flush(self.state)

    def record_failure(self, key):
        with self.lock:
            model = self.get_model(key)

            model["calls"] += 1
            model["failed"] += 1

            if model["failed"] >= DISABLE_AFTER_FAILURES:
                rate = model["success"] / max(model["calls"], 1)

                if rate < DISABLE_SUCCESS_RATE:
                    model["disabled"] = True

            self.store.mark_dirty()
            self.store.maybe_flush(self.state)

    def snapshot(self):
        with self.lock:
            self.roll_if_needed()
            return {
                "date": self.state.get("date"),
                "models": dict(self.state.get("models", {})),
            }

    def flush(self):
        with self.lock:
            return self.store.maybe_flush(self.state, force=True)

    def reset(self):
        with self.lock:
            self.state = self.default_state()
            self.store.maybe_flush(self.state, force=True)
