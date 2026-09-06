"""路由层：按模式/任务类型挑选模型，失败后沿降级链继续。

保留原有的 capability 三维打分思路，修掉三处会影响决策的缺陷：
  1. speed_score 阈值原本按毫秒级 API 设计，LLM 请求全部落进 0 分档，该维度失效；
  2. fail_counts 只增不减，模型偶发失败几次后会永久停在最长冷却档；
  3. 冷却状态只在内存，进程一重启坏模型立刻满血复活。
"""

import json
import copy
import sqlite3
import os
import threading
import time
import uuid

import yaml

from core.paths import CONFIG_FILE, COOLDOWN_FILE
from core.registry import configure_providers, get_provider
from core.state import StateManager
from core.model_logs import ModelCallLog, failure_code
from core.call_trace import CallTrace
from core.quota import QuotaManager
from core.capability import CapabilityManager
from core.routing_metrics import RoutingMetrics
from router.policy import effective_policy, failure_category
from providers.base import ProviderError, response_has_output, usage_tokens

# 冷却梯度：首次失败短冷却给个机会，连续失败才拉长。
COOLDOWN_STEPS = (300, 900, 1800)

# 按实测延迟分布定档（groq 0.1-0.3s / agnes 0.7-2.7s / gemini 1-8s /
# openrouter 0.7-4.6s / opencode nemotron 34s）。原来的 0.5/1/2 秒阈值
# 对 LLM 没有区分度。最慢档保留 10 分而不是 0，避免慢但可用的模型
# 与完全不可用的模型得分相同。
SPEED_TIERS = ((1.0, 100), (3.0, 80), (8.0, 60), (20.0, 30))
SPEED_FLOOR = 10

# 样本太少时不做速度/成功率判断，先给满分让它有机会积累数据。
MIN_SAMPLES = 10


