"""provider 级配额与限流。

原先只统计当日调用次数，而且 record() 没有落盘，重启即丢。
这里补上落盘、每分钟请求数（滑动窗口）与 token 计量——
免费额度普遍同时卡日调用量和 RPM，只看其中一个会在另一边撞墙。
"""

import threading
import time
from collections import deque
from datetime import datetime

import yaml

from core.paths import CONFIG_FILE, QUOTA_FILE
from core.storage import ThrottledStore, read_json

RPM_WINDOW = 60.0


def today():
    return datetime.now().strftime("%Y-%m-%d")


class QuotaManager:

    def __init__(self, config_file=CONFIG_FILE):
        self.lock = threading.RLock()
        self.store = ThrottledStore(QUOTA_FILE)
        self.config = self.load_config(config_file)
        self.data = self.load()

        # provider -> 最近请求时间戳，用于 RPM 滑动窗口
        self.recent = {}

    def load_config(self, config_file):
        try:
            with open(config_file, "r") as f:
                config = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return {}

        return config.get("quota") or {}

    def default_data(self):
        return {"date": today(), "providers": {}}

    def load(self):
        data = read_json(QUOTA_FILE)

        if not data or data.get("date") != today():
            return self.default_data()

        data.setdefault("providers", {})
        return data

    def roll_if_needed(self):
        """调用方已持锁。"""

        if self.data.get("date") != today():
            self.store.maybe_flush(self.data, force=True)
            self.data = self.default_data()
            self.recent = {}

    def get_limit(self, provider, field, default=0):
        return (self.config.get(provider) or {}).get(field, default)

    def get_usage(self, provider):
        """调用方已持锁。"""

        providers = self.data["providers"]

        if provider not in providers:
            providers[provider] = {"calls": 0, "tokens": 0}

        providers[provider].setdefault("tokens", 0)
        return providers[provider]

    def current_rpm(self, provider, now=None):
        """调用方已持锁。"""

        now = now or time.time()
        window = self.recent.get(provider)

        if window is None:
            window = deque()
            self.recent[provider] = window

        while window and (now - window[0]) > RPM_WINDOW:
            window.popleft()

        return len(window)

    def allowed(self, provider):
        with self.lock:
            self.roll_if_needed()

            daily = self.get_limit(provider, "daily")

            # 0 表示不限
            if daily and self.get_usage(provider)["calls"] >= daily:
                return False

            rpm = self.get_limit(provider, "rpm")

            if rpm and self.current_rpm(provider) >= rpm:
                return False

            return True

    def record(self, provider, tokens=0):
        with self.lock:
            self.roll_if_needed()

            usage = self.get_usage(provider)
            usage["calls"] += 1

            if tokens:
                usage["tokens"] += tokens

            self.current_rpm(provider)  # 顺带清理过期窗口
            self.recent[provider].append(time.time())

            self.store.mark_dirty()
            self.store.maybe_flush(self.data)

    def snapshot(self):
        with self.lock:
            self.roll_if_needed()

            result = {}

            for name, usage in self.data.get("providers", {}).items():
                result[name] = {
                    "calls": usage.get("calls", 0),
                    "tokens": usage.get("tokens", 0),
                    "rpm_now": self.current_rpm(name),
                    "daily_limit": self.get_limit(name, "daily"),
                    "rpm_limit": self.get_limit(name, "rpm"),
                }

            return result

    def flush(self):
        with self.lock:
            return self.store.maybe_flush(self.data, force=True)

    def record_tokens_only(self, provider, tokens):
        """只补记 token，不增加调用次数。

        流式请求的 usage 要等最后一个分块才拿得到，
        那时 record() 早已在路由阶段调用过了。
        """

        if not tokens:
            return

        with self.lock:
            self.roll_if_needed()
            self.get_usage(provider)["tokens"] += tokens
            self.store.mark_dirty()
            self.store.maybe_flush(self.data)
