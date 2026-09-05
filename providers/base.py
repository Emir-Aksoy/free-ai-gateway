"""Provider 基类。

所有已接入的 provider 都是 OpenAI 兼容接口，差异只在 base_url 与密钥来源，
因此统一在这里实现：连接复用、重试退避、流式、参数透传、超时分离。
"""

import json
import os
import random
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter


class ProviderError(Exception):
    """provider 调用失败。

    retryable 表示同一 provider 上重试有意义（限流、瞬时 5xx、网络抖动），
    路由层据此决定是就地重试还是直接降级到下一个模型。
    """

    def __init__(self, message, status=None, retryable=False, tokens=0, code=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.tokens = tokens
        self.code = code


def usage_tokens(payload):
    """读取上游已消耗的 token；异常 usage 不应打断请求处理。"""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else 0


def _has_text(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(part, dict) and _has_text(part.get("text")) for part in value)
    return False


def _has_function_call(value, streaming):
    if not isinstance(value, dict):
        return False
    # 非流式必须有函数名；流式参数可独立出现在后续分块里。
    return _has_text(value.get("name")) or (streaming and _has_text(value.get("arguments")))


def response_has_output(payload, streaming=False):
    """只有最终文本或工具调用才是有效输出，reasoning/role/usage 不算。"""
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("delta" if streaming else "message")
        if not isinstance(message, dict):
            continue
        if _has_text(message.get("content")):
            return True
        if _has_function_call(message.get("function_call"), streaming):
            return True
        calls = message.get("tool_calls")
        if isinstance(calls, list) and any(
            isinstance(call, dict) and _has_function_call(call.get("function"), streaming)
            for call in calls
        ):
            return True
    return False


# 同一 provider 的请求共用一个 Session，复用 TLS 连接。
# 跨境握手每次 100-300ms，复用后只有首次付这个成本。
_sessions = {}
_sessions_lock = threading.Lock()


def get_session(name):
    with _sessions_lock:
        session = _sessions.get(name)

        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=4,
                pool_maxsize=16,
                max_retries=0,  # 重试由我们自己控制，好记录每次尝试
            )
            session.mount("https://", adapter)
            _sessions[name] = session

        return session


# 客户端传来的这些参数原样转发给上游，不再丢弃。
PASSTHROUGH_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "tools",
    "tool_choice",
    "response_format",
    "n",
    "logprobs",
    "top_logprobs",
    "reasoning_effort",
    "user",
)

# 这些状态码重试有意义；4xx 里除了 408/425/429 都是请求本身的问题，重试只是浪费额度。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 上游或中间代理偶尔会在错误页里回显请求头，别让密钥跟着进日志和客户端响应
BEARER_PATTERN = re.compile(
    r"(?i)bearer\s+(?=[A-Za-z0-9._~+/=\-]*[0-9_\-.])[A-Za-z0-9._~+/=\-]{12,}"
)


