"""线程安全的每日业务统计，以及独立于日期的自动禁用/恢复状态。"""

import copy
import threading
import time
import uuid
from datetime import datetime

from core.paths import STATE_FILE
from core.storage import ThrottledStore, read_json

# 本轮累计失败达到阈值且当日成功率低时，暂停路由并等待后台恢复。
DISABLE_AFTER_FAILURES = 8
DISABLE_SUCCESS_RATE = 0.15
RECOVERY_DELAYS = (900, 1800, 3600)


def today():
    return datetime.now().strftime("%Y-%m-%d")


class StateManager:

    def __init__(self):
        self.lock = threading.RLock()
        self.store = ThrottledStore(STATE_FILE)
        self.state = self.load()

    def default_state(self):
        return {"date": today(), "models": {}}

    def new_day_state(self, previous):
        state = self.default_state()
        for key, model in (previous or {}).get("models", {}).items():
            if model.get("disabled"):
                recovery = copy.deepcopy(model.get("recovery", {}))
                # 新日统计不能被前一天仍在途的探测结果覆盖。
                recovery.pop("token", None)
                state["models"][key] = {
                    "calls": 0, "success": 0, "failed": 0, "disabled": True,
                    "latency_total": 0, "latency_avg": 0, "tokens": 0,
                    "recovery": recovery,
                }
        self.store.maybe_flush(state, force=True)
        return state

    def load(self):
        state = read_json(STATE_FILE)

        if not state or state.get("date") != today():
            return self.new_day_state(state)

        state.setdefault("models", {})
        return state

    def roll_if_needed(self):
        """跨天则清空当日统计。调用方已持锁。"""

        if self.state.get("date") != today():
            self.state = self.new_day_state(self.state)
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
            if model["disabled"]:
                model["failure_baseline"] = model["failed"] - model.get("excluded_failures", 0)
            model["disabled"] = False
            model.pop("recovery", None)

            self.store.mark_dirty()
            self.store.maybe_flush(self.state)

    def record_failure(self, key, eligible=True):
        with self.lock:
            model = self.get_model(key)

            model["calls"] += 1
            model["failed"] += 1
            newly_disabled = False

            if not eligible:
                model["excluded_failures"] = model.get("excluded_failures", 0) + 1
            failures = model["failed"] - model.get("excluded_failures", 0)
            if eligible and failures - model.get("failure_baseline", 0) >= DISABLE_AFTER_FAILURES:
                rate = model["success"] / max(model["success"] + failures, 1)

                if rate < DISABLE_SUCCESS_RATE and not model["disabled"]:
                    model["disabled"] = True
                    newly_disabled = True
                    model["recovery"] = {"next_at": time.time() + RECOVERY_DELAYS[0], "attempts": 0}

            self.store.mark_dirty()
            self.store.maybe_flush(self.state, force=newly_disabled)

    def claim_recovery(self, key, now):
        """预留一次探测并落盘；旧 disabled 状态立即可探测。"""
        with self.lock:
            model = self.get_model(key)
            recovery = model.get("recovery", {})
            if not model["disabled"] or recovery.get("next_at", 0) > now:
                return None
            token = uuid.uuid4().hex
            recovery.update(token=token, next_at=now + RECOVERY_DELAYS[0])
            model["recovery"] = recovery
            self.store.mark_dirty()
            self.flush()
            return token

    def finish_recovery(self, key, token, success, now):
        """只接收当前禁用周期的结果；不将健康探测混入业务统计。"""
        with self.lock:
            model = self.get_model(key)
            recovery = model.get("recovery", {})
            if not model["disabled"] or recovery.get("token") != token:
                return False
            recovery.pop("token", None)
            attempts = recovery.get("attempts", 0) + 1
            recovery.update(attempts=attempts, last_at=now,
                            last_result="recovered" if success else "failed")
            if success:
                model["disabled"] = False
                model["failure_baseline"] = model["failed"] - model.get("excluded_failures", 0)
                recovery["next_at"] = None
            else:
                recovery["next_at"] = now + RECOVERY_DELAYS[min(attempts, len(RECOVERY_DELAYS) - 1)]
            self.store.mark_dirty()
            self.flush()
            return True

    def snapshot(self):
        with self.lock:
            self.roll_if_needed()
            return {
                "date": self.state.get("date"),
                "models": copy.deepcopy(self.state.get("models", {})),
            }

    def flush(self):
        with self.lock:
            return self.store.maybe_flush(self.state, force=True)

    def reset(self):
        with self.lock:
            self.state = self.default_state()
            self.store.maybe_flush(self.state, force=True)
