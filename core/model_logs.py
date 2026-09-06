"""Indexed summaries with separately compressed, credential-redacted call details."""
import gzip
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
MAX_DETAIL_BYTES = 512 * 1024 * 1024
SOURCES = {'business', 'manual_test', 'recovery'}
OUTCOMES = {'success', 'failed', 'skipped', 'cancelled'}
REASONS = {
    'provider_auth': '服务商密钥或权限异常，请检查 Provider 配置',
    'rate_limited': '服务商限流或余额不足，等待额度恢复',
    'request_incompatible': '当前请求或模型不兼容，不计入模型可靠性',
    'cooldown': '模型正在冷却，暂时跳过',
    'error_placeholder': '上游只返回了模型错误占位文本，已判定失败',
    'model_error': '上游通过结束标记报告模型错误，已判定失败',
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
        for key in ('decision', 'metrics'):
            if isinstance(fields.get(key), dict):
                from core.call_trace import redact, known_secrets
                try: row[key] = redact(fields[key], known_secrets())
                except (OSError, ValueError, TypeError): pass
        if fields.get('failure_category') in ('provider_auth','rate_limited','request_incompatible','reliability'):
            row['failure_category'] = fields['failure_category']
        details = fields.get('details')
        compressed = None
        if isinstance(details, dict):
            from core.call_trace import redact, known_secrets
            try:
                details = redact(details, known_secrets())
            except (OSError, ValueError, TypeError):
                details = {"capture_complete": False, "capture_error": "密钥清单无法读取，未保存可能含密钥的正文"}
            compressed = gzip.compress(json.dumps(details, ensure_ascii=False).encode('utf-8'), compresslevel=3)
        row['details_available'] = compressed is not None and details.get('capture_complete', True)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(sqlite3.connect(str(self.path), timeout=1)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS details (call_id INTEGER PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE, payload BLOB NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_target_id ON calls(target,id)')
            db.execute("CREATE INDEX IF NOT EXISTS calls_request_id ON calls(json_extract(payload,'$.request_id'),id)")
            db.execute('CREATE INDEX IF NOT EXISTS calls_time_id ON calls(created_at,id)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_target_time_id ON calls(target,created_at,id)')
            db.execute("CREATE INDEX IF NOT EXISTS calls_provider_time_id ON calls(substr(target,1,instr(target,':')-1),created_at,id)")
            cursor = db.execute('INSERT INTO calls(target,created_at,payload) VALUES (?,?,?)',
                                (target, time.time(), json.dumps(row, ensure_ascii=False)))
            if compressed is not None:
                db.execute('INSERT INTO details(call_id,payload) VALUES (?,?)', (cursor.lastrowid, compressed))
            db.execute('DELETE FROM calls WHERE id <= ?', (cursor.lastrowid - MAX_RECORDS,))
            used = db.execute('SELECT coalesce(sum(length(payload)),0) FROM details').fetchone()[0]
            if used > MAX_DETAIL_BYTES:
                cutoff = None
                for ident, size in db.execute('SELECT call_id,length(payload) FROM details WHERE call_id < ? ORDER BY call_id', (cursor.lastrowid,)):
                    used -= size
                    cutoff = ident
                    if used <= MAX_DETAIL_BYTES:
                        break
                if cutoff is not None:
                    db.execute('DELETE FROM calls WHERE id <= ?', (cutoff,))

    def read(self, target=None, limit=50, before=None, source=None, outcome=None,
             provider=None, start_at=None, end_at=None, request_id=None):
        if target is not None and (not isinstance(target, str) or ':' not in target or len(target) > 512):
            raise ValueError('请选择有效模型')
        if provider is not None and (not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', provider)):
            raise ValueError('请选择有效 provider')
        if target is not None and provider is not None and target.split(':', 1)[0] != provider:
            raise ValueError('模型与 provider 不匹配')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('每次查询上限为 50 条')
        if before is not None and (type(before) is not int or not 0 < before < 2**53):
            raise ValueError('日志游标无效，请刷新')
        if source is not None and (not isinstance(source, str) or source not in SOURCES):
            raise ValueError('日志来源无效')
        if outcome is not None and (not isinstance(outcome, str) or outcome not in OUTCOMES):
            raise ValueError('日志结果筛选无效')
        for stamp in (start_at, end_at):
            if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= 253402300799):
                raise ValueError('时间范围无效')
        if start_at is not None and end_at is not None and start_at > end_at:
            raise ValueError('开始时间不能晚于结束时间')
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id)):
            raise ValueError('请求编号无效')
        result = {'target': target, 'provider': provider, 'entries': [], 'next_cursor': None,
                  'retention_limit': MAX_RECORDS, 'available': self.path.is_file()}
        if not result['available']:
            return result
        where = ['1 = 1']; args = []
        if request_id is not None:
            where.append("json_extract(payload, '$.request_id') = ?"); args.append(request_id)
        if target is not None:
            where.append('target = ?'); args.append(target)
        elif provider is not None:
            where.append("substr(target,1,instr(target,':')-1) = ?"); args.append(provider)
        for clause, value in (('created_at >= ?', start_at), ('created_at <= ?', end_at)):
            if value is not None:
                where.append(clause); args.append(value)
        for key, value in (('source', source), ('outcome', outcome)):
            if value is not None:
                where.append("json_extract(payload, '$.%s') = ?" % key); args.append(value)
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            if before is not None:
                boundary = db.execute('SELECT created_at FROM calls WHERE id = ?', (before,)).fetchone()
                if boundary is None:
                    raise ValueError('旧日志已清理，请刷新最新记录')
                where.append('(created_at < ? OR (created_at = ? AND id < ?))')
                args.extend([boundary[0], boundary[0], before])
            sql = 'SELECT id,created_at,target,payload FROM calls WHERE ' + ' AND '.join(where) + ' ORDER BY created_at DESC,id DESC LIMIT ?'
            rows = db.execute(sql, args + [limit + 1]).fetchall()
        for ident, stamp, actual_target, payload in rows[:limit]:
            row = json.loads(payload)
            row.update(id=ident, time=stamp, target=actual_target)
            row['reason'] = REASONS.get(row.get('code'))
            result['entries'].append(row)
        if len(rows) > limit:
            result['next_cursor'] = rows[limit - 1][0]
        return result


    def export_rows(self, target=None, provider=None, start_at=None, end_at=None, source=None, outcome=None, ident=None):
        # Reuse the list query's strict filter validation, keeping its 50-row cap.
        self.read(target, limit=1, provider=provider, start_at=start_at, end_at=end_at, source=source, outcome=outcome)
        if ident is not None and (type(ident) is not int or not 0 < ident < 2**53):
            raise ValueError('日志编号无效')
        if not self.path.is_file():
            return
        from core.call_trace import redact, known_secrets
        secrets = known_secrets()
        where, args = ['1=1'], []
        for clause, value in [('c.target=?', target), ("substr(c.target,1,instr(c.target,':')-1)=?", provider),
                              ('c.created_at>=?', start_at), ('c.created_at<=?', end_at), ('c.id=?', ident)]:
            if value is not None:
                where.append(clause); args.append(value)
        for key, value in [('source', source), ('outcome', outcome)]:
            if value is not None:
                where.append("json_extract(c.payload, '$.%s')=?" % key); args.append(value)
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('BEGIN')
            has_details = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='details'").fetchone()
            column = 'd.payload' if has_details else 'NULL'
            join = ' LEFT JOIN details d ON d.call_id=c.id' if has_details else ''
            rows = db.execute('SELECT c.id,c.created_at,c.target,c.payload,' + column + ' FROM calls c' + join + ' WHERE ' + ' AND '.join(where) + ' ORDER BY c.created_at DESC,c.id DESC', args)
            found = False
            for row_id, stamp, actual_target, payload, compressed in rows:
                found = True
                row = json.loads(payload)
                row.update(id=row_id, time=stamp, target=actual_target, details_available=compressed is not None)
                row['reason'] = REASONS.get(row.get('code'))
                row['details'] = json.loads(gzip.decompress(compressed)) if compressed is not None else None
                if row['details'] is not None and row['details'].get('capture_complete') is False:
                    row['details_available'] = False
                yield redact(row, secrets)
            if ident is not None and not found:
                raise ValueError('日志不存在或已按保留策略清理')
