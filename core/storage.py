"""JSON 持久化工具。

转成多线程后，原来"每次请求直接覆盖写 JSON"的做法有两个问题：
写入过程中崩溃会留下半个文件，以及每请求两次同步落盘的 IO 竞争。
这里统一成原子写 + 节流刷盘。
"""

import json
import os
import threading
import time


def open_private_tmp(tmp):
    """以 0600 独占创建临时文件：写入期间内容不会以 umask 决定的权限暴露（apikeys.json 里有密钥）。"""

    try:
        os.unlink(tmp)
    except OSError:
        pass

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w")


def atomic_write_json(path, data, mode=None):
    """先写临时文件再 rename。同目录下 rename 是原子操作，
    读到的要么是旧内容要么是新内容，不会是半个文件。

    最终权限：mode 指定则用 mode；否则沿用旧文件的权限；新文件 0644。
    """

    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp = "%s.tmp.%d" % (path, os.getpid())

    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644

    try:
        with open_private_tmp(tmp) as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True

    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass

        return False


def read_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}

    try:
        with open(path, "r") as f:
            return json.load(f)

    except (ValueError, OSError):
        return default if default is not None else {}


class ThrottledStore:
    """带节流的落盘器。

    高频计数不必每次都写盘：标记为脏，最多每 interval 秒刷一次，
    进程退出时再强制刷一次。极端情况下最多丢 interval 秒的计数，
    对统计用途可以接受，换来的是 IO 从每请求 2 次降到每秒 1 次以内。
    """

    def __init__(self, path, interval=2.0):
        self.path = path
        self.interval = interval
        self.lock = threading.RLock()
        self.dirty = False
        self.last_flush = 0.0

    def mark_dirty(self):
        with self.lock:
            self.dirty = True

    def maybe_flush(self, data, force=False):
        with self.lock:
            if not self.dirty and not force:
                return False

            now = time.time()

            if not force and (now - self.last_flush) < self.interval:
                return False

            ok = atomic_write_json(self.path, data)

            if ok:
                self.dirty = False
                self.last_flush = now

            return ok
