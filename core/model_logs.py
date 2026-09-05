"""按模型索引的有限调用摘要；不保存请求/响应正文或原始异常。"""
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from core.paths import LOG_DIR

MAX_RECORDS = 50000
SOURCES = {'business', 'manual_test', 'recovery'}
OUTCOMES = {'success', 'failed', 'skipped', 'cancelled'}
REASONS = {
    'empty_response': '上游没有返回有效正文或工具调用',
    'invalid_json': '上游返回的内容不是合法 JSON',
    'stream_incomplete': '流式响应在正常结束标记前中断',
    'stream_error': '上游在流式响应中返回错误',
    'network_error': '连接上游时发生网络错误',
    'timeout': '等待上游响应超时',
    'http_error': '上游通过 HTTP 错误拒绝了请求',
    'upstream_error': '上游调用失败，未提供可安全展示的详细原因',
    'disabled_pending_recovery': '模型已禁用，等待自动恢复',
    'quota_exceeded': '当前 provider 配额或频率限制已用尽',
    'client_closed': '客户端提前结束请求，不计为模型失败',
    'missing_key': '未配置 provider 密钥',
    'invalid_response': '上游响应不符合兼容接口格式',
    'test_failed': '探测失败',
    'budget_exceeded': '本轮测试时间预算已用完，未发送探测',
    'config_error': '模型配置无效，未发送探测',
}


def failure_code(error):
    code = getattr(error, 'code', None)
    if code in REASONS:
        return code
    return 'http_error' if getattr(error, 'status', None) else 'upstream_error'


class ModelCallLog:
    def __init__(self, path=None):
        self.path = Path(path or Path(LOG_DIR) / 'model-calls.sqlite3')

    def write(self, target, **fields):
        if not isinstance(target, str) or ':' not in target or len(target) > 512:
            return
        row = {k: fields[k] for k, values in (('source', SOURCES), ('outcome', OUTCOMES))
               if fields.get(k) in values}
        if len(row) != 2:
            return
        code = fields.get('code')
        if code in REASONS:
            row['code'] = code
        mode = fields.get('mode')
        if mode in ('fast', 'balanced', 'thinking', 'code', 'writing', 'agent', 'task'):
            row['mode'] = mode
        request_id = fields.get('request_id')
        if isinstance(request_id, str) and re.fullmatch(r'[a-f0-9]{32}', request_id):
            row['request_id'] = request_id
        for key in ('status', 'input_messages', 'input_chars', 'tool_count', 'max_tokens'):
            value = fields.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9:
                row[key] = value
        duration = fields.get('duration')
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration):
            row['duration'] = round(max(0, duration), 3)
        if isinstance(fields.get('stream'), bool):
            row['stream'] = fields['stream']
        next_target = fields.get('next_target')
        if isinstance(next_target, str) and ':' in next_target and len(next_target) <= 512:
            row['next_target'] = next_target
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(sqlite3.connect(str(self.path), timeout=0.2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_target_id ON calls(target,id)')
            cursor = db.execute('INSERT INTO calls(target,created_at,payload) VALUES (?,?,?)',
                                (target, time.time(), json.dumps(row, ensure_ascii=False)))
            db.execute('DELETE FROM calls WHERE id <= ?', (cursor.lastrowid - MAX_RECORDS,))

    def read(self, target, limit=50, before=None, source=None, outcome=None):
        if not isinstance(target, str) or ':' not in target or len(target) > 512:
            raise ValueError('请选择有效模型')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('每页条数必须在 1 到 200 之间')
        if before is not None and (type(before) is not int or not 0 < before < 2**53):
            raise ValueError('日志游标无效，请刷新')
        if source is not None and source not in SOURCES:
            raise ValueError('日志来源无效')
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError('日志结果筛选无效')
        result = {'target': target, 'entries': [], 'next_cursor': None, 'retention_limit': MAX_RECORDS,
                  'available': self.path.is_file()}
        if not result['available']:
            return result
        where = ['target = ?']; args = [target]
        if before is not None:
            where.append('id < ?'); args.append(before)
        # source/outcome 不接入 SQL 字符串；所有外部值都使用参数绑定。
        for key, value in (('source', source), ('outcome', outcome)):
            if value is not None:
                where.append("json_extract(payload, '$.%s') = ?" % key); args.append(value)
        sql = 'SELECT id,created_at,payload FROM calls WHERE ' + ' AND '.join(where) + ' ORDER BY id DESC LIMIT ?'
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute(sql, args + [limit + 1]).fetchall()
        for ident, stamp, payload in rows[:limit]:
            row = json.loads(payload)
            row.update(id=ident, time=stamp, target=target)
            row['reason'] = REASONS.get(row.get('code'))
            result['entries'].append(row)
        if len(rows) > limit:
            result['next_cursor'] = rows[limit - 1][0]
        return result
