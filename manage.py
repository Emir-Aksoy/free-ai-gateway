#!/usr/bin/env python3
"""ai-gateway 管理 CLI。

    python3 manage.py <命令> [子命令] [--json]

参数一律从 stdin 读一个 JSON 对象（没有就当 {}），结果以单个 JSON 对象写到 stdout：
成功 {"ok": true, ...}，失败 {"ok": false, "error": "...", "code": "..."} 且退出码非 0。
命令行上只有固定的命令名，不接受任何自由文本参数——桌面管理工具经 SSH 调用这里，
SSH 那一侧因此只需要一个固定的白名单。

命令一览：
    status                       服务状态、今日调用、各 key 用量、冷却、配额
    models list|test|scan        配置中的模型 / 实测可用性 / 扫描各 provider 的免费模型
    config get|set               读取 / 校验并写入降级链与 capability 评分（写入后重启）
    keys list|create|disable|enable|delete
    providers list|test|set      provider 密钥状态 / 验证 / 更换（只写不读）
    install check|run            新机器环境检查 / 写入密钥并启动

除 `install check` 外都依赖同目录下的 core/ 与 tools/；`install check` 只用标准库，
可以单独拷到新机器上运行。任何输出都不包含 provider 密钥；网关客户端密钥的完整值
只在 `keys create` 的响应里出现一次。
"""

import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_PORT = 8090
HEALTH_WAIT = 12.0
BACKUP_KEEP = 10

REQUIRED_MODES = ("fast", "balanced", "thinking")
REQUIRED_TASKS = ("code", "writing", "agent")
CAP_FIELDS = ("agent", "thinking", "coding")
OPTIONAL_CAP_FIELDS = ("fast", "balanced", "writing")
DEFAULT_ROUTING = {"mode": "scored", "use_latency": True, "use_success_rate": True}
MODE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# stdin 策略：不需要参数的命令根本不碰 stdin；参数可选的命令等不到就当 {}；
# 其余命令参数是必需的，等不到就明确报错，而不是悄悄用空参数跑下去
NO_INPUT = frozenset({"status", "models list", "config get", "keys list", "providers list"})
OPTIONAL_INPUT = frozenset({"models test", "models scan", "keys create", "install check"})
INPUT_WAIT = 10.0

# 任何输出经过这里：Bearer 令牌与网关客户端密钥的形态一律抹掉
SECRET_PATTERNS = (
    # 像令牌的 Bearer 值（≥12 位且含数字/_/-/.），"bearer authentication" 这类自然语言不算
    re.compile(r"(?i)bearer\s+(?=[A-Za-z0-9._~+/=\-]*[0-9_\-.])[A-Za-z0-9._~+/=\-]{12,}"),
    re.compile(r"nvx-[0-9a-f]{32}"),
)

# 成功响应里唯一允许原样输出完整密钥的字段（keys create / install run 各一次）
KEEP_FIELDS = {"keys create": ("key",), "install run": ("first_key",)}
INPUT_READ_LIMIT = 30.0          # 首字节到达后，整段 stdin 必须在这么多秒内读完（调用方要关 stdin）
INPUT_MAX_BYTES = 4 * 1024 * 1024


class ManageError(Exception):
    def __init__(self, message, code="error", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ====================================================================
# 通用工具
# ====================================================================

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def read_params(cmd):
    """按命令的 stdin 策略读一个 JSON 对象（见 NO_INPUT / OPTIONAL_INPUT）。交互式终端一律不读。

    首字节到达后也不无限等：整段输入必须在 INPUT_READ_LIMIT 秒内以 EOF 结束，
    调用方（桌面端 / ssh）写完参数就要关闭 stdin。
    """

    if cmd in NO_INPUT or sys.stdin.isatty():
        return {}

    try:
        ready, _, _ = select.select([sys.stdin], [], [], INPUT_WAIT)
    except (OSError, ValueError):
        ready = [sys.stdin]

    if not ready:
        if cmd in OPTIONAL_INPUT:
            return {}

        raise ManageError(
            "%.0f 秒内没有从 stdin 收到参数；该命令需要 JSON 参数（没有参数请传 {}）" % INPUT_WAIT,
            "input_timeout",
        )

    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        fd = None

    if fd is None:
        raw = sys.stdin.read()
    else:
        chunks = []
        total = 0
        deadline = time.monotonic() + INPUT_READ_LIMIT

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise ManageError(
                    "读取 stdin 超过 %.0f 秒仍未收到 EOF；调用方写完参数后必须关闭 stdin" % INPUT_READ_LIMIT,
                    "input_timeout",
                )

            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                ready = [fd]

            if not ready:
                continue

            chunk = os.read(fd, 65536)

            if not chunk:
                break

            total += len(chunk)

            if total > INPUT_MAX_BYTES:
                raise ManageError("stdin 超过 %d 字节" % INPUT_MAX_BYTES, "invalid_input")

            chunks.append(chunk)

        raw = b"".join(chunks).decode("utf-8", "replace")

    raw = raw.strip()

    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ManageError("stdin 不是合法 JSON: %s" % e, "invalid_input")

    if not isinstance(data, dict):
        raise ManageError("stdin 的 JSON 必须是对象", "invalid_input")

    return data


def emit(payload, exit_code=0):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def sh(args, timeout=60, env=None, cwd=None):
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def systemctl_show(name, props):
    rc, out, _ = sh(["systemctl", "show", name, "-p", ",".join(props)])
    result = {}

    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value

    return result


def service_is_active(name):
    rc, out, _ = sh(["systemctl", "is-active", name])
    return out == "active"


def service_env_file(name):
    """从 unit 的 EnvironmentFiles 找服务 env 文件；没有就按约定 /etc/<name>.env。"""

    raw = systemctl_show(name, ["EnvironmentFiles"]).get("EnvironmentFiles", "")
    path = raw.split(" ", 1)[0].strip() if raw else ""

    if path.startswith("-"):
        path = path[1:]

    return path or "/etc/%s.env" % name


def read_env_file(path):
    env = {}

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()

                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass

    return env


def write_env_var(path, var, value, mode=0o600):
    """替换或追加 VAR=value，其他行原样保留；临时文件 + 原子替换。"""

    lines = []

    try:
        with open(path, "r") as f:
            lines = f.read().splitlines()
    except OSError:
        pass

    out = []
    done = False

    for line in lines:
        if line.strip().startswith(var + "="):
            if not done:
                out.append("%s=%s" % (var, value))
                done = True
            continue

        out.append(line)

    if not done:
        out.append("%s=%s" % (var, value))

    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp = "%s.tmp.%d" % (path, os.getpid())

    with open_private_tmp(tmp) as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())

    os.chmod(tmp, mode)
    os.replace(tmp, path)


