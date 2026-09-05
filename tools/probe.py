"""探测 provider 此刻真实可用的对话模型，以及验证密钥。

manage.py 的 models test / models scan / providers test / providers set 都走这里。
每次只发一个短请求（max_tokens=256，给推理模型留出回答预算），不经过网关的重试与降级逻辑，
反映的是"现在能不能用"，而不是"多试几次能不能用"。

返回的每个字典都不含密钥：上游错误体先经 scrub() 抹掉密钥值与 Bearer 令牌。
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from core.registry import PROVIDERS, env_file_for, provider_definition
from decimal import Decimal, InvalidOperation
from providers.base import response_has_output

# 非对话模型一律跳过：语音、向量、审核、图像、视频、实时等
NON_CHAT = re.compile(
    r"whisper|embedding|tts|orpheus|guard|safety|moderation|rerank|"
    r"image|video|live|transcribe|computer-use|robotics|audio|translate",
    re.I,
)

BEARER_PATTERN = re.compile(
    r"(?i)bearer\s+(?=[A-Za-z0-9._~+/=\-]*[0-9_\-.])[A-Za-z0-9._~+/=\-]{12,}"
)

USER_AGENT = "free-ai-gateway/2.0 probe"
CONNECT_TIMEOUT = 10
LIST_TIMEOUT = 30
TEST_TIMEOUT = 75      # 单个模型实测的读超时
SCAN_TIMEOUT = 40      # 扫描时给每个模型的读超时：短探测请求的读取超时
BUDGET = 240           # 一次 test/scan 的总时间预算（秒），超出的模型标记为未测
MIN_SLICE = 3          # 预算剩不到这么多秒就不再发起新请求


class ProbeError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def scrub(text, key=None):
    """抹掉密钥值与 Bearer 令牌。"""

    if not isinstance(text, str):
        return text

    if key:
        text = text.replace(key, "****")

    return BEARER_PATTERN.sub("Bearer ****", text)


def provider_class(name):
    cls = PROVIDERS.get(name)

    if cls is None:
        raise ProbeError("未知的 provider: %s" % name)

    return cls


def read_env_value(path, var):
    """读 VAR=value 形式的一行；文件或变量不存在返回 None。"""

    if not path:
        return None

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()

                if line.startswith(var + "="):
                    return line.split("=", 1)[1].strip().strip("'\"") or None
    except OSError:
        return None

    return None


def current_key(name):
    cls = provider_class(name)
    return read_env_value(env_file_for(name), cls.env_var)


def _headers(key):
    return {
        "Authorization": "Bearer %s" % key,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _url(cls, path):
    return cls.base_url.rstrip("/") + path


def classify(status, body):
    """把失败归成几类，扫描结果里比裸状态码好读。鉴权类先判，免得错误文本里的额度字样干扰。"""

    text = (body or "")[:300].lower()

    if status in (401, 403):
        return "鉴权失败"

    if status == 429:
        return "限流"

    if status == 402 or "credit" in text or "insufficient_quota" in text:
        return "需付费"

    if status == 503:
        return "过载"

    if status == 404:
        return "已下线"

    if status == 400:
        return "接口不兼容"

    if status >= 500:
        return "上游错误"

    return "HTTP %d" % status


def _empty_result(name, model):
    return {
        "target": "%s:%s" % (name, model),
        "provider": name,
        "model": model,
        "ok": False,
        "latency": None,
        "status": None,
        "reason": None,
        "error": None,
    }


def list_models(name, key=None, timeout=LIST_TIMEOUT):
    """拉 /models 并过滤掉非对话模型；openrouter 只保留 :free。"""

    cls = provider_class(name)
    key = key or current_key(name)

    if not key:
        raise ProbeError("%s 未配置密钥" % name)

    try:
        response = requests.get(
            _url(cls, "/models"),
            headers=_headers(key),
            timeout=(CONNECT_TIMEOUT, timeout),
        )
    except requests.exceptions.RequestException as e:
        raise ProbeError("网络错误: %s" % scrub(str(e), key)[:200])

    if response.status_code >= 400:
        raise ProbeError(
            "HTTP %d: %s" % (response.status_code, scrub(response.text[:2048], key)[:200]),
            status=response.status_code,
        )

    try:
        data = response.json().get("data") or []
    except (ValueError, AttributeError):
        raise ProbeError("/models 响应不是预期的 JSON")

    ids = [
        str(item.get("id", "")).replace("models/", "")
        for item in data
        if isinstance(item, dict)
    ]

    if name == "openrouter":
        ids = [i for i in ids if i.endswith(":free")]

    return sorted(set(i for i in ids if i and not NON_CHAT.search(i)))



def _free_evidence(name, model, item):
    """Zero prices or an explicit policy are evidence; HTTP success is not."""
    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        # Even partial non-zero metadata overrides a stale manual free declaration.
        for field in ("prompt", "completion", "input", "output", "input_price", "output_price", "request", "image", "web_search", "internal_reasoning"):
            if field in pricing:
                try:
                    price = Decimal(str(pricing[field]))
                    if not price.is_finite() or price != 0:
                        return False, "价格元数据包含收费项或无效价格"
                except (InvalidOperation, ValueError, TypeError):
                    return False, "价格元数据不完整或无效"
        pairs = (("prompt", "completion"), ("input", "output"), ("input_price", "output_price"))
        for a, b in pairs:
            if a in pricing and b in pricing:
                try:
                    prices = [Decimal(str(v)) for v in pricing.values()]
                    if all(v.is_finite() and v == 0 for v in prices):
                        return True, "价格元数据：输入、输出及附加项均为 0"
                    return False, "价格元数据包含收费项或无效价格"
                except (InvalidOperation, ValueError, TypeError):
                    return False, "价格元数据不完整或无效"
    definition = provider_definition(name)
    if model in definition.get("free_models", []):
        return True, "用户明确登记的免费模型"
    if name == "openrouter" and model.endswith(":free"):
        return True, "OpenRouter :free 模型策略"
    if name == "opencode" and model.endswith("-free"):
        return True, "OpenCode -free 模型策略"
    return False, "计费未知，自动跳过；确认免费后可登记"


def model_catalog(name, key=None, timeout=LIST_TIMEOUT):
    """Read catalog only. Never generates text or assumes successful means free."""
    cls = provider_class(name)
    key = key or current_key(name)
    if not key:
        raise ProbeError("%s 未配置密钥" % name)
    try:
        response = requests.get(_url(cls, "/models"), headers=_headers(key), timeout=(CONNECT_TIMEOUT, timeout))
    except requests.exceptions.RequestException as e:
        raise ProbeError("网络错误: %s" % scrub(str(e), key)[:200])
    if response.status_code >= 400:
        raise ProbeError("HTTP %d: %s" % (response.status_code, scrub(response.text[:2048], key)[:200]), status=response.status_code)
    try:
        data = response.json().get("data")
    except (ValueError, AttributeError):
        data = None
    if not isinstance(data, list):
        raise ProbeError("/models 响应不是预期的模型数组")
    result = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model = item["id"][7:] if item["id"].startswith("models/") else item["id"]
        if not model or NON_CHAT.search(model):
            continue
        free, basis = _free_evidence(name, model, item)
        result[model] = {"id": model, "free": free, "basis": basis, "listed": True}
    for model in provider_definition(name).get("free_models", []):
        if model not in result and not NON_CHAT.search(model):
            result[model] = {"id": model, "free": True, "basis": "用户明确登记；目录未列出", "listed": False}
    return sorted(result.values(), key=lambda item: item["id"])

def test_model(name, model, key=None, timeout=TEST_TIMEOUT, max_tokens=256):
    """对单个模型发一次最小请求。2xx 还要带有效文本或工具调用才算可用。"""

    cls = provider_class(name)
    key = key or current_key(name)
    result = _empty_result(name, model)

    if not key:
        result["reason"] = "未配置密钥"
        result["error"] = "%s 未配置密钥" % name
        return result

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": max_tokens,
    }

    start = time.time()

    try:
        response = requests.post(
            _url(cls, "/chat/completions"),
            json=payload,
            headers=_headers(key),
            timeout=(min(CONNECT_TIMEOUT, timeout), timeout),
        )
    except requests.exceptions.Timeout:
        result["latency"] = round(time.time() - start, 2)
        result["reason"] = "超时"
        result["error"] = "%d 秒内没有响应" % timeout
        return result
    except requests.exceptions.RequestException as e:
        result["latency"] = round(time.time() - start, 2)
        result["reason"] = "网络错误"
        result["error"] = scrub(str(e), key)[:200]
        return result

    result["latency"] = round(time.time() - start, 2)
    result["status"] = response.status_code

    if response.status_code >= 400:
        result["reason"] = classify(response.status_code, response.text)
        result["error"] = scrub(response.text[:2048], key)[:200]
        return result

    try:
        body = response.json()
    except ValueError:
        body = None

    choices = body.get("choices") if isinstance(body, dict) else None

    if (
        not isinstance(body, dict)
        or body.get("error")
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        result["reason"] = "接口不兼容"
        result["error"] = scrub(
            str(body.get("error")) if isinstance(body, dict) and body.get("error")
            else "响应不是 OpenAI 格式（choices 缺失或为空）",
            key,
        )[:200]
        return result

    if not response_has_output(body):
        result["reason"] = "空响应"
        result["error"] = "上游未返回有效正文或工具调用"
        return result

    result["ok"] = True
    return result


def test_key(name, key, timeout=LIST_TIMEOUT):
    """验证密钥能否通过鉴权。

    openrouter 的 /models 不需要鉴权，错密钥也能拿到列表，所以改查 /key。
    """

    cls = provider_class(name)
    out = {"ok": False, "latency": None, "model_count": None, "error": None}
    start = time.time()
    path = "/key" if name == "openrouter" else "/models"

    try:
        response = requests.get(
            _url(cls, path), headers=_headers(key), timeout=(CONNECT_TIMEOUT, timeout)
        )
    except requests.exceptions.RequestException as e:
        out["latency"] = round(time.time() - start, 2)
        out["error"] = "网络错误: %s" % scrub(str(e), key)[:200]
        return out

    out["latency"] = round(time.time() - start, 2)

    if response.status_code >= 400:
        out["error"] = "HTTP %d: %s" % (response.status_code, scrub(response.text[:2048], key)[:200])
        return out

    try:
        body = response.json()
    except ValueError:
        out["error"] = "%s 响应不是 JSON" % path
        return out

    data = body.get("data") if isinstance(body, dict) else None

    # 非 openrouter 的 /models 必须给模型数组；openrouter 的 /key 必须给密钥信息对象
    if name == "openrouter":
        if not isinstance(data, dict):
            out["error"] = "%s 响应不是预期结构" % path
            return out
    else:
        if not isinstance(data, list):
            out["error"] = "%s 响应不是预期结构" % path
            return out

        out["model_count"] = len(data)

    out["ok"] = True
    return out


def _over_budget(deadline):
    """剩余预算不足 MIN_SLICE 秒就不再发起新请求。用单调时钟，不受系统时间调整影响。"""

    return time.monotonic() + MIN_SLICE >= deadline


def _remaining_timeout(deadline, cap):
    """给下一个请求的读超时：不超过单模型上限，也不超过剩余预算。

    注意 requests 的读超时是"相邻两个字节的空闲时间"，上游持续慢速吐字节时
    单个请求仍可能略超剩余预算；这是 requests 的限制，桌面端另有 300 秒的总超时兜底。
    """

    return max(1.0, min(cap, deadline - time.monotonic()))


def test_targets(targets, workers=5, budget=BUDGET, per_model_timeout=TEST_TIMEOUT):
    """按 provider 分组：组内串行避免撞 RPM，组间并行。结果按输入顺序返回。

    总耗时受 budget 约束：预算用完后剩下的模型标记为"超出时间预算"而不是一直等。
    """

    deadline = time.monotonic() + budget
    groups = {}
    by_target = {}

    for target in targets:
        name, _, model = target.partition(":")

        if name not in PROVIDERS or not model:
            r = _empty_result(name, model)
            r["target"] = target
            r["reason"] = "配置错误"
            r["error"] = "未注册的 provider 或格式不是 provider:model"
            by_target[target] = r
            continue

        groups.setdefault(name, []).append(model)

    def run(name):
        results = []

        for model in groups[name]:
            if _over_budget(deadline):
                r = _empty_result(name, model)
                r["reason"] = "超出时间预算"
                r["error"] = "%d 秒预算已用完，未测" % budget
                results.append(r)
                continue

            results.append(
                test_model(name, model, timeout=_remaining_timeout(deadline, per_model_timeout))
            )

        return results

    if groups:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups)))) as ex:
            for results in ex.map(run, list(groups)):
                for r in results:
                    by_target[r["target"]] = r

    return [by_target[t] for t in targets]


def scan_free(providers=None, workers=5, budget=BUDGET, per_model_timeout=SCAN_TIMEOUT, targets=None):
    """扫描各 provider：先拉列表，再逐个实测。预算用完后剩下的模型进 skipped。"""

    deadline = time.monotonic() + budget
    names = list(providers or PROVIDERS)

    def run(name):
        entry = {"available": [], "unavailable": [], "skipped": [], "total": 0, "truncated": False, "error": None}

        try:
            catalog = model_catalog(name)
            if targets is not None:
                catalog = [item for item in catalog if "%s:%s" % (name, item["id"]) in targets]
            models = [item["id"] for item in catalog if item["free"]]
            entry["skipped"] = [item["id"] for item in catalog if not item["free"]]
            entry["excluded"] = [item for item in catalog if not item["free"]]
        except Exception as e:
            entry["error"] = scrub(str(e), current_key(name))[:200]
            return entry

        entry["total"] = len(catalog)

        for model in models:
            if _over_budget(deadline):
                entry["skipped"].append(model)
                entry["truncated"] = True
                continue

            r = test_model(name, model, timeout=_remaining_timeout(deadline, per_model_timeout))

            if r["ok"]:
                entry["available"].append({"model": model, "latency": r["latency"]})
            else:
                entry["unavailable"].append(
                    {
                        "model": model,
                        "reason": r["reason"],
                        "error": r["error"],
                        "latency": r["latency"],
                    }
                )

        return entry

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as ex:
        return dict(zip(names, ex.map(run, names)))
