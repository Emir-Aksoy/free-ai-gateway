"""Per-call transport details. Credentials are removed before persistence."""
import contextvars
import json
import re
from contextlib import contextmanager
from urllib.parse import quote

_CURRENT = contextvars.ContextVar('gateway_call_trace', default=None)
MASK = '[REDACTED]'
CREDENTIAL = re.compile(r'^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-goog-api-key|key|(?:[a-z0-9]+[_-])*api[_-]?key|access[_-]?token|refresh[_-]?token|secret|client[_-]?secret|password|private[_-]?key|token)$', re.I)
TOKEN = re.compile(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]+|\b(?:sk-or-v1-[a-z0-9]{20,}|sk-[a-z0-9_-]{20,}|gsk_[a-z0-9]{20,}|AIza[a-z0-9_-]{25,}|nvx-[a-f0-9]{32}|gh[pousr]_[a-z0-9]{25,}|github_pat_[a-z0-9_]{30,})')
ASSIGNMENT = re.compile(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|token)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|[^\s,;&<>"']+)''')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----', re.S)


def known_secrets():
    from core.registry import PROVIDERS, env_file_for
    from core.paths import KEY_FILE
    values = []
    for name, cls in list(PROVIDERS.items()):
        path = env_file_for(name)
        if not path:
            continue
        try:
            with open(path) as f:
                for line in f:
                    if line.strip().startswith(cls.env_var + '='):
                        value = line.split('=', 1)[1].strip().strip("\"'")
                        if value:
                            values.append(value)
        except FileNotFoundError:
            pass
    try:
        with open(KEY_FILE) as f:
            values.extend(json.load(f).get('keys', {}))
    except FileNotFoundError:
        pass
    return values


def _mask_assignment(match):
    value = match[2]
    first = value[:1] if value[:1] in ('"', "'") else ''
    last = first if first and len(value) > 1 and value.endswith(first) else ''
    return match[1] + first + MASK + last


def _sensitive_spans(text, secrets):
    spans = [match.span() for match in TOKEN.finditer(text)]
    spans += [match.span() for match in PRIVATE_KEY.finditer(text)]
    for match in ASSIGNMENT.finditer(text):
        a, b = match.span(2)
        if text[a:a+1] in ('"', "'"):
            quote = text[a]; a += 1
            if b > a and text[b-1] == quote:
                b -= 1
        if b > a:
            spans.append((a, b))
    for secret in secrets:
        if secret:
            spans.extend(match.span() for match in re.finditer(re.escape(secret), text))
    return spans


def _decode_json_escapes(text):
    # Keep source offsets so masking a decoded key also removes its escaped bytes.
    values, offsets = [], []
    pattern = re.compile(r'\\u[0-9a-fA-F]{4}|\\["\\/bfnrt]|[\s\S]')
    for match in pattern.finditer(text):
        token = match[0]
        if token.startswith('\\'):
            try:
                token = json.loads('"' + token + '"')
            except ValueError:
                pass
        values.append(token)
        offsets.extend([match.span()] * len(token))
    return ''.join(values), offsets


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {redact(str(k), secrets): MASK if CREDENTIAL.fullmatch(str(k)) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    if value.startswith('data:') or '\ndata:' in value:
        return redact_sse(value, secrets)
    if value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
            cleaned = redact(parsed, secrets)
            if cleaned != parsed:
                return json.dumps(cleaned, ensure_ascii=False)
            return value
        except (ValueError, TypeError):
            pass
    for key in sorted(set(s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        for variant in (key, quote(key, safe=''), json.dumps(key)[1:-1]):
            value = value.replace(variant, MASK)
    value = PRIVATE_KEY.sub(MASK, value)
    value = TOKEN.sub(MASK, value)
    return ASSIGNMENT.sub(_mask_assignment, value)


def redact_sse(body, secrets):
    """Also redact known credentials split across successive JSON delta fields."""
    lines = body.splitlines(keepends=True)
    parsed, groups = {}, {}
    def collect(value, path, refs):
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str):
                    refs.setdefault(path + (k,), []).append((value, k))
                else:
                    collect(v, path + (k,), refs)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                index = v.get('index', i) if isinstance(v, dict) else i
                if not isinstance(index, (int, str)):
                    index = i
                collect(v, path + (index,), refs)
    for i, line in enumerate(lines):
        if not line.startswith('data:'):
            continue
        try:
            item = json.loads(line[5:].strip())
        except ValueError:
            continue
        parsed[i] = item
        collect(item, (), groups)
    for refs in groups.values():
        joined = ''.join(obj[key] for obj, key in refs)
        spans = _sensitive_spans(joined, secrets)
        decoded, offsets = _decode_json_escapes(joined)
        if decoded != joined:
            for a, b in _sensitive_spans(decoded, secrets):
                spans.append((offsets[a][0], offsets[b-1][1]))
        merged = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        offset = 0
        for obj, key in refs:
            text = obj[key]; end = offset + len(text)
            for a, b in reversed(merged):
                if a < end and b > offset:
                    left, right = max(0, a-offset), min(len(text), b-offset)
                    text = text[:left] + (MASK if offset <= a else '') + text[right:]
            obj[key] = text
            offset = end
    for i, item in parsed.items():
        ending = '\r\n' if lines[i].endswith('\r\n') else '\n' if lines[i].endswith('\n') else ''
        lines[i] = 'data: ' + json.dumps(redact(item, secrets), ensure_ascii=False) + ending
    for i, line in enumerate(lines):
        if i not in parsed:
            # Non-JSON SSE lines cannot be decoded. Redact their content directly.
            prefix = 'data:' if line.startswith('data:') else ''
            content = line[len(prefix):]
            # Avoid re-entering SSE detection for an unparseable data line.
            for secret in secrets:
                if secret:
                    content = content.replace(secret, MASK)
            content = PRIVATE_KEY.sub(MASK, TOKEN.sub(MASK, content))
            content = ASSIGNMENT.sub(_mask_assignment, content)
            lines[i] = prefix + content
    return ''.join(lines)


class CallTrace:
    def __init__(self, request=None):
        self.data = {'request': request, 'attempts': []}
        self.secrets = []

    @contextmanager
    def bind(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def wrap(self, iterator):
        try:
            while True:
                with self.bind():
                    try:
                        item = next(iterator)
                    except StopIteration:
                        return
                yield item
        finally:
            closer = getattr(iterator, 'close', None)
            if closer:
                with self.bind():
                    closer()

    def begin(self, url, payload, headers, key=None):
        if key:
            self.secrets.append(key)
        row = {'request': {'url': url, 'headers': dict(headers), 'body': payload}}
        self.data['attempts'].append(row)
        return row

    def snapshot(self):
        try:
            secrets = self.secrets + known_secrets()
        except (OSError, ValueError, TypeError):
            return {"capture_complete": False, "capture_error": "密钥清单无法读取，未保存可能含密钥的正文"}
        data = dict(self.data, attempts=[], capture_complete=True)
        if '_fallback_stream' in data:
            data['stream_body'] = redact_sse(''.join(data.pop('_fallback_stream')), secrets)
        for attempt in self.data['attempts']:
            row = dict(attempt)
            response = dict(row.get('response') or {})
            if response.get('stream'):
                response['body'] = redact_sse(''.join(response.pop('lines', [])), secrets)
            if response:
                row['response'] = response
            data['attempts'].append(row)
        return redact(data, secrets)


def current_trace():
    return _CURRENT.get()


def begin_attempt(url, payload, headers, key=None):
    trace = current_trace()
    return trace.begin(url, payload, headers, key) if trace is not None else None


def capture_response(attempt, response, streaming=False):
    if attempt is None:
        return
    headers = response.headers if isinstance(response.headers, dict) or hasattr(response.headers, 'items') else {}
    attempt['response'] = {'status': response.status_code, 'headers': dict(headers), 'stream': streaming}
    if streaming:
        attempt['response']['lines'] = []
    else:
        body = response.text
        attempt['response']['body'] = body if isinstance(body, str) else json.dumps(response.json(), ensure_ascii=False)
