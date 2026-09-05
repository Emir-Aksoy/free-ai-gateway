"""低频后台健康探测；探测消耗配额，但不改变业务调用统计。"""
import copy
import threading
import time

from core.registry import get_provider
from providers.base import ProviderError, response_has_output, usage_tokens


class RecoveryWorker:
    def __init__(self, router, logger=None):
        self.router = router
        self.logger = logger
        self.stopped = threading.Event()
        self.run_lock = threading.Lock()
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="model-recovery", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _log(self, data):
        if self.logger:
            try:
                self.logger.write("recovery", data)
            except OSError:
                pass

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.run_once()
            except Exception:
                # 不保存上游错误文本，避免地址、凭据或响应内容进入状态。
                self._log({"event": "recovery_worker_error"})
            self.stopped.wait(30)

    def run_once(self):
        if not self.run_lock.acquire(blocking=False):
            return
        try:
            targets = set(self.router.fallbacks)
            for chain in self.router.modes.values():
                if isinstance(chain, dict):
                    for task_chain in chain.values():
                        targets.update(task_chain)
                elif isinstance(chain, list):
                    targets.update(chain)
            for target in sorted(targets):
                if self.stopped.is_set():
                    break
                self._probe(target)
        finally:
            self.run_lock.release()

    def _probe(self, target):
        state = self.router.state
        if not state.is_disabled(target):
            return
        provider_name, model = self.router.parse_target(target)
        # 在配额锁内检查并预留本次探测，避免多个探测争用剩余额度。
        with self.router.quota.lock:
            if not self.router.quota.allowed(provider_name):
                return
            token = state.claim_recovery(target, time.time())
            if token is None:
                return
            self.router.quota.record(provider_name)
        success = False
        tokens = 0
        try:
            # 探测独立超时与重试设置，不修改前台复用的 provider 实例。
            provider = copy.copy(get_provider(provider_name))
            provider.max_retries = 0
            provider.connect_timeout = 5
            result = provider.chat(model, [{"role": "user", "content": "Reply exactly OK."}],
                                   params={"max_tokens": 256}, timeout=20)
            tokens = usage_tokens(result)
            success = response_has_output(result)
        except ProviderError as error:
            tokens = error.tokens
        except Exception:
            pass
        finally:
            if tokens:
                self.router.quota.record_tokens_only(provider_name, tokens)
            self.router.quota.flush()
        # 停止时不让迟到的探测重新启用模型；落盘的预约到期后会重试。
        if self.stopped.is_set():
            return
        with self.router.lock:
            if state.finish_recovery(target, token, success, time.time()):
                if success:
                    self.router.mark_success(target)
                self._log({"event": "model_recovered" if success else "recovery_failed",
                           "target": target})
