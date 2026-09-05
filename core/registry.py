"""provider 实例注册表。

实例内部持有连接池，需要全局复用；转多线程后加锁避免重复创建。
密钥文件的位置可由 config.yaml 的 providers.<name>.env 指定，
让同一台机器上的多个实例各用各的密钥文件。
"""

import threading
import os

from providers.base import BaseProvider
from core.provider_config import validate_definition, validate_free_models

from providers.groq import GroqProvider
from providers.gemini import GeminiProvider
from providers.opencode import OpenCodeProvider
from providers.agnes import AgnesProvider
from providers.openrouter import OpenRouterProvider

PROVIDERS = {
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "opencode": OpenCodeProvider,
    "agnes": AgnesProvider,
    "openrouter": OpenRouterProvider,
}

BUILTIN_PROVIDERS = dict(PROVIDERS)
_definitions = {}

_instances = {}
_env_files = {}
_lock = threading.Lock()


def configure_env_files(mapping):
    """由路由层读完 config.yaml 后调用。没指定的 provider 沿用类里的默认路径。"""

    with _lock:
        _env_files.clear()

        for name, path in (mapping or {}).items():
            if path:
                _env_files[name] = str(path)


def env_file_for(name):
    cls = PROVIDERS.get(name)
    return _env_files.get(name) or (cls.env_file if cls else None)


def get_provider(name):
    instance = _instances.get(name)

    if instance is not None:
        return instance

    with _lock:
        # 双重检查：等锁期间可能已被别的线程建好
        instance = _instances.get(name)

        if instance is not None:
            return instance

        if name not in PROVIDERS:
            raise ValueError("未知的 provider: %s" % name)

        instance = PROVIDERS[name](env_file=_env_files.get(name))
        _instances[name] = instance

        return instance


def provider_names():
    return sorted(PROVIDERS.keys())


def configure_providers(mapping):
    """Load custom providers and per-instance key paths from config.yaml."""
    mapping = mapping or {}
    if not isinstance(mapping, dict):
        raise ValueError("providers 必须是对象")
    from core.paths import BASE_DIR
    custom = {}
    env_files = {}
    for name, raw in mapping.items():
        cfg = raw or {}
        if not isinstance(cfg, dict):
            raise ValueError("Provider 配置必须是对象")
        if name in BUILTIN_PROVIDERS:
            if any(k in cfg for k in ("type", "base_url", "env_var")):
                raise ValueError("不能覆盖内置 Provider: %s" % name)
        else:
            cfg = validate_definition(name, cfg)
            custom[name] = cfg
        if "free_models" in cfg:
            validate_free_models(cfg["free_models"])
        if cfg.get("env"):
            path = cfg["env"]
            env_files[name] = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    with _lock:
        old = dict(_definitions)
        for name in list(PROVIDERS):
            if name not in BUILTIN_PROVIDERS and name not in custom:
                PROVIDERS.pop(name)
                _instances.pop(name, None)
        for name, cfg in custom.items():
            if old.get(name) != cfg:
                PROVIDERS[name] = type("CustomProvider_" + name, (BaseProvider,), {
                    "name": name, "base_url": cfg["base_url"],
                    "env_var": cfg["env_var"], "env_file": env_files.get(name),
                })
                _instances.pop(name, None)
        for name in list(_instances):
            if _env_files.get(name) != env_files.get(name):
                _instances.pop(name, None)
        _definitions.clear()
        _definitions.update(custom)
        _definitions.update({name: dict(cfg or {}) for name, cfg in mapping.items() if name in BUILTIN_PROVIDERS})
        _env_files.clear()
        _env_files.update(env_files)


def provider_definition(name):
    return dict(_definitions.get(name, {}))