class AIRouter:

    def __init__(self, config_file=CONFIG_FILE):
        with open(config_file, "r") as f:
            self.config = yaml.safe_load(f)

        self.modes = self.config["gateway"]["modes"]
        self.routing = self.config["gateway"].get("routing") or {}
        self.fallbacks = self.config.get("fallback", [])

        # providers.<name>.env 指定各家密钥文件的位置；不写则用 provider 类里的默认路径
        providers_cfg = self.config.get("providers") or {}
        configure_providers(providers_cfg)

        self.state = StateManager()
        self.model_log = ModelCallLog()
        self.quota = QuotaManager()
        self.capability = CapabilityManager()
        self.metrics = RoutingMetrics()

        # ThreadingHTTPServer 下多个请求会并发读写这些结构。
        self.lock = threading.RLock()

        self.cooldowns = {}
        self.fail_counts = {}
        self.load_cooldowns()

    # ---------- 冷却状态持久化 ----------

    def load_cooldowns(self):
        if not os.path.exists(COOLDOWN_FILE):
            return

        try:
            with open(COOLDOWN_FILE, "r") as f:
                data = json.load(f)
        except (ValueError, OSError):
            return

        now = time.time()

        for target, info in data.get("cooldowns", {}).items():
            until = info.get("until", 0)

            # 只恢复还没到期的，过期的直接丢掉
            if until > now:
                self.cooldowns[target] = until
                self.fail_counts[target] = info.get("fails", 1)

    def save_cooldowns(self, locked=False):
        """把未过期的冷却写盘。

        locked=True 表示调用方已持锁（mark_failure/mark_success 走这条）。
        从 flush_all 这类外部路径调用时必须让它自己加锁，否则会在
        工作线程正改这两个字典时迭代到一半，抛 RuntimeError。
        """

        if not locked:
            with self.lock:
                return self.save_cooldowns(locked=True)

        now = time.time()

        payload = {
            "cooldowns": {
                target: {
                    "until": until,
                    "fails": self.fail_counts.get(target, 1),
                }
                for target, until in self.cooldowns.items()
                if until > now
            }
        }

        tmp = COOLDOWN_FILE + ".tmp"

        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=1)

            os.replace(tmp, COOLDOWN_FILE)
        except OSError:
            pass

    # ---------- 路由选择 ----------

    def parse_target(self, target):
        provider, model = target.split(":", 1)
        return provider, model

    def get_routes(self, mode, task_type=None):
        """解析模式名到降级链。

        task 子表里的名字（code/writing/agent）也允许直接作为 model 传进来，
        原先只有 mode=='task' 才查子表，客户端传 code 会 KeyError 打崩连接。
        """

        task_modes = self.modes.get("task", {})

        if mode == "task":
            return task_modes.get(task_type, self.modes["balanced"])

        if mode in task_modes:
            return task_modes[mode]

        if mode in self.modes:
            routes = self.modes[mode]

            # task 是子表不是降级链，不能直接返回
            if isinstance(routes, list):
                return routes

        return self.modes["balanced"]

    def _legacy_rank_routes(self, routes, task_type=None):
        now = time.time()

        with self.lock:
            cooldowns = dict(self.cooldowns)

        if self.routing.get("mode", "scored") == "manual":
            # 手动模式完全保留配置顺序，fallback 也不得被搬到链尾。
            available = [target for target in routes
                         if now >= cooldowns.get(target, 0)]
            return available

        # scored 模式保持旧版 fallback 兜底行为。
        normal = [x for x in routes if x not in self.fallbacks]
        tail = [x for x in routes if x in self.fallbacks]

        scored = []

        for target in normal:
            if target in cooldowns and now < cooldowns[target]:
                continue

            state = self.state.get_model(target)

            calls = state.get("calls", 0)
            success = state.get("success", 0)
            latency = state.get("latency_avg", 999)

            success_rate = 1.0 if calls < MIN_SAMPLES else success / calls

            cap = self.capability.get(target)
            capability_score = self.capability_score(cap, task_type)

            if calls < MIN_SAMPLES:
                speed_score = 100
            else:
                speed_score = SPEED_FLOOR

                for threshold, points in SPEED_TIERS:
                    if latency <= threshold:
                        speed_score = points
                        break

            score = capability_score * 0.7
            if self.routing.get("use_success_rate", True):
                score += success_rate * 100 * 0.2
            if self.routing.get("use_latency", True):
                score += speed_score * 0.1

            scored.append((score, target))

        scored.sort(key=lambda item: item[0], reverse=True)

        ranked = [target for _, target in scored]

        # 全被冷却时不能返回空链，否则整个请求直接失败；
        # 退回原始顺序让它们再试一次总好过没有候选。
        if not ranked and not tail:
            return list(normal)

        return ranked + tail

    def rank_routes(self, routes, task_type=None):
        if not hasattr(self, "metrics"):
            return self._legacy_rank_routes(routes, task_type)
        return self.explain_routes(routes, task_type)["order"]

    def explain_routes(self, routes, task_type, preserve_order=False):
        policy = effective_policy(self.routing, task_type)
        if preserve_order:
            policy = dict(policy, mode="manual")
        now = time.time()
        with self.lock:
            cooldowns = dict(self.cooldowns)
        candidates = []
        for position, target in enumerate(routes):
            recent = self.metrics.stats(target, task_type)
            cap = self.capability_score(self.capability.get(target), task_type)
            # Missing/low samples are neutral. Completion duration is never used as TTFT.
            speed = 50
            if recent["ttft_samples"] >= MIN_SAMPLES and recent["ttft"] is not None:
                speed = next((points for threshold, points in SPEED_TIERS if recent["ttft"] <= threshold), SPEED_FLOOR)
            parts = {"capability": round(cap * .7, 3),
                     "success": round(recent["smoothed_success_rate"] * 20, 3) if policy["use_success_rate"] else 0,
                     "latency": speed * .1 if policy["use_latency"] else 0}
            provider = target.split(":", 1)[0]
            blocked = self.metrics.provider_status(provider)
            skip = (blocked["reason"] if blocked else
                    "disabled_pending_recovery" if self.state.is_disabled(target) else
                    "cooldown" if cooldowns.get(target, 0) > now else
                    "quota_exceeded" if not self.quota.allowed(provider) else None)
            candidates.append({"target": target, "position": position + 1,
                               "score": round(sum(parts.values()), 3), "components": parts,
                               "recent": recent, "skip_reason": skip,
                               "retry_at": blocked["until"] if blocked else cooldowns.get(target)})
        active = [row for row in candidates if cooldowns.get(row["target"], 0) <= now]
        if policy["mode"] != "manual":
            active.sort(key=lambda row: (policy["mode"] == "scored" and row["target"] in self.fallbacks, -row["score"]))
            if policy["mode"] == "preferred" and routes:
                active.sort(key=lambda row: row["target"] != routes[0])
        order = [row["target"] for row in active]
        for row in candidates:
            row["rank"] = order.index(row["target"]) + 1 if row["target"] in order else None
        return {"task": task_type, "policy": policy, "candidates": candidates, "order": order,
                "window_hours": 24, "sample_limit": 100}

    def _prepare_routes(self, mode, task_type, context, preserve_order=False):
        if preserve_order:
            configured = self.modes.get('thinking', []) if mode == 'thinking' else []
            if not isinstance(configured, list):
                configured = []
        else:
            configured = self.get_routes(mode, task_type)
        if not hasattr(self, "metrics"):
            if preserve_order:
                with self.lock:
                    cooldowns = dict(self.cooldowns)
                return [target for target in configured if cooldowns.get(target, 0) <= time.time()]
            return self.rank_routes(configured, task_type or mode)
        decision = self.explain_routes(configured, task_type or mode, preserve_order=preserve_order)
        context["decision"] = decision
        for row in decision["candidates"]:
            if row["target"] not in decision["order"]:
                self._log_attempt(row["target"], context, "skipped", time.time(), code=row["skip_reason"] or "cooldown")
        return decision["order"]

    def _provider_block(self, provider):
        return self.metrics.provider_status(provider) if hasattr(self, "metrics") else None

    def _record_metrics(self, target, context, success, start, result=None, eligible=True):
        if not hasattr(self, "metrics"): return
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        output = usage.get("completion_tokens") if isinstance(usage, dict) else None
        values = {"duration": round(time.time() - start, 3), "ttft": context.get("ttft"),
                  "output_tokens": output if output is not None else context.get("output_tokens")}
        context["metrics"] = values
        try:
            self.metrics.record(target, context.get("mode", "balanced"), success, eligible=eligible, **values)
        except (OSError, ValueError, sqlite3.Error):
            pass

    def _record_failure(self, target, context, start, error):
        category = failure_category(error)
        context["failure_category"] = category
        eligible = category == "reliability"
        if eligible:
            self.state.record_failure(target)
            self.mark_failure(target)
        else:
            self.state.record_failure(target, eligible=False)
        if not eligible and category in ("provider_auth", "rate_limited") and hasattr(self, "metrics"):
            try:
                self.metrics.block(target.split(":",1)[0], category, getattr(error, "retry_after", None) or (900 if category == "provider_auth" else 60))
            except (OSError, ValueError, sqlite3.Error):
                pass
        self._record_metrics(target, context, False, start, eligible=eligible)

    def capability_score(self, cap, task_type):
        if task_type in ("fast", "balanced", "writing") and task_type in cap:
            return cap[task_type]

        weights = {
            "code": (("coding", 0.6), ("thinking", 0.3), ("agent", 0.1)),
            "thinking": (("thinking", 0.7), ("agent", 0.2), ("coding", 0.1)),
            "agent": (("agent", 0.6), ("thinking", 0.3), ("coding", 0.1)),
        }.get(
            task_type,
            (("agent", 0.35), ("thinking", 0.35), ("coding", 0.30)),
        )

        return sum(cap.get(field, 0) * weight for field, weight in weights)

    # ---------- 失败/成功记账 ----------

    def mark_failure(self, target):
        with self.lock:
            count = self.fail_counts.get(target, 0) + 1
            self.fail_counts[target] = count

            step = COOLDOWN_STEPS[min(count, len(COOLDOWN_STEPS)) - 1]
            self.cooldowns[target] = time.time() + step

            self.save_cooldowns(locked=True)

    def mark_success(self, target):
        """成功即清账。

        免费 provider 偶发 429 是常态，不清零的话跑几天所有模型都会
        停在 1800 秒冷却档。
        """

        with self.lock:
            # 两个 pop 都必须执行：写成 `a() or b()` 会因短路跳过 b()，
            # 而 mark_failure 是同时写这两个结构的，冷却将永远清不掉。
            had_fails = self.fail_counts.pop(target, None) is not None
            had_cooldown = self.cooldowns.pop(target, None) is not None

            if had_fails or had_cooldown:
                self.save_cooldowns(locked=True)

    # ---------- 调用 ----------

    def _log_context(self, mode, messages, params, streaming, task_type=None, request_body=None):
        params = params or {}
        return {"source": "business", "request_id": uuid.uuid4().hex,
                "request": request_body if request_body is not None else dict(params, model=mode, task_type=task_type, messages=messages, stream=streaming),
                "mode": task_type or mode, "stream": streaming, "input_messages": len(messages),
                "input_chars": sum(len(m.get("content", "")) for m in messages
                                   if isinstance(m, dict) and isinstance(m.get("content"), str)),
                "tool_count": len(params.get("tools", [])) if isinstance(params.get("tools", []), list) else 0,
                "max_tokens": params.get("max_completion_tokens", params.get("max_tokens"))}

    def _log_attempt(self, target, context, outcome, start, error=None, code=None):
        log = getattr(self, "model_log", None)
        if log is None:
            return
        try:
            fields = dict(context or {"source": "business"})
            if outcome in ("success", "cancelled"):
                fields.pop("next_target", None)
            fields.update(outcome=outcome, duration=time.time() - start,
                          code=code or (context.get("failure_category") if context.get("failure_category") != "reliability" else None) or (failure_code(error) if error else None),
                          status=getattr(error, "status", None))
            trace = fields.pop("_trace", None)
            if trace is not None:
                if error is not None:
                    trace.data["error"] = {"type": type(error).__name__, "message": str(error)}
                fields["details"] = trace.snapshot()
            else:
                fields["details"] = {"request": fields.get("request"), "attempts": []}
            log.write(target, **fields)
        except Exception:
            # 日志不可写不应改变请求结果、统计或降级链。
            pass

    def _attempt(self, target, task_type, call, context=None, response_validator=None):
        """在单个模型上执行 call，返回 (结果, 错误项)。"""

        provider_name, model = self.parse_target(target)
        context = dict(context or {})
        trace = CallTrace(context.get("request"))
        context["_trace"] = trace
        start = time.time()

        blocked = self._provider_block(provider_name)
        if blocked:
            self._log_attempt(target, context, "skipped", start, code=blocked["reason"])
            return None, {"provider": provider_name, "reason": blocked["reason"]}
        if self.state.is_disabled(target):
            self._log_attempt(target, context, "skipped", start, code="disabled_pending_recovery")
            return None, {"model": target, "reason": "disabled_pending_recovery"}

        with self.quota.lock:
            if not self.quota.allowed(provider_name):
                self._log_attempt(target, context, "skipped", start, code="quota_exceeded")
                return None, {"provider": provider_name, "reason": "quota_exceeded"}
            self.quota.record(provider_name)

        start = time.time()

        try:
            provider = get_provider(provider_name)
            with trace.bind():
                result = call(provider, model)
            trace.data["result"] = result
            tokens = usage_tokens(result)
            if tokens:
                self.quota.record_tokens_only(provider_name, tokens)
            if not response_has_output(result):
                raise ProviderError("%s: 响应没有有效内容" % target, retryable=True, code="empty_response")

            if response_validator is not None:
                response_validator(result)

            latency = round(time.time() - start, 3)

            self._record_metrics(target, context, True, start, result)
            self.state.record_success(target, latency, tokens=tokens)
            self.mark_success(target)
            self._log_attempt(target, context, "success", start)

            return {
                "success": True,
                "provider": provider_name,
                "model": model,
                "target": target,
                "latency": latency,
                "result": result,
            }, None

        except ProviderError as e:
            if e.tokens:
                self.quota.record_tokens_only(provider_name, e.tokens)
            self._record_failure(target, context, start, e)
            self._log_attempt(target, context, "failed", start, error=e)

            return None, {
                "model": target,
                "status": e.status,
                "error": str(e)[:300],
            }

        except Exception as e:
            self._record_failure(target, context, start, e)
            self._log_attempt(target, context, "failed", start, error=e)

            return None, {
                "model": target,
                "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            }

    def chat(self, mode, messages, task_type=None, params=None, request_body=None, response_validator=None, preserve_order=False):
        errors = []
        deadline = time.monotonic() + 100 if preserve_order else None

        def call(provider, model):
            if not preserve_order:
                return provider.chat(model, messages, params=params)
            # Registry instances are shared with business traffic. Only this shallow
            # copy gets a shorter budget; sessions, tracing and usage stay shared.
            provider = copy.copy(provider)
            provider.max_retries = 0
            provider.connect_timeout = 3
            return provider.chat(model, messages, params=params,
                                 timeout=max(1, min(25, deadline - time.monotonic())))

        context = self._log_context(mode, messages, params, False, task_type, request_body)
        routes = self._prepare_routes(mode, task_type, context, preserve_order=preserve_order)
        for index, target in enumerate(routes):
            if deadline is not None and time.monotonic() >= deadline:
                errors.append({"reason": "request_budget"})
                break
            context["next_target"] = routes[index + 1] if index + 1 < len(routes) else None
            result, error = self._attempt(
                target,
                task_type,
                call,
                context=context,
                response_validator=response_validator,
            )

            if result:
                result["errors"] = errors
                return result

            errors.append(error)

        return {"success": False, "errors": errors}

    def stream(self, mode, messages, task_type=None, params=None, request_body=None, response_adapter=None):
        """实质内容到达前允许降级；完整消费成功后才记成功。

        元信息 success 表示已选中可输出的流，最终统计由返回的生成器完成。
        已输出实质内容后的异常直接向调用方抛出，不得混入另一模型。
        """
        errors = []

        context = self._log_context(mode, messages, params, True, task_type, request_body)
        routes = self._prepare_routes(mode, task_type, context)
        for index, target in enumerate(routes):
            context["next_target"] = routes[index + 1] if index + 1 < len(routes) else None
            start = time.time()
            provider_name, model = self.parse_target(target)
            trace = CallTrace(context.get("request"))
            context = dict(context, _trace=trace)
            for field in ("failure_category", "metrics", "ttft", "output_tokens"):
                context.pop(field, None)
            blocked = self._provider_block(provider_name)
            if blocked:
                self._log_attempt(target, context, "skipped", start, code=blocked["reason"])
                errors.append({"provider": provider_name, "reason": blocked["reason"]})
                continue
            if self.state.is_disabled(target):
                self._log_attempt(target, context, "skipped", start, code="disabled_pending_recovery")
                errors.append({"model": target, "reason": "disabled_pending_recovery"})
                continue
            with self.quota.lock:
                if not self.quota.allowed(provider_name):
                    self._log_attempt(target, context, "skipped", start, code="quota_exceeded")
                    errors.append({"provider": provider_name, "reason": "quota_exceeded"})
                    continue
                self.quota.record(provider_name)

            start = time.time()
            iterator = None
            buffered = []
            tokens = 0
            try:
                provider = get_provider(provider_name)
                iterator = trace.wrap(iter(provider.stream(model, messages, params=params)))
                for chunk in iterator:
                    if not trace.data["attempts"]:
                        trace.data.setdefault("_fallback_stream", []).append("data: " + chunk + "\n")
                    if chunk == "[DONE]":
                        break
                    parsed = self._parse_stream_chunk(chunk)
                    tokens = max(tokens, usage_tokens(parsed))
                    usage = parsed.get("usage")
                    if isinstance(usage, dict) and type(usage.get("completion_tokens")) is int:
                        context["output_tokens"] = usage["completion_tokens"]
                    buffered.append(chunk)
                    if response_has_output(parsed, streaming=True):
                        break
                else:
                    raise ProviderError("%s: 流式响应没有有效内容" % target, retryable=True, code="empty_response")

                if not buffered or not response_has_output(parsed, streaming=True):
                    raise ProviderError("%s: 流式响应没有有效内容" % target, retryable=True, code="empty_response")

            except Exception as error:
                self._close_stream(iterator)
                if tokens:
                    self.quota.record_tokens_only(provider_name, tokens)
                self._record_failure(target, context, start, error)
                self._log_attempt(target, context, "failed", start, error=error)
                detail = {"model": target, "error": str(error)[:300]}
                if isinstance(error, ProviderError):
                    detail["status"] = error.status
                errors.append(detail)
                continue

            latency = round(time.time() - start, 3)
            meta = {
                "success": True,
                "provider": provider_name,
                "model": model,
                "target": target,
                "latency": latency,
                "errors": errors,
            }

            context = dict(context, next_target=None, ttft=latency)

            def generate():
                nonlocal tokens
                try:
                    # 由下方预启动并消费此内部哨兵，保证首次消费前 close()
                    # 也会执行 finally，及时归还已经打开的上游连接。
                    yield None
                    def emit(chunk):
                        if response_adapter is None:
                            yield chunk
                        else:
                            for event in response_adapter.feed(self._parse_stream_chunk(chunk)):
                                yield json.dumps(event, ensure_ascii=False)
                    for chunk in buffered:
                        yield from emit(chunk)
                    for chunk in iterator:
                        if not trace.data["attempts"]:
                            trace.data.setdefault("_fallback_stream", []).append("data: " + chunk + "\n")
                        if chunk == "[DONE]":
                            break
                        parsed = self._parse_stream_chunk(chunk)
                        tokens = max(tokens, usage_tokens(parsed))
                        usage = parsed.get("usage")
                        if isinstance(usage, dict) and type(usage.get("completion_tokens")) is int:
                            context["output_tokens"] = usage["completion_tokens"]
                        yield from emit(chunk)
                    if response_adapter is not None:
                        for event in response_adapter.finish():
                            yield json.dumps(event, ensure_ascii=False)
                except GeneratorExit:
                    self._log_attempt(target, context, "cancelled", start, code="client_closed")
                    raise
                except Exception as error:
                    self._record_failure(target, context, start, error)
                    self._log_attempt(target, context, "failed", start, error=error)
                    raise
                else:
                    self._record_metrics(target, context, True, start)
                    self.state.record_success(target, latency, tokens=tokens)
                    self.mark_success(target)
                    self._log_attempt(target, context, "success", start)
                finally:
                    self._close_stream(iterator)
                    if tokens:
                        self.quota.record_tokens_only(provider_name, tokens)

            stream = generate()
            next(stream)
            return meta, stream

        return {"success": False, "errors": errors}, None

    @staticmethod
    def _parse_stream_chunk(chunk):
        try:
            parsed = json.loads(chunk)
        except (ValueError, TypeError):
            raise ProviderError("上游流式响应不是合法 JSON", retryable=True, code="invalid_json")
        if not isinstance(parsed, dict) or parsed.get("error"):
            error = parsed.get("error") if isinstance(parsed, dict) else None
            status = error.get("code") if isinstance(error, dict) else None
            raise ProviderError("上游返回流式错误", status=status if type(status) is int and 400 <= status <= 599 else None, retryable=True, code="stream_error")
        return parsed

    @staticmethod
    def _close_stream(iterator):
        closer = getattr(iterator, "close", None)
        if closer:
            try:
                closer()
            except Exception:
                pass