class BaseProvider:
    name = "base"
    base_url = None
    env_file = None
    env_var = None

    # 连接超时短、读取超时长：上游挂掉时快速失败，正常生成时给足时间。
    # 实测 gemini 503 时会挂住 53 秒，分离超时后这类故障 10 秒内就能降级。
    connect_timeout = 10
    read_timeout = 90

    max_retries = 2
    backoff_base = 0.5
    backoff_cap = 8.0

    def __init__(self, env_file=None):
        self.api_key = self.load_key(env_file or self.env_file)
        self.session = get_session(self.name)

    def load_key(self, env_file):
        if not env_file or not os.path.exists(env_file):
            raise ProviderError(
                "%s: 密钥文件不存在 %s" % (self.name, env_file)
            )

        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()

                if line.startswith(self.env_var + "="):
                    value = line.split("=", 1)[1].strip().strip("'\"")

                    if value:
                        return value

        raise ProviderError(
            "%s: %s 未在 %s 中找到" % (self.name, self.env_var, env_file)
        )

    def headers(self):
        return {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "free-ai-gateway/2.0",
        }

    def endpoint(self):
        return self.base_url.rstrip("/") + "/chat/completions"

    def build_payload(self, model, messages, params=None, stream=False):
        payload = {"model": model, "messages": messages}

        for key in PASSTHROUGH_PARAMS:
            if params and params.get(key) is not None:
                payload[key] = params[key]

        if stream:
            payload["stream"] = True
            # 拿到 usage 才能做 token 计量，多数 OpenAI 兼容端点支持该选项，
            # 不支持的会忽略它。
            payload["stream_options"] = {"include_usage": True}

        return payload

    def sleep_before_retry(self, attempt, retry_after=None):
        """指数退避 + 抖动。上游给了 Retry-After 就听它的。"""

        if retry_after:
            try:
                delay = min(float(retry_after), self.backoff_cap)
                time.sleep(delay)
                return delay
            except (TypeError, ValueError):
                pass

        delay = min(self.backoff_base * (2 ** attempt), self.backoff_cap)
        delay = delay * (0.5 + random.random() * 0.5)  # 抖动，避免多路由同时重试
        time.sleep(delay)
        return delay

    def _redact(self, text):
        if self.api_key:
            text = text.replace(self.api_key, "****")

        return BEARER_PATTERN.sub("Bearer ****", text)

    def _raise_for_response(self, response):
        if response.status_code < 400:
            return

        # 窗口留足 400 字输出 + 最长密钥，先脱敏再截断；不对超大错误体做整体正则
        body = self._redact(response.text[:2048])[:400]
        raise ProviderError(
            "%s HTTP %d: %s" % (self.name, response.status_code, body),
            status=response.status_code,
            code="http_error",
            retryable=response.status_code in RETRYABLE_STATUS,
        )

    def _request(self, payload, stream, timeout=None):
        read_timeout = timeout or self.read_timeout
        last_error = None
        response = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    self.endpoint(),
                    json=payload,
                    headers=self.headers(),
                    timeout=(self.connect_timeout, read_timeout),
                    stream=stream,
                )

                self._raise_for_response(response)
                return response

            except ProviderError as e:
                last_error = e

                if not e.retryable or attempt >= self.max_retries:
                    raise

                retry_after = None

                if e.status == 429 and response is not None:
                    retry_after = response.headers.get("Retry-After")

                # 上一次的错误响应不再需要，显式归还连接，别等 GC
                if response is not None:
                    response.close()

                self.sleep_before_retry(attempt, retry_after)

            except requests.exceptions.RequestException as e:
                last_error = ProviderError(
                    "%s 网络错误: %s" % (self.name, self._redact(str(e))[:200]),
                    retryable=True,
                    code="timeout" if isinstance(e, requests.exceptions.Timeout) else "network_error",
                )

                if attempt >= self.max_retries:
                    raise last_error

                self.sleep_before_retry(attempt)

        raise last_error

    def chat(self, model, messages, params=None, timeout=None):
        """非流式调用，返回完整的 OpenAI 格式响应。"""

        payload = self.build_payload(model, messages, params, stream=False)
        response = self._request(payload, stream=False, timeout=timeout)

        try:
            result = response.json()
            if not response_has_output(result):
                raise ProviderError(
                    "%s: 响应没有有效内容" % self.name,
                    retryable=True,
                    tokens=usage_tokens(result),
                    code="empty_response",
                )
            return result
        except ValueError:
            raise ProviderError(
                "%s: 响应不是合法 JSON: %s"
                % (self.name, self._redact(response.text[:2048])[:200]),
                code="invalid_json",
            )
        finally:
            response.close()

    def stream(self, model, messages, params=None, timeout=None):
        """流式调用，逐个 yield SSE 数据行（已剥掉 'data: ' 前缀的 JSON 字符串）。

        末尾的 [DONE] 不在这里产出，由调用方决定如何收尾。
        """

        payload = self.build_payload(model, messages, params, stream=True)
        response = self._request(payload, stream=True, timeout=timeout)
        finished_choices = set()
        expected_choices = payload.get("n", 1)
        if not isinstance(expected_choices, int) or expected_choices < 1:
            expected_choices = 1

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue

                line = raw_line.strip()

                if not line.startswith("data:"):
                    continue

                chunk = line[5:].strip()

                if chunk == "[DONE]":
                    return

                if chunk:
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        parsed = None
                    choices = parsed.get("choices") if isinstance(parsed, dict) else None
                    if isinstance(choices, list):
                        for choice in choices:
                            if not isinstance(choice, dict):
                                continue
                            reason = choice.get("finish_reason")
                            index = choice.get("index", 0)
                            if (
                                isinstance(reason, str) and reason
                                and isinstance(index, int) and 0 <= index < expected_choices
                            ):
                                finished_choices.add(index)
                    yield chunk

            # 有些兼容端点只发 finish_reason 而没有 [DONE]，允许这种正常结束。
            # 没有任何完整结束标记的 EOF 可能是代理截断，不能补成成功。
            if len(finished_choices) < expected_choices:
                raise ProviderError("%s: 流式响应在结束标记前中断" % self.name, retryable=True, code="stream_incomplete")
        except requests.exceptions.RequestException as error:
            raise ProviderError(
                "%s 流式网络错误: %s" % (self.name, self._redact(str(error))[:200]),
                retryable=True,
                code="timeout" if isinstance(error, requests.exceptions.Timeout) else "network_error",
            ) from None
        finally:
            response.close()
