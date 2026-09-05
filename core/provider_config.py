"""Validation for persisted OpenAI-compatible provider definitions (never stores keys)."""
import re
from urllib.parse import urlsplit

SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_VAR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def validate_free_models(models):
    if not isinstance(models, list) or len(models) > 1000:
        raise ValueError("free_models 必须是最多 1000 项的数组")
    if any(not isinstance(m, str) or not m or len(m) > 256 or any(c.isspace() or ord(c) < 32 for c in m) for m in models):
        raise ValueError("免费模型 ID 不能为空、含空白或超过 256 字符")
    return list(dict.fromkeys(models))


def validate_definition(name, cfg):
    if not isinstance(name, str) or not SLUG.fullmatch(name):
        raise ValueError("Provider 名称须为小写字母开头的 1–32 位字母、数字、_ 或 -")
    if not isinstance(cfg, dict) or cfg.get("type") != "openai":
        raise ValueError("自定义 Provider 的 type 必须是 openai")
    url = cfg.get("base_url")
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("base_url 必须是有效 HTTPS 地址")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("base_url 地址或端口无效")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local and cfg.get("allow_local_http") is True)) or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or (port is not None and not 0 < port < 65536):
        raise ValueError("base_url 必须使用 HTTPS、无凭据/查询参数；仅明确勾选时允许 localhost HTTP 测试")
    if not isinstance(cfg.get("env_var"), str) or not ENV_VAR.fullmatch(cfg["env_var"]):
        raise ValueError("env_var 须为大写字母开头的大写字母、数字或下划线")
    env = cfg.get("env")
    if not isinstance(env, str) or not env or any(ord(c) < 32 for c in env):
        raise ValueError("自定义 Provider 缺少有效密钥文件路径")
    return dict(cfg, base_url=url.rstrip("/"), free_models=validate_free_models(cfg.get("free_models", [])))
