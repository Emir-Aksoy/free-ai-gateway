"""Private native-to-native Provider transfer. Never return secrets in preview/apply."""
import copy
import hashlib
import json
import os
import stat
import uuid
import unicodedata
from contextlib import contextmanager

from core.provider_config import SLUG, validate_definition, validate_free_models
from core.registry import BUILTIN_PROVIDERS

MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_FIELDS = {'name', 'base_url', 'env_var', 'custom', 'free_models', 'allow_local_http', 'key', 'quota'}
SAFE_ERROR = 'Provider 同步失败，请重新检查配置后重试'


class SyncError(Exception):
    def __init__(self, code='invalid_input'):
        self.code = code
        super().__init__(SAFE_ERROR)


def _names(names):
    if not isinstance(names, list) or not 1 <= len(names) <= 100:
        raise SyncError()
    if any(not isinstance(name, str) or not SLUG.fullmatch(name) for name in names) or len(set(names)) != len(names):
        raise SyncError()
    return names


def _key(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096 or any(c.isspace() or unicodedata.category(c).startswith('C') or c in "\"'" for c in value):
        raise SyncError()
    return value


def _quota(value):
    if not isinstance(value, dict) or set(value) != {'daily', 'rpm'}:
        raise SyncError()
    if any(v is not None and (type(v) is not int or v < 0 or v > 2**53 - 1) for v in value.values()):
        raise SyncError()
    return dict(value)


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {'version', 'providers'} or type(bundle['version']) is not int or bundle['version'] != 1:
        raise SyncError()
    if len(json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise SyncError()
    items = bundle['providers']
    if not isinstance(items, list) or any(not isinstance(item, dict) or set(item) != _FIELDS for item in items):
        raise SyncError()
    _names([item['name'] for item in items])
    result = []
    for item in items:
        name = item['name']
        builtin = BUILTIN_PROVIDERS.get(name)
        if type(item['custom']) is not bool or item['custom'] != (builtin is None) or type(item['allow_local_http']) is not bool:
            raise SyncError()
        definition = validate_definition(name, dict(type='openai', base_url=item['base_url'], env_var=item['env_var'], env='env/' + name + '.env', free_models=item['free_models'], allow_local_http=item['allow_local_http']))
        if builtin and (item['base_url'] != builtin.base_url or item['env_var'] != builtin.env_var or item['allow_local_http']):
            raise SyncError()
        result.append(dict(item, base_url=definition['base_url'], free_models=definition['free_models'], key=_key(item['key']), quota=_quota(item['quota'])))
    return result


def _read(path, optional=False):
    # Missing is a stable state; unreadable, nonregular and dangling links fail closed.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional and not os.path.lexists(path):
            return None
        raise
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SyncError('bad_config')
        value = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(value) > MAX_BUNDLE_BYTES:
            raise SyncError('bad_config')
        return value


def _env_path(name, definition):
    from core.paths import BASE_DIR
    path = definition.get('env') or (BUILTIN_PROVIDERS[name].env_file if name in BUILTIN_PROVIDERS else None)
    if not isinstance(path, str) or not path:
        raise SyncError('bad_config')
    return os.path.join(BASE_DIR, path)


def _extract(data, variable):
    if data is None:
        return None
    for line in data.decode('utf-8').splitlines():
        line = line.strip()
        if line.startswith(variable + '='):
            value = line.split('=', 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def _snapshot(manage):
    from core.paths import CONFIG_FILE
    import yaml
    if os.path.islink(CONFIG_FILE):
        raise SyncError('bad_config')
    original = _read(CONFIG_FILE)
    config = yaml.safe_load(original)
    if not isinstance(config, dict) or not isinstance(config.get('gateway'), dict) or not isinstance(config['gateway'].get('modes'), dict):
        raise SyncError('bad_config')
    definitions = config.get('providers') or {}
    quotas = config.get('quota') or {}
    if not isinstance(definitions, dict) or not isinstance(quotas, dict):
        raise SyncError('bad_config')
    manage.apply_env_config(config)
    digest = hashlib.sha256()
    def add(value):
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    add(original)
    credentials = {}
    for name in sorted(set(BUILTIN_PROVIDERS) | set(definitions)):
        definition = definitions.get(name) or {}
        if not isinstance(definition, dict):
            raise SyncError('bad_config')
        path = _env_path(name, definition)
        data = _read(path, optional=True)
        add(name.encode('utf-8'))
        add(path.encode('utf-8'))
        add(b'missing' if data is None else b'present' + data)
        credentials[name] = data
    return config, original, credentials, digest.hexdigest()


def _existing(config, credentials, name):
    from core.registry import PROVIDERS
    definition = (config.get('providers') or {}).get(name) or {}
    if name not in PROVIDERS or definition.get('enabled') is False:
        return None
    cls = PROVIDERS[name]
    key = _extract(credentials.get(name), cls.env_var)
    if not key:
        return None
    limits = (config.get('quota') or {}).get(name) or {}
    return dict(name=name, base_url=cls.base_url, env_var=cls.env_var, custom=name not in BUILTIN_PROVIDERS, free_models=validate_free_models(definition.get('free_models', [])), allow_local_http=definition.get('allow_local_http', False), key=key, quota={field: limits.get(field) for field in ('daily', 'rpm')})


def _rows(items, config, credentials):
    known = set(BUILTIN_PROVIDERS) | set(config.get('providers') or {})
    return [{'name': item['name'], 'action': 'unchanged' if _existing(config, credentials, item['name']) == item else ('update' if item['name'] in known else 'add')} for item in items]


@contextmanager
def _private_env_dir():
    from core.paths import BASE_DIR
    path = os.path.join(BASE_DIR, 'env')
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise SyncError('write_failed')
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _apply(manage, items, config, original, credentials):
    from core.paths import CONFIG_FILE
    import yaml
    rows = _rows(items, config, credentials)
    changed = {row['name'] for row in rows if row['action'] != 'unchanged'}
    result = {'providers': [item['name'] for item in items], 'restarted': False, 'count': len(items)}
    if not changed:
        return result
    updated = copy.deepcopy(config)
    updated['providers'] = dict(updated.get('providers') or {})
    updated['quota'] = dict(updated.get('quota') or {})
    files = []
    for item in items:
        name = item['name']
        if name not in changed:
            continue
        filename = name + '-' + uuid.uuid4().hex + '.env'
        definition = dict(enabled=True, env='env/' + filename, free_models=item['free_models'])
        if item['custom']:
            definition.update(type='openai', base_url=item['base_url'], env_var=item['env_var'], allow_local_http=item['allow_local_http'])
        updated['providers'][name] = definition
        limits = dict(updated['quota'].get(name) or {})
        for field, value in item['quota'].items():
            if value is None:
                limits.pop(field, None)
            else:
                limits[field] = value
        updated['quota'][name] = limits
        files.append((filename, item['env_var'] + '=' + item['key'] + '\n'))
    text = original.decode('utf-8')
    for section in ('providers', 'quota'):
        text = manage.replace_top_level_section(text, section, yaml.safe_dump({section: updated[section]}, allow_unicode=True, sort_keys=False))
    if yaml.safe_load(text) != updated:
        raise SyncError('bad_config')
    service = manage.service_name()
    port = manage.gateway_port(service)
    active = manage.service_is_active(service)
    with _private_env_dir() as directory:
        backup = manage.backup_file(CONFIG_FILE)
        created = []
        try:
            for filename, content in files:
                fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                created.append(filename)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            manage.write_text_atomic(CONFIG_FILE, text)
            manage.apply_env_config(updated)
            if active:
                ok, _ = manage.restart_service(service, port)
                if not ok:
                    raise SyncError('restart_failed')
        except BaseException as exc:
            failures = manage.restore_files({CONFIG_FILE: backup})
            # A failed config restore may leave the new configuration installed.
            # Retain its private credentials so recovery never destroys a usable state.
            if not failures:
                for filename in created:
                    try:
                        os.unlink(filename, dir_fd=directory)
                    except OSError:
                        failures[filename] = True
            try:
                manage.apply_env_config()
            except Exception:
                failures['registry'] = True
            recovered = not active
            if active:
                try:
                    recovered, _ = manage.restart_service(service, port)
                except Exception:
                    recovered = False
            if not isinstance(exc, Exception):
                raise
            error = SyncError('rollback_failed' if failures or not recovered else 'write_failed')
            raise error from None
    result['restarted'] = active
    return result


def run(manage, action, params):
    """All public exceptions and audit fields are independent of incoming values."""
    count = 0
    try:
        expected = {'export': {'names'}, 'preview': {'bundle'}, 'apply': {'bundle', 'token'}}
        if action not in expected or not isinstance(params, dict) or set(params) != expected[action]:
            raise SyncError()
        with manage.management_lock('provider-sync ' + action):
            if action == 'export':
                names = _names(params['names'])
                config, _, credentials, _ = _snapshot(manage)
                items = [_existing(config, credentials, name) for name in names]
                if any(item is None for item in items):
                    raise SyncError('not_configured')
                bundle = {'version': 1, 'providers': items}
                validate_bundle(bundle)
                result = {'bundle': bundle}
                count = len(items)
            else:
                items = validate_bundle(params['bundle'])
                config, original, credentials, token = _snapshot(manage)
                count = len(items)
                if action == 'preview':
                    result = {'rows': _rows(items, config, credentials), 'token': token, 'count': count}
                else:
                    supplied = params['token']
                    if not isinstance(supplied, str) or supplied != token:
                        raise SyncError('stale_preview')
                    result = _apply(manage, items, config, original, credentials)
    except Exception as exc:
        manage.audit('provider-sync ' + action, False, count=count)
        code = exc.code if isinstance(exc, SyncError) else 'sync_failed'
        raise manage.ManageError(SAFE_ERROR, code) from None
    manage.audit('provider-sync ' + action, True, count=count)
    return result
