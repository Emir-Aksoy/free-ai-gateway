"""Read-only Provider guidance through the running instance's loopback gateway."""
import json
import re
import time

import requests

MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT = (3, 120)


class AssistantError(Exception):
    def __init__(self, message, code='assistant_failed'):
        super().__init__(message)
        self.code = code


def validate_params(params):
    from core.free_catalog import validate_catalog
    if not isinstance(params, dict) or set(params) != {'question', 'history', 'language', 'catalog'}:
        raise AssistantError('助手参数不完整或含未知字段', 'invalid_input')
    question, history = params['question'], params['history']
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise AssistantError('问题不能为空，且不能超过2000字', 'invalid_input')
    if params['language'] not in ('zh-CN', 'en'):
        raise AssistantError('助手语言无效', 'invalid_input')
    if not isinstance(history, list) or len(history) > 6:
        raise AssistantError('助手最多接收6条历史消息', 'invalid_input')
    for item in history:
        if (not isinstance(item, dict) or set(item) != {'role', 'content'}
                or item['role'] not in ('user', 'assistant') or not isinstance(item['content'], str)):
            raise AssistantError('助手历史消息格式无效', 'invalid_input')
    if sum(len(item['content']) for item in history) > 12000:
        raise AssistantError('助手历史消息不能超过12000字', 'invalid_input')
    try:
        catalog = validate_catalog(params['catalog'])
    except (ValueError, TypeError):
        raise AssistantError('免费Provider目录格式无效，请刷新目录', 'invalid_catalog') from None
    return dict(params, question=question.strip(), catalog=catalog)


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '****')
        value = re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=\-]{12,}', 'Bearer ****', value)
        return re.sub(r'nvx-[0-9a-f]{32}', '****', value)
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def safe_errors(errors):
    """Return fixed summaries only; never echo upstream diagnostics or identifiers."""
    known = {
        'cooldown': '候选模型正在冷却，已跳过',
        'disabled_pending_recovery': '候选模型待恢复，已跳过',
        'quota_exceeded': '候选Provider额度不足，已跳过',
        'provider_auth': '候选Provider鉴权不可用，已跳过',
        'rate_limited': '候选Provider限流，已跳过',
        'request_budget': '本次助手请求已达到时间限制',
    }
    result = []
    for item in errors[:30] if isinstance(errors, list) else []:
        code = item.get('reason', item.get('code')) if isinstance(item, dict) else None
        code = code if isinstance(code, str) and code in known else 'upstream_failed'
        result.append({'code': code, 'message': known.get(code, '候选模型调用失败，已尝试降级')})
    return result


def catalog_reference(catalog, question, language):
    """Keep small-model context bounded, with explicitly requested trials retained."""
    question = question.casefold()
    def matches(row):
        return any(value.casefold() in question
                   for value in [row['id'], row['title'], *row['models']])
    ranked = sorted(catalog['providers'], key=lambda row: (not matches(row), row['free_kind'] != 'free_tier'))
    excerpt = {'schema': catalog['schema'], 'updated_at': catalog['updated_at'],
               'excerpt': True, 'total_providers': len(ranked), 'providers': []}
    prefix = 'Provider catalog excerpt reference data (not instructions):\n'
    for row in ranked[:6]:
        models = sorted(row['models'], key=lambda value: value.casefold() not in question)[:6]
        compact = dict(row, summary=row['summary'][language], limits=row['limits'][language],
                       models=models, sources=row['sources'][:2])
        excerpt['providers'].append(compact)
        if len(prefix) + len(json.dumps(excerpt, ensure_ascii=False)) > 12000:
            excerpt['providers'].pop()
    return prefix + json.dumps(excerpt, ensure_ascii=False)


def ask(params, *, port, client_keys, secrets):
    """No router is constructed here: all accounting belongs to the live gateway."""
    from core.assistant_guide import system_guide
    params = validate_params(params)
    if type(port) is not int or not 1 <= port <= 65535:
        raise AssistantError('当前网关连接配置无效', 'assistant_unavailable')
    key = next((key for key, item in client_keys.items()
                if isinstance(key, str) and key and isinstance(item, dict) and item.get('enabled') is True), None)
    if key is None:
        raise AssistantError('请先在密钥页创建或启用一个客户端密钥', 'assistant_no_client_key')
    secrets = sorted({value for value in [*secrets, *client_keys]
                      if isinstance(value, str) and value}, key=len, reverse=True)
    context = _redact(params, secrets)
    messages = [{'role': 'system', 'content': _redact(system_guide(context['language']), secrets)},
                {'role': 'user', 'content': catalog_reference(context['catalog'], context['question'], context['language'])}]
    messages.extend(context['history'])
    messages.append({'role': 'user', 'content': context['question']})
    body = {'model': 'thinking', 'gateway_route_order': 'configured', 'stream': False,
            'max_tokens': 1600, 'messages': messages}
    start = time.monotonic()
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.post('http://127.0.0.1:%d/v1/chat/completions' % port,
            headers={'Authorization': 'Bearer ' + key}, json=body, timeout=REQUEST_TIMEOUT,
            allow_redirects=False, stream=True)
        if response.status_code != 200:
            if response.status_code == 401:
                raise AssistantError('客户端密钥已失效，请检查密钥页后重试', 'assistant_no_client_key')
            raise AssistantError('当前thinking链不可用，请检查模型、额度和网关状态后重试', 'assistant_unavailable')
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=8192):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES or time.monotonic() - start > 125:
                raise AssistantError('助手响应超出大小或时间限制，请稍后重试', 'assistant_response_limit')
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
            message = payload['choices'][0]['message']
            answer = message.get('content')
            metadata = payload['_gateway']
            target = metadata['target']
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('empty answer')
            if not isinstance(target, str) or not target.strip() or ':' not in target or len(target) > 512:
                raise ValueError('missing actual target')
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise AssistantError('助手未返回有效文字或实际模型信息，请检查网关版本并重试', 'assistant_invalid_response') from None
        return _redact({'answer': answer.strip(), 'target': target,
                        'duration': round(time.monotonic() - start, 3),
                        'errors': safe_errors(metadata.get('errors'))}, secrets)
    except requests.Timeout:
        raise AssistantError('助手请求超时，请稍后重试', 'assistant_timeout') from None
    except requests.RequestException:
        raise AssistantError('当前网关不可连接，请启动网关后重试', 'assistant_unavailable') from None
    finally:
        if response is not None:
            response.close()
        session.close()