def open_private_tmp(tmp):
    """以 0600 独占创建临时文件；写入期间不会以 umask 决定的权限暴露内容。"""

    try:
        os.unlink(tmp)
    except OSError:
        pass

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w")


def copy_private(src, dst):
    """复制文件内容与元数据，目标先以 0600 建好再写，最后才套用源文件权限。"""

    with open(src, "rb") as fin, os.fdopen(
        os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as fout:
        shutil.copyfileobj(fin, fout)
        fout.flush()
        os.fsync(fout.fileno())

    shutil.copystat(src, dst)


def write_text_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())

    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644

    with open_private_tmp(tmp) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())

    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_file(path):
    if not os.path.exists(path):
        return None

    dst = "%s.bak.%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))

    try:
        os.unlink(dst)
    except OSError:
        pass

    copy_private(path, dst)
    prune_backups(path)
    return dst


def prune_backups(path, keep=BACKUP_KEEP):
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".bak."

    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith(prefix))
    except OSError:
        return

    for name in names[:-keep]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def restore_file(backup, path):
    """从备份原子恢复：先复制到临时文件再 os.replace，中途失败不会留下半个文件。失败抛 OSError。"""

    if not backup or not os.path.exists(backup):
        raise OSError("备份不存在: %s" % backup)

    tmp = "%s.restore.%d" % (path, os.getpid())

    try:
        try:
            os.unlink(tmp)
        except OSError:
            pass

        copy_private(backup, tmp)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def restore_files(backups):
    """按备份表恢复全部文件。值为 None 表示写入前该文件不存在，应删除。

    每个文件独立处理，一个失败不影响其它；返回 {路径: 失败原因}，空字典表示全部恢复成功。
    """

    failures = {}

    for path, backup in backups.items():
        try:
            if backup:
                restore_file(backup, path)
            elif os.path.exists(path):
                os.unlink(path)
        except OSError as e:
            failures[path] = str(e)[:200]

    return failures


def rollback_and_raise(label, exc, backups, was_active, service, port, secrets=(), **audit_info):
    """写入型命令的统一失败路径：恢复文件 → 服务原本在跑就再拉起 → 审计 → 抛结构化错误。

    恢复失败不会掩盖原始异常：失败明细进 rollback_errors，rolled_back 只在全部恢复成功时为 true。
    KeyboardInterrupt / SystemExit 做完恢复后原样重抛。
    """

    failures = restore_files(backups)
    recovered = None
    why_again = None

    if was_active:
        try:
            recovered, why_again = restart_service(service, port)
        except Exception as again:
            recovered, why_again = False, str(again)[:200]

    reason = scrub(str(exc), secrets)[:200]
    audit(
        label, False, reason=reason, rolled_back=not failures,
        rollback_errors=failures or None, recovered=recovered, **audit_info
    )

    if not isinstance(exc, Exception):
        raise exc

    if isinstance(exc, ManageError):
        error = exc
        error.args = (scrub(str(exc), secrets),)
    else:
        error = ManageError("写入过程出错：%s: %s" % (type(exc).__name__, reason), "internal")

    error.extra["rolled_back"] = not failures

    if failures:
        error.extra["rollback_errors"] = failures

    error.extra["service_recovered"] = recovered

    if was_active and not recovered:
        error.extra["recovery_error"] = why_again

    raise error


def scrub(text, secrets=()):
    """抹掉已知密钥值、Bearer 令牌、网关客户端密钥。"""

    if not isinstance(text, str):
        return text

    for secret in secrets:
        if secret:
            text = text.replace(secret, "****")

    for pattern in SECRET_PATTERNS:
        text = pattern.sub("****", text)

    return text


def scrub_payload(obj, secrets=(), keep=()):
    """递归脱敏整个输出对象；keep 里的顶层字段原样保留（只用于 keys create / install run 的一次性密钥）。"""

    if isinstance(obj, str):
        return scrub(obj, secrets)

    if isinstance(obj, list):
        return [scrub_payload(item, secrets) for item in obj]

    if isinstance(obj, dict):
        return {
            key: (value if key in keep else scrub_payload(value, secrets))
            for key, value in obj.items()
        }

    return obj


def provider_secrets():
    """当前已配置的各家密钥值，只用于脱敏比对，绝不输出。"""

    try:
        from core.registry import PROVIDERS
        from tools import probe

        apply_env_config()
        return [k for k in (probe.current_key(name) for name in PROVIDERS) if k]
    except Exception:
        return []


def log_tail(path, lines=20):
    try:
        with open(path, "r", errors="replace") as f:
            tail = f.read().splitlines()[-lines:]
    except OSError:
        return []

    secrets = provider_secrets()
    return [scrub(line, secrets) for line in tail]


def wait_healthy(port, timeout=HEALTH_WAIT):
    url = "http://127.0.0.1:%d/health" % port
    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True, None

                last = "HTTP %d" % response.status
        except Exception as e:
            last = str(e)[:120]

        time.sleep(0.5)

    return False, last or "健康检查超时"


def restart_service(name, port):
    rc, out, err = sh(["systemctl", "restart", name], timeout=90)

    if rc != 0:
        # 一次显式保存可能碰到 systemd 的短时启动次数上限。
        # 仅对此错误清理目标服务的计数并重试一次；自动崩溃重启的限流仍保留。
        _, result, _ = sh(["systemctl", "show", name, "-p", "Result", "--value"], timeout=10)
        if result.strip() == "start-limit-hit":
            reset_rc, _, _ = sh(["systemctl", "reset-failed", name], timeout=10)
            if reset_rc == 0:
                rc, out, err = sh(["systemctl", "restart", name], timeout=90)

    if rc != 0:
        return False, (err or out or "systemctl restart 失败")[:300]

    ok, why = wait_healthy(port)

    if not ok:
        return False, why

    if not service_is_active(name):
        return False, "服务未处于 active 状态"

    return True, None


def audit(cmd, ok, **info):
    """管理操作留痕到 logs/manage.log，不记任何密钥。"""

    try:
        from core.logger import GatewayLogger

        GatewayLogger().write("manage", dict({"cmd": cmd, "ok": ok}, **info))
    except Exception:
        pass


# ====================================================================
# 网关相关的读取
# ====================================================================

def service_name():
    from core.paths import SERVICE_NAME

    return SERVICE_NAME


