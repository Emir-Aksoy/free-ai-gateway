"""Stateless Responses adapter over the existing Chat Completions router.

No hosted tools or server-side conversation store. Tool execution stays in clients.
"""
import copy
import hashlib
import json
import re
import time
import uuid


def _text(value, field):
    if not isinstance(value, str):
        raise ValueError('%s must be a string' % field)
    return value


def _content(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError('message content must be text or a content array')
    parts = []
    for part in value:
        if not isinstance(part, dict):
            raise ValueError('invalid content part')
        kind = part.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            parts.append({'type': 'text', 'text': _text(part.get('text'), 'text')})
        elif kind == 'input_image' and isinstance(part.get('image_url'), str):
            parts.append({'type': 'image_url', 'image_url': {'url': part['image_url'], 'detail': part.get('detail', 'auto')}})
        else:
            raise ValueError('unsupported content type: %s' % kind)
    if all(p['type'] == 'text' for p in parts):
        return '\n'.join(p['text'] for p in parts)
    return parts


class ToolMapping(set):
    """Custom-tool names plus reversible, per-request namespace aliases."""
    def __init__(self):
        super().__init__()
        self.aliases = {}
        self.reverse = {}


def _tools(raw):
    if not isinstance(raw, list):
        raise ValueError('tools must be an array')
    mapping = ToolMapping()
    flat = []
    for tool in raw:
        if not isinstance(tool, dict):
            raise ValueError('invalid tool')
        namespace = tool.get('name') if tool.get('type') == 'namespace' else None
        children = tool.get('tools') if namespace is not None else [tool]
        if not isinstance(children, list):
            raise ValueError('namespace tools must be an array')
        for child in children:
            if not isinstance(child, dict) or child.get('type') not in ('function', 'custom'):
                raise ValueError('only client function and custom tools are supported')
            name = _text(child.get('name'), 'tool name')
            alias = name
            if namespace is not None:
                _text(namespace, 'namespace')
                label = re.sub('[^a-zA-Z0-9_-]', '_', namespace)[:14] + '_' + re.sub('[^a-zA-Z0-9_-]', '_', name)[:24]
                alias = 'gw_' + label + '_' + hashlib.sha256((namespace+'\0'+name).encode()).hexdigest()[:16]
            if alias in mapping.aliases:
                raise ValueError('duplicate tool name')
            mapping.aliases[alias] = (namespace, name)
            mapping.reverse[(namespace, name)] = alias
            flat.append(dict(child, name=alias))
    return flat, mapping


def _tool_alias(mapping, item):
    name = _text(item.get('name'), 'tool name')
    namespace = item.get('namespace')
    if namespace is not None:
        _text(namespace, 'namespace')
    return mapping.reverse.get((namespace, name), name)


def to_chat_request(body):
    if not isinstance(body, dict):
        raise ValueError('request must be an object')
    for field in ('store', 'background'):
        if body.get(field) is not None and body[field] is not False:
            raise ValueError('%s is not supported; use false' % field)
    for field in ('previous_response_id', 'conversation', 'context_management'):
        if body.get(field) is not None:
            raise ValueError('%s is not supported; send full input history' % field)
    if body.get('truncation') not in (None, 'disabled'):
        raise ValueError('automatic truncation is not supported')
    if body.get('stream') is not None and type(body['stream']) is not bool:
        raise ValueError('stream must be boolean')
    if body.get('model') is not None:
        _text(body['model'], 'model')
    if body.get('instructions') is not None:
        _text(body['instructions'], 'instructions')
    chat = {'model': body.get('model') or 'agent', 'stream': bool(body.get('stream')), 'messages': []}
    if body.get('instructions'):
        chat['messages'].append({'role': 'system', 'content': _text(body['instructions'], 'instructions')})
    tools, custom = _tools(body.get('tools', []))
    items = body.get('input')
    if isinstance(items, str):
        items = [{'role': 'user', 'content': items}]
    if not isinstance(items, list) or not items:
        raise ValueError('input must contain messages')
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('input item must be an object')
        kind = item.get('type', 'message')
        if kind == 'message':
            role = item.get('role')
            if role not in ('system', 'developer', 'user', 'assistant'):
                raise ValueError('unsupported message role')
            chat['messages'].append({'role': 'system' if role == 'developer' else role, 'content': _content(item.get('content'))})
        elif kind in ('function_call', 'custom_tool_call'):
            arguments = item.get('arguments') if kind == 'function_call' else json.dumps({'input': _text(item.get('input'), 'input')}, ensure_ascii=False)
            call = {'id': _text(item.get('call_id'), 'call_id'), 'type': 'function', 'function': {'name': _tool_alias(custom, item), 'arguments': _text(arguments, 'arguments')}}
            messages = chat['messages']
            if not messages or messages[-1]['role'] != 'assistant':
                messages.append({'role': 'assistant', 'content': None})
            messages[-1].setdefault('tool_calls', []).append(call)
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            chat['messages'].append({'role': 'tool', 'tool_call_id': _text(item.get('call_id'), 'call_id'), 'content': _content(item.get('output'))})
        elif kind == 'reasoning' and not item.get('encrypted_content'):
            # This adapter never emits opaque reasoning state. Summary text is optional presentation.
            summary = item.get('summary', [])
            if not isinstance(summary, list) or any(not isinstance(part, dict) for part in summary):
                raise ValueError('reasoning summary must be an array of text parts')
            if summary:
                text = '\n'.join(_text(p.get('text'), 'summary') for p in item['summary'])
                chat['messages'].append({'role': 'assistant', 'content': text})
        else:
            raise ValueError('unsupported input item: %s' % kind)
    converted = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') not in ('function', 'custom'):
            raise ValueError('only client function and custom tools are supported')
        name = _text(tool.get('name'), 'tool name')
        function = {'name': name, 'description': tool.get('description', '')}
        if tool['type'] == 'custom':
            custom.add(name)
            function['parameters'] = {'type': 'object', 'properties': {'input': {'type': 'string', 'description': 'The exact raw tool input.'}}, 'required': ['input'], 'additionalProperties': False}
            fmt = tool.get('format', {'type': 'text'})
            if not isinstance(fmt, dict):
                raise ValueError('custom format must be an object')
            if fmt.get('type') != 'text':
                raise ValueError('custom grammar tools are not supported; use text format')
        else:
            function['parameters'] = tool.get('parameters') or {'type': 'object', 'properties': {}}
            if tool.get('strict') is not None:
                function['strict'] = tool['strict']
        converted.append({'type': 'function', 'function': function})
    if converted:
        chat['tools'] = converted
    choice = body.get('tool_choice')
    if isinstance(choice, dict):
        if choice.get('type') not in ('function', 'custom'):
            raise ValueError('unsupported tool_choice')
        choice = {'type': 'function', 'function': {'name': _tool_alias(custom, choice)}}
    if choice is not None:
        if not isinstance(choice, dict) and choice not in ('auto', 'none', 'required'):
            raise ValueError('unsupported tool_choice')
        chat['tool_choice'] = choice
    for field in ('temperature', 'top_p', 'parallel_tool_calls'):
        if body.get(field) is not None:
            chat[field] = body[field]
    if body.get('max_output_tokens') is not None:
        maximum = body['max_output_tokens']
        if type(maximum) is not int or maximum < 1:
            raise ValueError('max_output_tokens must be a positive integer')
        chat['max_tokens'] = maximum
    reasoning = body.get('reasoning')
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ValueError('reasoning must be an object')
        if reasoning.get('effort'):
            chat['reasoning_effort'] = reasoning['effort']
    text = body.get('text')
    if text is not None:
        if not isinstance(text, dict):
            raise ValueError('text must be an object')
        fmt = text.get('format', {'type': 'text'})
        if not isinstance(fmt, dict):
            raise ValueError('text format must be an object')
        if fmt.get('type') == 'json_schema':
            chat['response_format'] = {'type': 'json_schema', 'json_schema': {k: v for k, v in fmt.items() if k != 'type'}}
        elif fmt.get('type') == 'json_object':
            chat['response_format'] = {'type': 'json_object'}
        elif fmt.get('type') != 'text':
            raise ValueError('unsupported text format')
    return chat, custom


class ResponseStream:
    def __init__(self, model, custom_tools):
        self.custom = custom_tools
        self.sequence = 0
        self.output = []
        self.message = None
        self.calls = {}
        self.finish_reason = None
        self.size = 0
        self.response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()), 'status': 'in_progress', 'model': model, 'output': self.output, 'error': None, 'incomplete_details': None, 'usage': None, 'store': False, 'parallel_tool_calls': True}

    def event(self, kind, **fields):
        event = {'type': kind, 'sequence_number': self.sequence, **fields}
        self.sequence += 1
        return copy.deepcopy(event)

    def start_events(self):
        yield self.event('response.created', response=self.response)
        yield self.event('response.in_progress', response=self.response)

    def feed(self, chunk):
        if not isinstance(chunk, dict) or chunk.get('error'):
            raise ValueError('upstream returned an invalid response')
        usage = chunk.get('usage')
        if isinstance(usage, dict):
            def number(v): return v if type(v) is int and v >= 0 else 0
            inp = number(usage.get('prompt_tokens')); out = number(usage.get('completion_tokens'))
            cached = usage.get('prompt_tokens_details') or {}; reasoning = usage.get('completion_tokens_details') or {}
            self.response['usage'] = {'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out, 'input_tokens_details': {'cached_tokens': number(cached.get('cached_tokens'))}, 'output_tokens_details': {'reasoning_tokens': number(reasoning.get('reasoning_tokens'))}}
        choices = chunk.get('choices') or []
        if not choices:
            return
        choice = choices[0]; delta = choice.get('delta') or {}
        self.finish_reason = choice.get('finish_reason') or self.finish_reason
        text = delta.get('content')
        if text:
            text = _text(text, 'upstream content'); self._count(text)
            if self.message is None:
                item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}
                index = len(self.output); self.output.append(item); self.message = (index, item)
                yield self.event('response.output_item.added', output_index=index, item=item)
                item['content'].append({'type': 'output_text', 'text': '', 'annotations': [], 'logprobs': []})
                yield self.event('response.content_part.added', output_index=index, item_id=item['id'], content_index=0, part=item['content'][0])
            index, item = self.message
            item['content'][0]['text'] += text
            yield self.event('response.output_text.delta', output_index=index, item_id=item['id'], content_index=0, delta=text, logprobs=[])
        for call in delta.get('tool_calls') or []:
            position = call.get('index', 0)
            state = self.calls.setdefault(position, {'id': '', 'name': '', 'arguments': '', 'item': None})
            fn = call.get('function') or {}
            state['id'] = call.get('id') or state['id']
            name_part = fn.get('name') or ''
            if state['item'] is not None and name_part:
                raise ValueError('upstream changed a tool name after announcing it')
            self._count(name_part)
            state['name'] += name_part
            args = fn.get('arguments') or ''; self._count(args)
            state['arguments'] += args
            aliases = getattr(self.custom, 'aliases', None)
            if state['item'] is None and state['name'] and state['id'] and (aliases is None or (state['name'] in aliases and not any(name != state['name'] and name.startswith(state['name']) for name in aliases))):
                yield from self._add_call(state)
                args = state['arguments']
            item = state['item']
            if item is not None and item['type'] == 'function_call' and args:
                item['arguments'] += args
                yield self.event('response.function_call_arguments.delta', output_index=state['index'], item_id=item['id'], delta=args)

    def _add_call(self, state):
        custom = state['name'] in self.custom
        item = {'id': ('ct_' if custom else 'fc_') + uuid.uuid4().hex, 'type': 'custom_tool_call' if custom else 'function_call', 'status': 'in_progress', 'call_id': state['id'], 'name': state['name'], 'input' if custom else 'arguments': ''}
        namespace, name = getattr(self.custom, 'aliases', {}).get(state['name'], (None, state['name']))
        item['name'] = name
        if namespace is not None:
            item['namespace'] = namespace
        state['item'] = item; state['index'] = len(self.output); self.output.append(item)
        yield self.event('response.output_item.added', output_index=state['index'], item=item)

    def _count(self, text):
        self.size += len(text.encode("utf-8"))
        if self.size > 16 * 1024 * 1024:
            raise ValueError('response exceeds compatibility buffer limit')

    def finish(self):
        for state in self.calls.values():
            if state['item'] is None:
                aliases = getattr(self.custom, 'aliases', None)
                if not state['name'] or not state['id'] or (aliases is not None and state['name'] not in aliases):
                    raise ValueError('upstream returned an unknown or incomplete tool')
                yield from self._add_call(state)
                if state['item']['type'] == 'function_call':
                    state['item']['arguments'] = state['arguments']
                    yield self.event('response.function_call_arguments.delta', output_index=state['index'], item_id=state['item']['id'], delta=state['arguments'])
        if not self.output or (not self.calls and self.message and not self.message[1]['content'][0]['text'].strip()) or any(s['item'] is None for s in self.calls.values()):
            raise ValueError('upstream returned no valid output')
        incomplete = self.finish_reason in ('length', 'content_filter')
        for state in self.calls.values():
            item = state['item']
            namespace, name = getattr(self.custom, 'aliases', {}).get(state['name'], (None, state['name']))
            item['name'] = name
            if item['type'] == 'custom_tool_call':
                try:
                    value = json.loads(state['arguments'])
                    item['input'] = _text(value['input'], 'custom tool input')
                except (ValueError, KeyError, TypeError):
                    if not incomplete:
                        raise ValueError('invalid custom tool input') from None
                    item['input'] = ''
                    continue
                yield self.event('response.custom_tool_call_input.delta', output_index=state['index'], item_id=item['id'], delta=item['input'])
                yield self.event('response.custom_tool_call_input.done', output_index=state['index'], item_id=item['id'], input=item['input'])
            else:
                yield self.event('response.function_call_arguments.done', output_index=state['index'], item_id=item['id'], name=item['name'], arguments=item['arguments'])
        if self.message:
            index, item = self.message; part = item['content'][0]
            yield self.event('response.output_text.done', output_index=index, item_id=item['id'], content_index=0, text=part['text'], logprobs=[])
            yield self.event('response.content_part.done', output_index=index, item_id=item['id'], content_index=0, part=part)
        incomplete = self.finish_reason in ('length', 'content_filter')
        for index, item in enumerate(self.output):
            item['status'] = 'incomplete' if incomplete else 'completed'
            yield self.event('response.output_item.done', output_index=index, item=item)
        self.response['status'] = 'incomplete' if incomplete else 'completed'
        if incomplete:
            self.response['incomplete_details'] = {'reason': 'max_output_tokens' if self.finish_reason == 'length' else 'content_filter'}
        yield self.event('response.incomplete' if incomplete else 'response.completed', response=self.response)


def from_chat(payload, model, custom_tools):
    state = ResponseStream(model, custom_tools)
    choices = payload.get('choices') or []
    converted = {'usage': payload.get('usage'), 'choices': []}
    if choices:
        message = copy.deepcopy(choices[0].get('message') or {})
        for index, call in enumerate(message.get('tool_calls') or []):
            call['index'] = index
        converted['choices'] = [{'delta': message, 'finish_reason': choices[0].get('finish_reason')}]
    list(state.feed(converted)); list(state.finish())
    return state.response
