"""实例路径。

所有数据、日志、配置的位置都从代码所在目录推导，同一台机器上可以并存多个实例
（例如安装向导用 /opt/ai-gateway-test 做演练，不会碰正式实例的文件）。
GATEWAY_DIR 环境变量可显式覆盖；GATEWAY_SERVICE 覆盖 systemd 服务名（默认取目录名）。
"""

import os

BASE_DIR = os.path.abspath(
    os.environ.get("GATEWAY_DIR")
    or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)



def _service_name():
    """优先级：GATEWAY_SERVICE 环境变量 > 实例目录下的 .service-name（install.sh 写入）> 目录名。"""

    explicit = os.environ.get("GATEWAY_SERVICE")

    if explicit:
        return explicit

    try:
        with open(os.path.join(BASE_DIR, ".service-name"), "r") as f:
            name = f.read().strip()

        if name:
            return name
    except OSError:
        pass

    return os.path.basename(BASE_DIR)


SERVICE_NAME = _service_name()

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")

STATE_FILE = os.path.join(DATA_DIR, "state.json")
QUOTA_FILE = os.path.join(DATA_DIR, "quota.json")
KEY_FILE = os.path.join(DATA_DIR, "apikeys.json")
COOLDOWN_FILE = os.path.join(DATA_DIR, "cooldowns.json")
CAPABILITY_FILE = os.path.join(DATA_DIR, "capability.json")
SCAN_FILE = os.path.join(DATA_DIR, "scan-latest.json")
TEST_FILE = os.path.join(DATA_DIR, "test-latest.json")