def gateway_port(name=None):
    env = read_env_file(service_env_file(name or service_name()))

    try:
        return int(env.get("GATEWAY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def load_config():
    import yaml
    from core.paths import CONFIG_FILE

    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ManageError("读取 config.yaml 失败: %s" % e, "bad_config")

    gateway = config.get("gateway")

    if not isinstance(gateway, dict) or not isinstance(gateway.get("modes"), dict):
        raise ManageError("config.yaml 缺少 gateway.modes", "bad_config")

    return config


def apply_env_config(config=None):
    """让 registry 知道各家密钥文件在哪（config.yaml 的 providers.<name>.env）。"""

    from core.registry import configure_providers

    config = config or load_config()
    try:
        configure_providers(config.get("providers") or {})
    except ValueError as e:
        raise ManageError(str(e), "bad_config")


def config_targets(config):
    """配置里出现的全部模型（去重、保持首次出现顺序）及各自所属模式。"""

    modes = config["gateway"]["modes"]
    order = []
    membership = {}

    def add(mode_name, chain):
        for target in chain or []:
            if target not in membership:
                membership[target] = []
                order.append(target)

            membership[target].append(mode_name)

    for name, value in modes.items():
        if name == "task":
            for task_name, chain in (value or {}).items():
                add(task_name, chain)
        elif isinstance(value, list):
            add(name, value)

    return order, membership


def normalized_modes(config):
    modes = config["gateway"]["modes"]
    task = modes.get("task") or {}

    out = {}

    for name in ("fast", "balanced"):
        out[name] = list(modes.get(name) or [])

    out["task"] = {name: list(task.get(name) or []) for name in REQUIRED_TASKS}

    for name, chain in task.items():
        if name not in out["task"] and isinstance(chain, list):
            out["task"][name] = list(chain)

    out["thinking"] = list(modes.get("thinking") or [])

    for name, chain in modes.items():
        if name not in out and name != "task" and isinstance(chain, list):
            out[name] = list(chain)

    return out


def read_cooldowns():
    from core.paths import COOLDOWN_FILE
    from core.storage import read_json

    now = time.time()
    view = {}

    for target, info in (read_json(COOLDOWN_FILE).get("cooldowns") or {}).items():
        until = (info or {}).get("until", 0)

        if until > now:
            view[target] = {
                "seconds_left": int(until - now),
                "fails": (info or {}).get("fails", 0),
            }

    return view


def scan_access_log(log_dir, day):
    """从 access.log 统计：每个 key 今日调用数、各 provider 最近 60 秒请求数。

    服务进程的 RPM 滑动窗口只在内存里，管理工具拿不到，用访问日志近似。
    """

    from core.apikey import key_id

    path = os.path.join(log_dir, "access.log")
    per_key = {}
    rpm = {}
    cutoff = time.time() - 60
    prefix = '{"time": "%s ' % day

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if not line.startswith(prefix):
                    continue

                try:
                    record = json.loads(line)
                except ValueError:
                    continue

                event = record.get("event")

                if event not in ("request_success", "request_failed"):
                    continue

                kid = record.get("key_id")

                if not kid and record.get("key") and "..." not in record["key"]:
                    kid = key_id(record["key"])  # 旧日志记的是完整密钥

                if kid:
                    per_key[kid] = per_key.get(kid, 0) + 1

                provider = record.get("provider")

                if provider and event == "request_success":
                    try:
                        ts = time.mktime(
                            time.strptime(record["time"], "%Y-%m-%d %H:%M:%S")
                        )
                    except (KeyError, ValueError):
                        continue

                    if ts >= cutoff:
                        rpm[provider] = rpm.get(provider, 0) + 1
    except OSError:
        pass

    return per_key, rpm


# ====================================================================
# status
# ====================================================================

def cmd_status(params):
    from core.apikey import APIKeyManager
    from core.paths import BASE_DIR, LOG_DIR, QUOTA_FILE, STATE_FILE
    from core.registry import PROVIDERS
    from core.storage import read_json

    name = service_name()
    info = systemctl_show(
        name,
        ["ActiveState", "SubState", "MainPID", "ActiveEnterTimestampMonotonic", "NRestarts"],
    )

    try:
        pid = int(info.get("MainPID") or 0)
    except ValueError:
        pid = 0

    service = {
        "name": name,
        "active": info.get("ActiveState") == "active",
        "state": info.get("ActiveState") or "unknown",
        "pid": pid or None,
        "rss_mb": None,
        "uptime_s": None,
        "restarts": int(info.get("NRestarts") or 0),
        "port": gateway_port(name),
        "dir": BASE_DIR,
    }

    if pid:
        try:
            with open("/proc/%d/status" % pid, "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        service["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
        except (OSError, ValueError):
            pass

        try:
            started = int(info.get("ActiveEnterTimestampMonotonic") or 0) / 1e6

            with open("/proc/uptime", "r") as f:
                uptime = float(f.read().split()[0])

            if started:
                service["uptime_s"] = max(0, int(uptime - started))
        except (OSError, ValueError):
            pass

    day = today()

    state = read_json(STATE_FILE)
    models = state.get("models", {}) if state.get("date") == day else {}

    calls = sum(m.get("calls", 0) for m in models.values())
    success = sum(m.get("success", 0) for m in models.values())
    failed = sum(m.get("failed", 0) for m in models.values())
    tokens = sum(m.get("tokens", 0) for m in models.values())

    today_view = {
        "date": day,
        "calls": calls,
        "success": success,
        "failed": failed,
        "success_rate": round(success / calls, 3) if calls else None,
        "tokens": tokens,
        "models_disabled": sorted(t for t, m in models.items() if m.get("disabled")),
    }

    per_key_today, rpm_now = scan_access_log(LOG_DIR, day)

    keys = APIKeyManager().list_keys()

    for item in keys:
        item["calls_today"] = per_key_today.get(item["id"], 0)

    quota_data = read_json(QUOTA_FILE)
    usage = quota_data.get("providers", {}) if quota_data.get("date") == day else {}

    limits = load_config().get("quota") or {}
    quota = {}

    for provider in PROVIDERS:
        used = usage.get(provider) or {}
        limit = limits.get(provider) or {}

        quota[provider] = {
            "calls": used.get("calls", 0),
            "tokens": used.get("tokens", 0),
            "rpm_now": rpm_now.get(provider, 0),
            "rpm_limit": limit.get("rpm", 0),
            "daily_limit": limit.get("daily", 0),
        }

    return {
        "service": service,
        "today": today_view,
        "keys": keys,
        "cooldowns": read_cooldowns(),
        "quota": quota,
        "generated_at": now_iso(),
    }


# ====================================================================
# models
# ====================================================================

def parse_targets(raw):
    from core.registry import PROVIDERS

    if not isinstance(raw, list) or not raw:
        raise ManageError("targets 必须是非空数组", "invalid_input")

    targets = []

    for item in raw:
        error = validate_target(item, PROVIDERS)

        if error:
            raise ManageError("targets 中的 %r：%s" % (item, error), "invalid_input")

        if item not in targets:
            targets.append(item)

    return targets


def cmd_models_list(params):
    from core.paths import CAPABILITY_FILE, STATE_FILE
    from core.registry import PROVIDERS
    from core.storage import read_json

    config = load_config()
    order, membership = config_targets(config)

    day = today()
    state = read_json(STATE_FILE)
    models = state.get("models", {}) if state.get("date") == day else {}
    capability = read_json(CAPABILITY_FILE).get("models", {})
    cooldowns = read_cooldowns()

    out = []

    for target in order:
        provider, model = target.split(":", 1) if ":" in target else (target, "")
        stats = models.get(target, {})
        calls = stats.get("calls", 0)
        success = stats.get("success", 0)

        out.append(
            {
                "target": target,
                "provider": provider,
                "model": model,
                "modes": membership[target],
                "calls": calls,
                "success": success,
                "failed": stats.get("failed", 0),
                "success_rate": round(success / calls, 3) if calls else None,
                "latency_avg": stats.get("latency_avg") if success else None,
                "tokens": stats.get("tokens", 0),
                "disabled": bool(stats.get("disabled")),
                "cooldown_left": cooldowns.get(target, {}).get("seconds_left", 0),
                "capability": capability.get(target),
                "provider_registered": provider in PROVIDERS,
            }
        )

    return {"date": day, "models": out, "count": len(out)}


def cmd_models_test(params):
    from core.paths import TEST_FILE
    from core.storage import atomic_write_json, read_json
    from tools import probe

    if params.get("cached"):
        return {"cached": read_json(TEST_FILE) or None}

    config = load_config()
    apply_env_config(config)

    if params.get("targets") is None:
        targets = config_targets(config)[0]
    else:
        targets = parse_targets(params.get("targets"))

    if not targets:
        raise ManageError("配置里没有任何模型", "invalid_config")

    results = probe.test_targets(targets)
    payload = {
        "results": results,
        "summary": {"total": len(results), "ok": sum(1 for r in results if r["ok"])},
        "tested_at": now_iso(),
    }

    if params.get("targets") is None:
        atomic_write_json(TEST_FILE, payload)

    return payload


def cmd_models_scan(params):
    from core.paths import SCAN_FILE
    from core.registry import PROVIDERS
    from core.storage import atomic_write_json, read_json
    from tools import probe

    if params.get("cached"):
        return {"cached": read_json(SCAN_FILE) or None}

    apply_env_config()

    providers = params.get("providers")

    if providers is not None:
        if not isinstance(providers, list) or not providers:
            raise ManageError("providers 必须是非空数组", "invalid_input")

        unknown = [p for p in providers if p not in PROVIDERS]

        if unknown:
            raise ManageError("未注册的 provider: %s" % ", ".join(map(str, unknown)), "invalid_input")

    apply_env_config()

    targets = params.get("targets")
    if targets is not None:
        if not isinstance(targets, list) or not targets or any(not isinstance(t, str) or validate_target(t, PROVIDERS) for t in targets):
            raise ManageError("targets 必须是非空有效模型数组", "invalid_input")
        providers = list(dict.fromkeys(t.split(":", 1)[0] for t in targets))
    result = probe.scan_free(providers, targets=targets)
    payload = {
        "scanned_at": now_iso(),
        "budget_seconds": probe.BUDGET,
        "truncated": any(entry["truncated"] for entry in result.values()),
        "providers": result,
        "summary": {
            name: {
                "total": entry["total"],
                "ok": len(entry["available"]),
                "skipped": len(entry["skipped"]),
            }
            for name, entry in result.items()
        },
    }

    if providers is None and not atomic_write_json(SCAN_FILE, payload):
        payload["cache_written"] = False

    return payload


# ====================================================================
# config
# ====================================================================

def validate_target(target, providers):
    if not isinstance(target, str) or ":" not in target:
        return "格式应为 provider:model"

    provider, model = target.split(":", 1)

    if provider not in providers:
        return "未注册的 provider '%s'" % provider

    if not model or any(c.isspace() for c in model):
        return "模型名为空或含空白字符"

    return None


def validate_config(modes, capability):
    from core.registry import PROVIDERS

    errors = []
    warnings = []

    if not isinstance(modes, dict):
        errors.append("modes 必须是对象")
        modes = {}

    if not isinstance(capability, dict):
        errors.append("capability 必须是对象")
        capability = {}

    for name in REQUIRED_MODES:
        if name not in modes:
            errors.append("缺少模式 %s" % name)

    task = modes.get("task")

    if not isinstance(task, dict):
        errors.append("缺少 task 子表")
        task = {}
    else:
        for name in REQUIRED_TASKS:
            if name not in task:
                errors.append("task 子表缺少 %s" % name)

    chains = [(name, chain) for name, chain in modes.items() if name != "task"]
    chains += [("task.%s" % name, chain) for name, chain in task.items()]

    for label, chain in chains:
        short = label.split(".", 1)[-1]

        if not MODE_NAME.match(str(short)):
            errors.append("模式名 %r 不合法（小写字母开头，只能含字母数字 _ -）" % label)
            continue

        if not isinstance(chain, list) or not chain:
            errors.append("降级链 %s 为空" % label)
            continue

        seen = set()

        for target in chain:
            error = validate_target(target, PROVIDERS)

            if error:
                errors.append("%s 中的 %r：%s" % (label, target, error))
                continue

            if target in seen:
                errors.append("%s 中重复出现 %s" % (label, target))

            seen.add(target)

            if target not in capability:
                warnings.append("%s 没有 capability 评分，将按默认 50 分参与排序" % target)

    for target, values in capability.items():
        if not isinstance(target, str) or ":" not in target:
            errors.append("capability 键 %r 格式应为 provider:model" % target)
            continue

        if not isinstance(values, dict):
            errors.append("capability[%s] 必须是对象" % target)
            continue

        for field in CAP_FIELDS + tuple(f for f in OPTIONAL_CAP_FIELDS if f in values):
            value = values.get(field)

            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                errors.append("%s 的 %s 应为 0-100 的整数" % (target, field))

    return errors, sorted(set(warnings))


def validate_routing(value):
    if not isinstance(value, dict):
        raise ManageError("routing 必须是对象", "invalid_config")
    if set(value) - set(DEFAULT_ROUTING):
        raise ManageError("routing 含未知字段", "invalid_config")
    policy = dict(DEFAULT_ROUTING, **value)
    if policy["mode"] not in ("manual", "scored"):
        raise ManageError("排序方式必须为 manual 或 scored", "invalid_config")
    for field in ("use_latency", "use_success_rate"):
        if not isinstance(policy[field], bool):
            raise ManageError("%s 必须为布尔值" % field, "invalid_config")
    return policy


def routing_policy(config):
    return validate_routing(config.get("gateway", {}).get("routing", DEFAULT_ROUTING))


TOP_LEVEL_KEY = re.compile(r"^[^\s#-][^:]*:")


def replace_top_level_section(text, key, new_block):
    """用 new_block 替换 YAML 文本里某个顶层段，其余段的文本原样保留。

    顶层键的判定是"顶格、不是注释、不是列表项、含冒号"，带引号或非 ASCII 的键也算。
    紧贴下一段的注释和空行归下一段，不会被吞掉。同名顶层段出现多次时拒绝处理：
    PyYAML 取最后一个，文本替换却只能替换一个，两者会不一致。调用方必须再解析一次
    结果并核对顶层键集合与目标段内容，这里只做文本操作。
    """

    lines = text.splitlines()
    tops = [i for i, line in enumerate(lines) if TOP_LEVEL_KEY.match(line)]
    matches = [i for i in tops if lines[i].split(":", 1)[0].strip().strip("\"'") == key]

    if len(matches) > 1:
        raise ManageError(
            "config.yaml 里有 %d 个顶层 %s 段，请先手工去重再用管理工具改配置" % (len(matches), key),
            "invalid_config",
        )

    start = matches[0] if matches else None
    block_lines = new_block.rstrip("\n").split("\n")

    def joined(parts):
        # 文件尾部只保留一个换行，反复保存不会累积空行
        while parts and not parts[-1].strip():
            parts.pop()

        return "\n".join(parts) + "\n"

    if start is None:
        return joined(lines + [""] + block_lines)

    end = next((i for i in tops if i > start), len(lines))

    while end - 1 > start and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1

    # 下一段前面的空行由这里统一补一个，原有的先去掉，反复保存不会越积越多
    tail = lines[end:]

    while tail and not tail[0].strip():
        tail.pop(0)

    return joined(lines[:start] + block_lines + [""] + tail)


def ensure_other_sections_intact(config, parsed, key="gateway"):
    """重写后的 YAML 顶层键集合必须与原来完全一致，且除 key 之外的段内容不变。"""

    missing = sorted(set(config) - set(parsed))
    extra = sorted(set(parsed) - set(config))

    if missing or extra:
        raise ManageError(
            "生成的 config.yaml 顶层段不一致（丢失 %s，多出 %s），已放弃写入" % (missing, extra),
            "internal",
        )

    for name in config:
        if name != key and parsed[name] != config[name]:
            raise ManageError("生成的 config.yaml 意外改动了 %s 段，已放弃写入" % name, "internal")


def render_config(original_text, config, modes, routing=None):
    import yaml

    gateway = dict(config["gateway"])
    ordered = {}

    for name in ("fast", "balanced"):
        ordered[name] = list(modes[name])

    ordered["task"] = {name: list(modes["task"][name]) for name in REQUIRED_TASKS}

    for name, chain in modes["task"].items():
        if name not in ordered["task"] and isinstance(chain, list):
            ordered["task"][name] = list(chain)

    ordered["thinking"] = list(modes["thinking"])

    for name, chain in modes.items():
        if name not in ordered and name != "task" and isinstance(chain, list):
            ordered[name] = list(chain)

    gateway["modes"] = ordered
    if routing is not None:
        gateway["routing"] = routing

    class IndentedDumper(yaml.SafeDumper):
        """列表项相对父键缩进两格（PyYAML 默认顶格），与手写配置的风格一致。"""

        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    dumped = yaml.dump(
        {"gateway": gateway},
        Dumper=IndentedDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )

    first, rest = dumped.split("\n", 1)
    block = (
        first
        + "\n  # 本段由 manage.py / 桌面管理工具维护（最近写入 %s）。\n" % now_iso()
        + "  # 手写在本段里的注释会在下次保存时丢失；其余各段保持原样。\n"
        + rest
    )

    return replace_top_level_section(original_text, "gateway", block)


def cmd_config_get(params):
    from core.paths import CAPABILITY_FILE, CONFIG_FILE
    from core.registry import PROVIDERS
    from core.storage import read_json

    config = load_config()
    apply_env_config(config)

    return {
        "routing": routing_policy(config),
        "modes": normalized_modes(config),
        "capability": read_json(CAPABILITY_FILE).get("models", {}),
        "providers": list(PROVIDERS),
        "quota": config.get("quota") or {},
        "config_file": CONFIG_FILE,
    }


def cmd_config_set(params):
    import yaml
    from core.paths import BASE_DIR, CAPABILITY_FILE, CONFIG_FILE, LOG_DIR
    from core.storage import atomic_write_json, read_json

    modes = params.get("modes")
    capability = params.get("capability")

    if modes is None or capability is None:
        raise ManageError("需要 modes 和 capability 两个字段", "invalid_input")

    if "routing" in params:
        validate_routing(params["routing"])
    config = load_config()
    apply_env_config(config)
    errors, warnings = validate_config(modes, capability)

    if errors:
        raise ManageError(
            "配置校验未通过：" + "；".join(errors[:5]) + ("…" if len(errors) > 5 else ""),
            "invalid_config",
            errors=errors,
            warnings=warnings,
        )

    if params.get("validate_only"):
        return {"valid": True, "warnings": warnings}

    config = load_config()

    with open(CONFIG_FILE, "r") as f:
        original_text = f.read()

    policy = validate_routing(params["routing"]) if "routing" in params else routing_policy(config)
    new_text = render_config(original_text, config, modes, policy)

    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise ManageError("生成的 config.yaml 无法解析，已放弃写入：%s" % e, "internal")

    if not isinstance(parsed, dict):
        raise ManageError("生成的 config.yaml 顶层不是映射，已放弃写入", "internal")

    ensure_other_sections_intact(config, parsed)

    # 解析回来的 modes 必须与请求完全一致，否则说明文本替换与 YAML 解析看到的不是同一段
    if normalized_modes(parsed) != normalized_modes({"gateway": {"modes": modes}}):
        raise ManageError("生成的 config.yaml 解析出的 modes 与请求不一致，已放弃写入", "internal")

    cap_data = read_json(CAPABILITY_FILE) or {}
    old_cap = cap_data.get("models", {})
    new_cap = {
        target: {field: int(values[field]) for field in CAP_FIELDS + OPTIONAL_CAP_FIELDS if field in values}
        for target, values in capability.items()
    }

    changed = {
        "modes": normalized_modes(config) != normalized_modes(parsed),
        "routing": routing_policy(config) != routing_policy(parsed),
        "capability": old_cap != new_cap,
    }

    name = service_name()
    port = gateway_port(name)
    was_active = service_is_active(name)

    # 备份表：None 表示写入前该文件不存在，回滚时应删除而不是恢复
    backups = {CONFIG_FILE: backup_file(CONFIG_FILE), CAPABILITY_FILE: backup_file(CAPABILITY_FILE)}

    # 从第一次写盘开始，任何失败（包括异常）都回滚两个文件；服务原本在跑就再拉起来
    try:
        write_text_atomic(CONFIG_FILE, new_text)

        if not atomic_write_json(
            CAPABILITY_FILE,
            {
                "_comment": cap_data.get("_comment")
                or "三个维度各 0-100，供 router 按 task_type 加权。由 manage.py / 桌面管理工具维护。",
                "models": new_cap,
            },
        ):
            raise ManageError("写入 capability.json 失败", "write_failed")

        # 让一个干净的解释器把新配置完整加载一遍，比只做结构校验更接近真实启动
        rc, out, err = sh(
            [sys.executable, "-B", "-c", "from router.router import AIRouter; AIRouter()"],
            timeout=60,
            cwd=BASE_DIR,
            env=dict(os.environ, GATEWAY_DIR=BASE_DIR, PYTHONPATH=BASE_DIR),
        )

        if rc != 0:
            raise ManageError(
                "新配置无法被路由层加载：%s" % scrub(err or out, provider_secrets())[-300:],
                "load_failed",
            )

        if was_active:
            ok, why = restart_service(name, port)

            if not ok:
                raise ManageError(
                    "重启后服务异常（%s）" % why,
                    "restart_failed",
                    log_tail=log_tail(os.path.join(LOG_DIR, "system-error.log")),
                )

    except BaseException as e:
        rollback_and_raise("config set", e, backups, was_active, name, port)

    audit("config set", True, changed=changed, restarted=was_active)

    result = {
        "restarted": was_active,
        "service_active": was_active,
        "warnings": warnings,
        "changed": changed,
        "backups": [b for b in backups.values() if b],
    }

    if not was_active:
        result["note"] = "服务当前未运行，配置已写入，启动时生效"

    return result


# ====================================================================
# keys
# ====================================================================

def cmd_keys_list(params):
    from core.apikey import APIKeyManager

    return {"keys": APIKeyManager().list_keys()}


def cmd_keys_create(params):
    from core.apikey import APIKeyManager, key_id, mask_key

    name = str(params.get("name") or "client").strip()[:64] or "client"
    manager = APIKeyManager()
    key = manager.create_key(name)

    audit("keys create", True, name=name, id=key_id(key))

    return {
        "key": key,
        "id": key_id(key),
        "masked": mask_key(key),
        "name": name,
        "created": today(),
    }


def _key_by_id(manager, params):
    kid = params.get("id")

    if not isinstance(kid, str) or not kid:
        raise ManageError("缺少 id", "invalid_input")

    key = manager.find_by_id(kid)

    if not key:
        raise ManageError("找不到 id 为 %s 的密钥" % kid, "not_found")

    return key


def _keys_action(action):
    def handler(params):
        from core.apikey import APIKeyManager, key_id

        manager = APIKeyManager()
        key = _key_by_id(manager, params)
        ok = getattr(manager, action)(key)

        audit("keys " + action, ok, id=key_id(key))

        if not ok:
            raise ManageError("操作失败", "internal")

        return {"id": key_id(key), "action": action}

    return handler


# ====================================================================
# providers
# ====================================================================

def _require_provider(params):
    from core.registry import PROVIDERS

    apply_env_config()
    name = params.get("name")

    if not isinstance(name, str) or name not in PROVIDERS:
        raise ManageError(
            "name 必须是已注册的 provider：%s" % ", ".join(PROVIDERS), "invalid_input"
        )

    return name


def cmd_providers_list(params):
    from core.registry import PROVIDERS, BUILTIN_PROVIDERS, env_file_for, provider_definition
    from tools import probe

    apply_env_config()
    out = []

    for name, cls in PROVIDERS.items():
        path = env_file_for(name)
        key = probe.read_env_value(path, cls.env_var)

        out.append(
            {
                "name": name,
                "base_url": cls.base_url,
                "custom": name not in BUILTIN_PROVIDERS,
                "free_models": provider_definition(name).get("free_models", []),
                "env_file": path,
                "env_var": cls.env_var,
                "configured": bool(key),
                "masked": ("****" + key[-4:]) if key and len(key) >= 8 else ("****" if key else None),
            }
        )

    return {"providers": out}


def cmd_providers_test(params):
    from tools import probe

    name = _require_provider(params)
    apply_env_config()

    key = probe.current_key(name)

    if not key:
        raise ManageError("%s 未配置密钥" % name, "not_configured")

    result = probe.test_key(name, key)
    return {"provider": name, "test": result, "tested_at": now_iso()}


def cmd_providers_set(params):
    from core.paths import LOG_DIR
    from core.registry import PROVIDERS, env_file_for
    from tools import probe

    name = _require_provider(params)
    key = params.get("key")
    force = bool(params.get("force"))

    if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key.strip()):
        raise ManageError("密钥为空或含空白字符", "invalid_input")

    key = key.strip()
    apply_env_config()

    test = probe.test_key(name, key)

    if not test["ok"] and not force:
        raise ManageError(
            "新密钥验证失败：%s" % test["error"], "key_test_failed", test=test
        )

    cls = PROVIDERS[name]
    path = env_file_for(name)
    service = service_name()
    port = gateway_port(service)
    was_active = service_is_active(service)
    backups = {path: backup_file(path)}

    try:
        write_env_var(path, cls.env_var, key)

        if was_active:
            ok, why = restart_service(service, port)

            if not ok:
                raise ManageError(
                    "写入后服务异常（%s）" % why,
                    "restart_failed",
                    log_tail=log_tail(os.path.join(LOG_DIR, "system-error.log")),
                )

    except BaseException as e:
        # 新密钥此时已从文件里撤掉，最终输出的脱敏层看不到它，所以这里显式传入
        rollback_and_raise("providers set", e, backups, was_active, service, port, secrets=[key], provider=name)

    forced = force and not test["ok"]
    audit("providers set", True, provider=name, tested=test["ok"], forced=forced, restarted=was_active)

    result = {
        "provider": name,
        "tested": test["ok"],
        "test": test,
        "forced": forced,
        "restarted": was_active,
        "service_active": was_active,
        "backup": backups[path],
    }

    if not was_active:
        result["note"] = "服务当前未运行，密钥已写入，启动时生效"

    return result


def cmd_providers_catalog(params):
    from tools import probe
    name = _require_provider(params)
    return {"provider": name, "models": probe.model_catalog(name), "queried_at": now_iso()}


def _save_provider_config(config, name, definition, key=None):
    import yaml
    from core.paths import CONFIG_FILE, BASE_DIR
    config["providers"] = dict(config.get("providers") or {})
    config["providers"][name] = definition
    with open(CONFIG_FILE, "r") as f:
        original = f.read()
    block = yaml.safe_dump({"providers": config["providers"]}, allow_unicode=True, sort_keys=False)
    text = replace_top_level_section(original, "providers", block)
    parsed = yaml.safe_load(text)
    if parsed != config:
        raise ManageError("Provider 配置保存校验失败，未写入文件", "invalid_config")
    service = service_name()
    port = gateway_port(service)
    was_active = service_is_active(service)
    backups = {CONFIG_FILE: backup_file(CONFIG_FILE)}
    path = os.path.join(BASE_DIR, definition["env"]) if key else None
    if path:
        backups[path] = backup_file(path)
    try:
        if path:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            write_env_var(path, definition["env_var"], key)
        write_text_atomic(CONFIG_FILE, text)
        apply_env_config(config)
        if was_active:
            ok, why = restart_service(service, port)
            if not ok:
                raise ManageError("保存后服务异常（%s）" % why, "restart_failed")
    except BaseException as e:
        try:
            rollback_and_raise("providers save", e, backups, was_active, service, port, secrets=[key] if key else [], provider=name)
        finally:
            apply_env_config()
    audit("providers save", True, provider=name, restarted=was_active)
    return {"provider": name, "restarted": was_active, "service_active": was_active,
            "note": None if was_active else "服务当前未运行，配置已保存，启动时生效"}


def cmd_providers_add(params):
    from core.paths import BASE_DIR
    from core.provider_config import validate_definition
    from core.registry import PROVIDERS
    apply_env_config()
    name = params.get("name")
    if isinstance(name, str) and name in PROVIDERS:
        raise ManageError("Provider 已存在，不能覆盖内置或现有 Provider", "already_exists")
    key = params.get("key")
    if not isinstance(key, str) or not key.strip() or any(c.isspace() or ord(c) < 32 for c in key.strip()) or any(c in key for c in "\"'"):
        raise ManageError("密钥为空或含不支持的空白/引号字符", "invalid_input")
    key = key.strip()
    try:
        definition = validate_definition(name, {
            "type": "openai", "base_url": params.get("base_url"), "env_var": params.get("env_var"),
            "env": "env/%s.env" % name, "free_models": params.get("free_models", []),
            "allow_local_http": params.get("allow_local_http") is True,
        })
    except ValueError as e:
        raise ManageError(str(e), "invalid_input")
    path = os.path.join(BASE_DIR, definition["env"])
    if os.path.lexists(path) or os.path.islink(os.path.dirname(path)):
        raise ManageError("目标密钥文件已存在或 env 目录为链接，拒绝覆盖", "already_exists")
    return _save_provider_config(load_config(), name, definition, key)


def cmd_providers_free_models(params):
    from core.provider_config import validate_free_models
    name = _require_provider(params)
    try:
        models = validate_free_models(params.get("free_models"))
    except ValueError as e:
        raise ManageError(str(e), "invalid_input")
    config = load_config()
    definition = dict((config.get("providers") or {}).get(name) or {})
    definition["free_models"] = models
    return dict(_save_provider_config(config, name, definition), free_models=models)


# ====================================================================
# install（check 只用标准库，可单独拷到新机器运行）
# ====================================================================

def cmd_install_check(params):
    gateway_dir = str(params.get("gateway_dir") or "/opt/ai-gateway").rstrip("/") or "/opt/ai-gateway"
    # 服务名优先级与 install.sh / core/paths.py 一致：显式参数 > 目录里记录的 .service-name > 目录名
    service = str(params.get("service_name") or "").strip()

    if not service:
        try:
            with open(os.path.join(gateway_dir, ".service-name"), "r") as f:
                service = f.read().strip()
        except OSError:
            service = ""

    service = service or os.path.basename(gateway_dir)

    try:
        port = int(params.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT

    os_name = None

    try:
        with open("/etc/os-release", "r") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass

    def importable(module):
        try:
            __import__(module)
            return True
        except ImportError:
            return False

    mem_total = None

    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass

    port_in_use = False

    try:
        sock = socket.socket()
        sock.settimeout(1)
        port_in_use = sock.connect_ex(("127.0.0.1", port)) == 0
        sock.close()
    except OSError:
        pass

    has_systemctl = shutil.which("systemctl") is not None
    version = sys.version_info

    checks = {
        "root": os.geteuid() == 0,
        "os": os_name,
        "python": {
            "version": "%d.%d.%d" % (version[0], version[1], version[2]),
            "path": sys.executable,
            "ok": (version[0], version[1]) >= (3, 8),
        },
        "apt": shutil.which("apt-get") is not None,
        "systemd": has_systemctl and os.path.isdir("/run/systemd/system"),
        "deps": {"requests": importable("requests"), "yaml": importable("yaml")},
        "existing": {
            "path": gateway_dir,
            "installed": os.path.isfile(os.path.join(gateway_dir, "server.py")),
            "has_config": os.path.isfile(os.path.join(gateway_dir, "config.yaml")),
            "unit_exists": os.path.exists("/etc/systemd/system/%s.service" % service),
            "service_name": service,
            "service_active": has_systemctl and service_is_active(service),
        },
        "port": {"port": port, "in_use": port_in_use},
        "disk_free_mb": shutil.disk_usage("/").free // (1024 * 1024),
        "mem_total_mb": mem_total,
    }

    problems = []

    if not checks["root"]:
        problems.append("需要以 root 运行")

    if not checks["python"]["ok"]:
        problems.append("Python 需要 3.8 以上")

    if not checks["systemd"]:
        problems.append("没有 systemd")

    if not (checks["deps"]["requests"] and checks["deps"]["yaml"]) and not checks["apt"]:
        problems.append("缺少 requests/yaml 且没有 apt-get 可用")

    if port_in_use and not checks["existing"]["service_active"]:
        problems.append("端口 %d 已被其他程序占用" % port)

    checks["problems"] = problems
    checks["ready"] = not problems
    return checks


def cmd_install_run(params):
    from core.apikey import APIKeyManager, key_id
    from core.paths import BASE_DIR, LOG_DIR
    from core.registry import PROVIDERS, env_file_for
    from tools import probe

    env = params.get("env") or {}
    force = bool(params.get("force"))

    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise ManageError("env 必须是 {变量名: 值} 的对象", "invalid_input")

    service = service_name()
    steps = []

    try:
        port = int(params.get("port") or 0) or None
    except (TypeError, ValueError):
        raise ManageError("port 必须是整数", "invalid_input")

    host = params.get("host")

    if host is not None and (not isinstance(host, str) or not re.match(r"^[0-9A-Za-z.:]+$", host)):
        raise ManageError("host 不合法", "invalid_input")

    # 0. 先把提供的密钥都验证一遍，全部通过（或 force）才开始动盘
    provided = {}

    for name, cls in PROVIDERS.items():
        value = (env.get(cls.env_var) or "").strip()

        if not value:
            continue

        if any(c.isspace() for c in value):
            raise ManageError("%s 的密钥含空白字符" % cls.env_var, "invalid_input")

        provided[name] = value

    secrets = list(provided.values())
    tests = {name: probe.test_key(name, value) for name, value in provided.items()}
    failed = sorted(name for name, r in tests.items() if not r["ok"])

    if failed and not force:
        raise ManageError(
            "密钥验证失败：%s；未写入任何文件" % ", ".join(failed),
            "key_test_failed",
            tests=tests,
        )

    steps.append(
        {
            "step": "验证 provider 密钥",
            "ok": not failed,
            "detail": "通过 %d / %d%s"
            % (len(provided) - len(failed), len(provided), "（force 跳过失败项）" if failed else ""),
        }
    )

    # 1. unit 不存在说明 install.sh 还没跑过（或跑失败），这里补跑
    unit = "/etc/systemd/system/%s.service" % service

    if not os.path.exists(unit):
        script = os.path.join(BASE_DIR, "install.sh")

        if not os.path.exists(script):
            raise ManageError("找不到 %s" % script, "not_installed", steps=steps)

        child_env = dict(os.environ, GATEWAY_DIR=BASE_DIR, SERVICE_NAME=service)

        if port:
            child_env["GATEWAY_PORT"] = str(port)

        if host:
            child_env["GATEWAY_HOST"] = host

        rc, out, err = sh(["bash", script], timeout=900, env=child_env, cwd=BASE_DIR)
        steps.append(
            {"step": "install.sh", "ok": rc == 0, "detail": scrub(out + "\n" + err, secrets).strip()[-800:]}
        )

        if rc != 0:
            raise ManageError("install.sh 失败", "install_failed", steps=steps)

    # 2. 所有文件写入放在同一个事务里：provider 密钥 + 服务 env；任何一步失败就整体还原。
    #    同一路径只备份一次（多家 provider 可能共用一个 env 文件）。
    apply_env_config()
    backups = {}

    def backup_once(path):
        if path not in backups:
            backups[path] = backup_file(path)

    service_env = service_env_file(service)

    try:
        for name, value in provided.items():
            path = env_file_for(name)
            backup_once(path)
            write_env_var(path, PROVIDERS[name].env_var, value)

        current = read_env_file(service_env)
        backup_once(service_env)

        if port:
            write_env_var(service_env, "GATEWAY_PORT", str(port), mode=0o644)
        elif "GATEWAY_PORT" not in current:
            write_env_var(service_env, "GATEWAY_PORT", str(DEFAULT_PORT), mode=0o644)

        if host:
            write_env_var(service_env, "GATEWAY_HOST", host, mode=0o644)
        elif "GATEWAY_HOST" not in current:
            write_env_var(service_env, "GATEWAY_HOST", "0.0.0.0", mode=0o644)

    except BaseException as e:
        failures = restore_files(backups)

        if not isinstance(e, Exception):
            raise

        raise ManageError(
            "写入配置文件失败，已还原%s：%s"
            % ("（部分文件还原失败）" if failures else "", scrub(str(e), secrets)[:200]),
            "write_failed",
            steps=steps,
            rolled_back=not failures,
            rollback_errors=failures or None,
        )

    written = sorted(provided)
    steps.append({"step": "写入 provider 密钥", "ok": True, "detail": ", ".join(written) or "未提供任何密钥"})

    effective_port = gateway_port(service)
    steps.append({"step": "服务配置", "ok": True, "detail": "%s 端口 %d" % (service_env, effective_port)})

    # 3. 启动（已在运行则重启以加载新密钥）
    sh(["systemctl", "enable", service], timeout=60)
    ok, why = restart_service(service, effective_port)
    steps.append({"step": "启动服务", "ok": ok, "detail": why or "健康检查通过"})

    if not ok:
        audit("install run", False, reason=why)
        raise ManageError(
            "服务启动失败：%s" % why,
            "start_failed",
            steps=steps,
            log_tail=log_tail(os.path.join(LOG_DIR, "system-error.log")),
        )

    # 4. 没有客户端密钥的话发一个，不然网关装好了也没法调用
    manager = APIKeyManager()
    first_key = None

    if not manager.list_keys():
        first_key = manager.create_key("default")
        steps.append({"step": "创建首个客户端密钥", "ok": True, "detail": key_id(first_key)})

    audit("install run", True, providers=written, port=effective_port, first_key=bool(first_key))

    return {
        "steps": steps,
        "tests": tests,
        "service": service,
        "port": effective_port,
        "providers_written": written,
        "first_key": first_key,
        "first_key_id": key_id(first_key) if first_key else None,
    }


# ====================================================================
# 入口
# ====================================================================

COMMANDS = {
    "status": cmd_status,
    "models list": cmd_models_list,
    "models test": cmd_models_test,
    "models scan": cmd_models_scan,
    "config get": cmd_config_get,
    "config set": cmd_config_set,
    "keys list": cmd_keys_list,
    "keys create": cmd_keys_create,
    "keys disable": _keys_action("disable"),
    "keys enable": _keys_action("enable"),
    "keys delete": _keys_action("delete"),
    "providers list": cmd_providers_list,
    "providers test": cmd_providers_test,
    "providers set": cmd_providers_set,
    "providers add": cmd_providers_add,
    "providers catalog": cmd_providers_catalog,
    "providers free-models": cmd_providers_free_models,
    "install check": cmd_install_check,
    "install run": cmd_install_run,
}


def usage(error=None):
    return {
        "ok": False,
        "error": error or "用法: manage.py <命令> [子命令] [--json]，参数从 stdin 读 JSON",
        "code": "usage",
        "commands": sorted(COMMANDS),
    }


def main(argv):
    words = [a for a in argv if a != "--json"]

    if not words or words[0] in ("-h", "--help", "help"):
        emit(usage(), 2)

    cmd = " ".join(words)
    handler = COMMANDS.get(cmd)

    if handler is None:
        emit(usage("未知命令: %s" % cmd), 2)

    # 最终输出边界：无论成功失败，整个 JSON 递归脱敏；只有 keys create / install run 的一次性密钥字段例外
    def finish(payload, exit_code, keep=()):
        emit(scrub_payload(payload, provider_secrets(), keep), exit_code)

    try:
        params = read_params(cmd)
        result = handler(params)
    except ManageError as e:
        finish(dict({"ok": False, "error": str(e), "code": e.code}, **e.extra), 1)
    except subprocess.TimeoutExpired as e:
        finish({"ok": False, "error": "子进程超时: %s" % str(e)[:200], "code": "timeout"}, 1)
    except Exception as e:
        # KeyStoreError 来自 core.apikey：密钥文件没写成功，调用方必须知道这不是内部错误
        finish(
            {
                "ok": False,
                "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
                "code": "write_failed" if type(e).__name__ == "KeyStoreError" else "internal",
            },
            1,
        )

    finish(dict({"ok": True}, **result), 0, keep=KEEP_FIELDS.get(cmd, ()))


if __name__ == "__main__":
    main(sys.argv[1:])
