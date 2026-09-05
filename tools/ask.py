#!/usr/bin/env python3
"""用本网关当前能用的最好的模型问一句话。

    python3 tools/ask.py "问题"
    echo "问题" | python3 tools/ask.py -
    python3 tools/ask.py --list

维护技能（.claude/skills/gateway-maintain）用它把调研工作交给网关自己接的免费模型，
从而不额外消耗别处的额度。只依赖标准库；能读到 config.yaml + data/capability.json 时
按 router 同款权重挑模式，读不到就退回内置偏好序。

密钥从 GATEWAY_KEY 环境变量读，不要写在命令行上（会进 shell 历史）。
任何输出都不打印密钥。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("GATEWAY_DIR") or os.path.dirname(HERE)

DEFAULT_BASE = "http://127.0.0.1:8090"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TIMEOUT = 180

# router.capability_score 的同款权重，改那边记得同步这里
WEIGHTS = {
    "code": (("coding", 0.6), ("thinking", 0.3), ("agent", 0.1)),
    "thinking": (("thinking", 0.7), ("agent", 0.2), ("coding", 0.1)),
    "agent": (("agent", 0.6), ("thinking", 0.3), ("coding", 0.1)),
}
DEFAULT_WEIGHTS = (("agent", 0.35), ("thinking", 0.35), ("coding", 0.30))

# 读不到本地配置时的兜底顺序（越靠前越优先），按 task 分别给一份
FALLBACK_ORDER = {
    "thinking": ("thinking", "agent", "balanced", "code", "writing", "fast"),
    "code": ("code", "thinking", "agent", "balanced", "fast", "writing"),
    "agent": ("agent", "thinking", "balanced", "code", "writing", "fast"),
    "balanced": ("balanced", "agent", "thinking", "code", "writing", "fast"),
}


def die(msg, code=1):
    print("ask: %s" % msg, file=sys.stderr)
    sys.exit(code)


# ---------- 本地配置：给每个模式算一个「链上最强模型」的分 ----------

def load_local():
    """返回 (modes, capability)；任一读不到就返回 (None, None)，调用方退回偏好序。"""

    try:
        import yaml
    except ImportError:
        return None, None

    try:
        with open(os.path.join(BASE_DIR, "config.yaml"), "r") as f:
            config = yaml.safe_load(f) or {}

        with open(os.path.join(BASE_DIR, "data", "capability.json"), "r") as f:
            capability = (json.load(f) or {}).get("models", {})
    except (OSError, ValueError, yaml.YAMLError):
        return None, None

    modes = (config.get("gateway") or {}).get("modes")

    if not isinstance(modes, dict):
        return None, None

    return modes, capability


def flatten(modes):
    """{模式名: 降级链}；task 子表里的模式提到顶层，和网关 /v1/models 的口径一致。"""

    out = {}

    for name, value in (modes or {}).items():
        if name == "task" and isinstance(value, dict):
            for task_name, chain in value.items():
                if isinstance(chain, list):
                    out[task_name] = chain
        elif isinstance(value, list):
            out[name] = value

    return out


def score_model(cap, task):
    weights = WEIGHTS.get(task, DEFAULT_WEIGHTS)
    return sum(cap.get(field, 0) * weight for field, weight in weights)


def rank_modes(task):
    """按链上最强模型给模式排序，返回 [(模式, 分数, 领头模型)]；读不到配置返回 []。"""

    modes, capability = load_local()

    if modes is None:
        return []

    ranked = []

    for name, chain in flatten(modes).items():
        best = None

        for target in chain:
            cap = capability.get(target)

            if not isinstance(cap, dict):
                continue

            point = score_model(cap, task)

            if best is None or point > best[0]:
                best = (point, target)

        if best is not None:
            ranked.append((name, round(best[0], 1), best[1]))

    # 同分时（多个模式共用同一个领头模型）优先取名字和 task 一致的，再按兜底偏好序
    order = FALLBACK_ORDER.get(task, FALLBACK_ORDER["thinking"])

    def key(item):
        name, point, _ = item
        seat = order.index(name) if name in order else len(order)
        return (-point, 0 if name == task else 1, seat)

    ranked.sort(key=key)

    return ranked


# ---------- HTTP ----------

def request(base, path, key, payload=None, timeout=DEFAULT_TIMEOUT):
    url = base.rstrip("/") + path
    data = None
    headers = {"Authorization": "Bearer %s" % key, "Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]

        if e.code == 401:
            die("网关拒绝了密钥（401）。检查 GATEWAY_KEY 是否为有效的 nvx- 客户端密钥")

        die("HTTP %s %s：%s" % (e.code, path, body))
    except urllib.error.URLError as e:
        die("连不上 %s：%s" % (url, e.reason))
    except (ValueError, TimeoutError) as e:
        die("响应异常：%s" % e)


def available_modes(base, key):
    payload = request(base, "/v1/models", key, timeout=30)
    return [item.get("id") for item in (payload.get("data") or []) if item.get("id")]


def pick_mode(base, key, task):
    """先问网关有哪些模式可用，再在其中挑最强的。"""

    modes = available_modes(base, key)

    if not modes:
        die("网关没有报告任何可用模式，先查 config.yaml 的 gateway.modes")

    for name, _, _ in rank_modes(task):
        if name in modes:
            return name

    for name in FALLBACK_ORDER.get(task, FALLBACK_ORDER["thinking"]):
        if name in modes:
            return name

    return modes[0]


# ---------- 入口 ----------

def build_parser():
    p = argparse.ArgumentParser(
        prog="ask.py",
        description="用网关当前最强的模式问一句话（密钥读 GATEWAY_KEY，不接受写在命令行上）",
    )
    p.add_argument("prompt", nargs="?", help="提示词；写 - 表示从 stdin 读")
    p.add_argument("--base", default=os.environ.get("GATEWAY_URL") or DEFAULT_BASE,
                   help="网关地址，默认 $GATEWAY_URL 或 %s" % DEFAULT_BASE)
    p.add_argument("--mode", default="auto", help="指定模式名；auto（默认）= 自动挑最强")
    p.add_argument("--task", default="thinking",
                   choices=sorted(set(list(WEIGHTS) + ["balanced"])),
                   help="auto 挑模式时的侧重，默认 thinking")
    p.add_argument("--system", help="system 提示")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help="默认 %d；给推理型模型留够，否则可能只返回 reasoning" % DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--json", action="store_true", help="输出 JSON（mode/upstream/latency/content）")
    p.add_argument("--list", action="store_true", help="只列出可用模式与本地排名，不发请求")

    return p


def read_prompt(arg):
    if arg == "-" or arg is None:
        if sys.stdin.isatty():
            return None

        return sys.stdin.read().strip()

    return arg.strip()


def main(argv):
    args = build_parser().parse_args(argv)

    key = os.environ.get("GATEWAY_KEY", "").strip()

    if not key:
        die("没有设置 GATEWAY_KEY。用 manage.py keys create 建一把客户端密钥后 export，"
            "别把密钥写进命令行参数")

    if args.list:
        modes = available_modes(args.base, key)
        ranked = rank_modes(args.task)
        lines = ["网关可用模式：%s" % (", ".join(modes) or "(空)")]

        if ranked:
            lines.append("本地按 task=%s 的排名（分数 · 链上最强模型）：" % args.task)
            lines += ["  %-9s %6.1f  %s" % (n, s, m) for n, s, m in ranked if n in modes]
        else:
            lines.append("读不到本地 config.yaml / capability.json，auto 会退回内置偏好序：%s"
                         % ", ".join(FALLBACK_ORDER.get(args.task, ())))

        print("\n".join(lines))
        return 0

    prompt = read_prompt(args.prompt)

    if not prompt:
        die("没有提示词。传参数或用 - 从 stdin 读")

    mode = args.mode if args.mode != "auto" else pick_mode(args.base, key, args.task)

    messages = []

    if args.system:
        messages.append({"role": "system", "content": args.system})

    messages.append({"role": "user", "content": prompt})

    payload = {"model": mode, "messages": messages, "max_tokens": args.max_tokens}

    if args.temperature is not None:
        payload["temperature"] = args.temperature

    started = time.time()
    result = request(args.base, "/v1/chat/completions", key, payload, timeout=args.timeout)
    latency = round(time.time() - started, 2)

    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    note = None

    # 推理型上游会先把 token 花在 reasoning 上；max_tokens 给小了就只剩 reasoning
    if not content and message.get("reasoning"):
        note = "上游只返回了 reasoning（多半是 max_tokens 不够），下面是 reasoning 原文"
        content = str(message.get("reasoning")).strip()

    upstream = result.get("model") or "?"

    if args.json:
        print(json.dumps({
            "mode": mode,
            "upstream": upstream,
            "latency": latency,
            "finish_reason": choice.get("finish_reason"),
            "note": note,
            "content": content,
        }, ensure_ascii=False, indent=2))
    else:
        print("[模式 %s → 上游 %s · %ss]" % (mode, upstream, latency), file=sys.stderr)

        if note:
            print("[注意] %s" % note, file=sys.stderr)

        print(content)

    return 0 if content else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
