"""网关自身的客户端 API key 管理。

转多线程后原来的实现有并发问题：无锁的 calls 自增会丢计数，
且每次请求都同步覆盖写文件。这里加锁、节流落盘、原子写，
密钥比对保持简单的字典查找。

密钥文件还会被 manage.py 在服务进程外修改（新建 / 禁用 / 启用 / 删除）。
两边都遵守同一条规则："重读文件 → 合并 → 写入"整段持有跨进程文件锁，
且持锁期间一律强制重读（不依赖时间戳判断），重读时调用计数取两边较大值，
其余字段以文件为准。这样服务进程的节流落盘不会把外部改动覆盖回去，
manage.py 也不会丢掉服务刚记的计数。
"""

import copy
import fcntl
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from core.paths import KEY_FILE
from core.storage import atomic_write_json

KEY_PREFIX = "nvx-"
LOCK_FILE = KEY_FILE + ".lock"
FLUSH_INTERVAL = 2.0


class KeyStoreError(Exception):
    """密钥文件写入失败，调用方不得当作成功。"""


def key_id(key):
    """稳定、不可逆的短标识，供管理工具定位密钥而不必传递密钥本身。"""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_key(key):
    if not key:
        return "***"

    if len(key) > 12:
        return "%s...%s" % (key[:8], key[-4:])

    return "***"


@contextmanager
def file_lock():
    """跨进程锁。锁文件独立于数据文件：数据文件每次原子替换都会换 inode，锁在它上面没意义。"""

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class APIKeyManager:

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"keys": {}}
        self.loaded_version = None
        self.dirty = False
        self.last_flush = 0.0

        with self.lock, file_lock():
            self._reload_if_changed(force=True)

    # ---------- 文件同步 ----------

    def _file_version(self):
        """文件版本标识。原子替换必然换 inode，所以 (inode, 大小, mtime) 组合不会因时间戳粒度粗而撞车。"""

        try:
            st = os.stat(KEY_FILE)
        except OSError:
            return None

        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _read_file(self):
        """返回 (数据, 是否可信)。文件不存在是正常的空库；存在但读不出或结构不对则不可信。"""

        try:
            with open(KEY_FILE, "r") as f:
                fresh = json.load(f)
        except FileNotFoundError:
            return {"keys": {}}, True
        except (OSError, ValueError):
            return None, False

        if not isinstance(fresh, dict) or not isinstance(fresh.get("keys"), dict):
            return None, False

        # 每条记录也要成形：不是对象、calls 不是整数、enabled 不是布尔 → 整个文件不可信，
        # 读路径继续用内存数据，写路径拒绝覆盖
        for key, item in fresh["keys"].items():
            if not isinstance(key, str) or not isinstance(item, dict):
                return None, False

            if not isinstance(item.get("calls", 0), int) or isinstance(item.get("calls"), bool):
                return None, False

            if not isinstance(item.get("enabled", False), bool):
                return None, False

        return fresh, True

    def _reload_if_changed(self, force=False):
        """调用方已持 self.lock。文件被外部改过就重新读入，调用计数取两边较大值。

        持有 file_lock 的写路径必须 force=True：写之前的合并不能依赖版本判断。
        文件损坏时：读路径保留内存里的数据继续服务；写路径抛 KeyStoreError，
        绝不把一个空库写回去覆盖掉（可能只是人工改坏了一个逗号）。
        """

        version = self._file_version()

        if not force and version == self.loaded_version:
            return False

        fresh, trusted = self._read_file()

        if not trusted:
            if force:
                raise KeyStoreError("%s 无法解析，拒绝覆盖；请先修复或删除该文件" % KEY_FILE)

            return False

        for key, item in fresh["keys"].items():
            old = self.data["keys"].get(key)

            if old and isinstance(item, dict):
                item["calls"] = max(item.get("calls", 0), old.get("calls", 0))

        self.data = fresh
        self.loaded_version = version
        return True

    def _write_locked(self):
        """调用方已持 self.lock 与 file_lock。任何写入失败都抛 KeyStoreError。"""

        try:
            ok = atomic_write_json(KEY_FILE, self.data, mode=0o600)
        except OSError as e:
            raise KeyStoreError("写入 %s 失败: %s" % (KEY_FILE, e))

        if not ok:
            raise KeyStoreError("写入 %s 失败" % KEY_FILE)

        self.loaded_version = self._file_version()
        self.dirty = False
        self.last_flush = time.time()

    def _mutate(self, operation):
        """结构性改动：持跨进程锁，强制合并外部改动，再改，再立即落盘。

        改动或落盘过程中的任何异常都把内存恢复到改动前的快照再抛出，
        调用方不会拿到一个"内存里有、文件里没有"的状态。
        """

        with self.lock, file_lock():
            self._reload_if_changed(force=True)
            snapshot = copy.deepcopy(self.data)

            try:
                result = operation(self.data["keys"])
                self._write_locked()
            except Exception:
                self.data = snapshot
                raise

            return result

    def _flush_locked(self):
        """调用方已持 self.lock。节流落盘同样要在文件锁内先强制合并外部改动。"""

        with file_lock():
            try:
                self._reload_if_changed(force=True)
                self._write_locked()
                return True
            except Exception:
                # 计数丢一点无妨，下次再试；结构性改动不走这条路
                return False

    # ---------- 服务进程用 ----------

    def create_key(self, name):
        key = KEY_PREFIX + os.urandom(16).hex()

        def operation(keys):
            keys[key] = {
                "name": name,
                "enabled": True,
                "calls": 0,
                "created": datetime.now().strftime("%Y-%m-%d"),
            }

        # 新建密钥必须落盘成功才能发出去，否则会发出一个不存在的密钥
        self._mutate(operation)
        return key

    def verify(self, key):
        if not key:
            return False

        with self.lock:
            self._reload_if_changed()
            item = self.data["keys"].get(key)

            if not item:
                return False

            return bool(item.get("enabled", False))

    def record(self, key):
        with self.lock:
            self._reload_if_changed()
            item = self.data["keys"].get(key)

            if not item:
                return

            item["calls"] = item.get("calls", 0) + 1
            self.dirty = True

            if time.time() - self.last_flush >= FLUSH_INTERVAL:
                self._flush_locked()

    def flush(self):
        with self.lock:
            if not self.dirty:
                return False

            return self._flush_locked()

    # ---------- 管理工具用 ----------

    def set_enabled(self, key, enabled):
        def operation(keys):
            item = keys.get(key)

            if not item:
                return False

            item["enabled"] = bool(enabled)
            return True

        return self._mutate(operation)

    def disable(self, key):
        return self.set_enabled(key, False)

    def enable(self, key):
        return self.set_enabled(key, True)

    def delete(self, key):
        def operation(keys):
            if key not in keys:
                return False

            del keys[key]
            return True

        return self._mutate(operation)

    def find_by_id(self, kid):
        with self.lock:
            self._reload_if_changed()

            for key in self.data["keys"]:
                if key_id(key) == kid:
                    return key

        return None

    def list_keys(self):
        """脱敏视图，永远不包含完整密钥。"""

        with self.lock:
            self._reload_if_changed()

            return [
                {
                    "id": key_id(key),
                    "masked": mask_key(key),
                    "name": item.get("name"),
                    "enabled": bool(item.get("enabled", False)),
                    "calls": item.get("calls", 0),
                    "created": item.get("created"),
                }
                for key, item in self.data["keys"].items()
                if isinstance(item, dict)
            ]
