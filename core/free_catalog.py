"""Small, fixed-source public catalog. No local instance data or credentials."""
import copy
from http.client import HTTPException
import ipaddress
import json
import re
import threading
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

CATALOG_URL = 'https://raw.githubusercontent.com/Emir-Aksoy/free-ai-gateway/master/docs/free-api-catalog.json'
MAX_BYTES = 100 * 1024

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Catalog redirects are not accepted')

# Only a fixed public URL is fetched. Never reuse ambient auth, cookies or proxies.
def urlopen(request, timeout):
    return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)

def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ValueError('Invalid catalog text')
    return value

def _url(value):
    _text(value,500)
    try:
        url=urlsplit(value);host=url.hostname or ''
        if url.scheme!='https' or url.username or url.password or url.port not in (None,443) or url.query or url.fragment or '.' not in host or host.endswith(('.local','.localhost','.internal')):
            raise ValueError('Invalid public URL')
        try:
            ip=ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[a-zA-Z0-9.-]+',host):raise ValueError('Invalid hostname')
        else:
            if not ip.is_global:raise ValueError('Invalid public address')
    except (TypeError, ValueError):
        raise ValueError('Invalid catalog URL') from None
    return value

def _date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('Invalid date')
    if date.fromisoformat(value)>datetime.now(timezone.utc).date():raise ValueError('Future catalog date')
    return value

def validate_catalog(value):
    if not isinstance(value,dict) or set(value)!={'schema','updated_at','providers'} or type(value['schema']) is not int or value['schema']!=1:raise ValueError('Invalid catalog schema')
    if len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())>MAX_BYTES:raise ValueError('Catalog too large')
    _date(value['updated_at'])
    if not isinstance(value['providers'],list) or not 1<=len(value['providers'])<=30:raise ValueError('Invalid providers')
    ids=set()
    for row in value['providers']:
        fields={'id','title','base_url','env_var','free_kind','models','summary','limits','sources','checked_at'}
        if not isinstance(row,dict) or set(row)!=fields:raise ValueError('Invalid provider fields')
        ident=_text(row['id'],32)
        if not re.fullmatch('[a-z][a-z0-9_-]{0,31}',ident) or ident in ids:raise ValueError('Invalid provider id')
        ids.add(ident);_text(row['title'],80);_url(row['base_url']);_date(row['checked_at'])
        if row['checked_at']>value['updated_at']:raise ValueError('Inconsistent check date')
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,63}',_text(row['env_var'],64)):raise ValueError('Invalid variable')
        if row['free_kind'] not in ('free_tier','trial','unknown'):raise ValueError('Invalid free kind')
        if not isinstance(row['models'],list) or len(row['models'])>30:raise ValueError('Invalid models')
        for model in row['models']:
            if any(c.isspace() for c in _text(model,160)):raise ValueError('Invalid model id')
        for key in ('summary','limits'):
            if not isinstance(row[key],dict) or set(row[key])!={'zh-CN','en'}:raise ValueError('Invalid translation')
            for text in row[key].values():_text(text,600)
        if not isinstance(row['sources'],list) or not 1<=len(row['sources'])<=5:raise ValueError('Missing sources')
        for source in row['sources']:
            if not isinstance(source,dict) or set(source)!={'title','url'}:raise ValueError('Invalid source')
            _text(source['title'],80);_url(source['url'])
    return copy.deepcopy(value)

FETCH_BUDGET_SECONDS = 9

def _fetch_catalog():
    request=Request(CATALOG_URL,headers={'Accept':'application/json','User-Agent':'free-ai-gateway/1.3'})
    with urlopen(request,timeout=8) as response:
        raw=response.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('Catalog too large')
    return validate_catalog(json.loads(raw))

def load_catalog():
    checked=datetime.now(timezone.utc).isoformat(timespec='seconds')
    result=[]
    def fetch():
        try:
            result.append(_fetch_catalog())
        except (OSError,ValueError,TypeError,HTTPException,RecursionError):
            pass
    # Socket timeouts apply per address/read. Bound DNS, address retries and slow
    # bodies together. This command runs in a short-lived process; a stalled
    # daemon worker cannot delay process exit or leave a resident service.
    worker=threading.Thread(target=fetch,daemon=True,name='public-catalog')
    worker.start();worker.join(FETCH_BUDGET_SECONDS)
    if result:
        return {'ok':True,'catalog':result[0],'source':'github','checked_at':checked,'warning':None}
    return {'ok':True,'catalog':copy.deepcopy(BUNDLED_CATALOG),'source':'bundled','checked_at':checked,'warning':'catalog_unavailable'}

# Generated from docs/free-api-catalog.json; the unit suite enforces byte-equivalent data.
BUNDLED_CATALOG = json.loads('{\n  "schema": 1,\n  "updated_at": "2026-09-06",\n  "providers": [\n    {\n      "id": "openrouter",\n      "title": "OpenRouter",\n      "base_url": "https://openrouter.ai/api/v1",\n      "env_var": "OPENROUTER_API_KEY",\n      "free_kind": "free_tier",\n      "models": [],\n      "summary": {\n        "zh-CN": "仅选择官方标为免费且当前可用的模型变体；一般以 :free 结尾。模型名单经常变化，请在扫描页查询，不把所有模型当免费。",\n        "en": "Choose currently available free variants, typically ending in :free. Discover current IDs in Scan; not every model is free."\n      },\n      "limits": {\n        "zh-CN": "免费请求有平台与上游限制；账号余额/购买历史可能影响可用额度，以当前账号页面为准。",\n        "en": "Platform and upstream limits apply. Account balance and purchase history may affect access; check your account."\n      },\n      "sources": [\n        {\n          "title": "Free variants",\n          "url": "https://openrouter.ai/docs/guides/routing/model-variants/free"\n        },\n        {\n          "title": "Limits",\n          "url": "https://openrouter.ai/docs/api_reference/limits"\n        },\n        {\n          "title": "Quickstart",\n          "url": "https://openrouter.ai/docs/quickstart"\n        }\n      ],\n      "checked_at": "2026-09-06"\n    },\n    {\n      "id": "groq",\n      "title": "Groq",\n      "base_url": "https://api.groq.com/openai/v1",\n      "env_var": "GROQ_API_KEY",\n      "free_kind": "free_tier",\n      "models": [\n        "openai/gpt-oss-20b",\n        "openai/gpt-oss-120b"\n      ],\n      "summary": {\n        "zh-CN": "官方提供 Free Plan。示例模型来自免费限额表，接入前仍需查询当前目录及账号权限。",\n        "en": "The official Free Plan lists these example models. Recheck the live catalog and account access before use."\n      },\n      "limits": {\n        "zh-CN": "按组织和模型限制 RPM、RPD、TPM 等，任一维度先用尽都会限流。账号控制台为准。",\n        "en": "Organization/model RPM, RPD and token limits all apply. The first exhausted limit blocks requests; check your console."\n      },\n      "sources": [\n        {\n          "title": "Free plan limits",\n          "url": "https://console.groq.com/docs/rate-limits"\n        },\n        {\n          "title": "OpenAI compatibility",\n          "url": "https://console.groq.com/docs/openai"\n        }\n      ],\n      "checked_at": "2026-09-06"\n    },\n    {\n      "id": "gemini",\n      "title": "Google Gemini",\n      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",\n      "env_var": "GEMINI_API_KEY",\n      "free_kind": "free_tier",\n      "models": [\n        "gemini-2.5-flash",\n        "gemini-2.5-flash-lite"\n      ],\n      "summary": {\n        "zh-CN": "仅部分模型提供免费层；示例来自官方定价表。付费层、音视频/图片模型与附加工具可能采用不同定价。",\n        "en": "Only selected models offer a free tier. Examples come from official pricing; paid tiers, media models and extra tools differ."\n      },\n      "limits": {\n        "zh-CN": "额度、地区和账号资格以 AI Studio 为准。免费层数据使用政策与付费层可能不同。",\n        "en": "Limits, regions and eligibility depend on AI Studio. Free-tier data-use policies may differ from paid tiers."\n      },\n      "sources": [\n        {\n          "title": "Pricing",\n          "url": "https://ai.google.dev/gemini-api/docs/pricing"\n        },\n        {\n          "title": "OpenAI compatibility",\n          "url": "https://ai.google.dev/gemini-api/docs/openai"\n        }\n      ],\n      "checked_at": "2026-09-06"\n    },\n    {\n      "id": "cerebras",\n      "title": "Cerebras",\n      "base_url": "https://api.cerebras.ai/v1",\n      "env_var": "CEREBRAS_API_KEY",\n      "free_kind": "trial",\n      "models": [],\n      "summary": {\n        "zh-CN": "限时试用，不是持续免费 API。官方当前说明：验证付款方式后赠送5美元，30天到期。不要把试用模型登记为永久免费。",\n        "en": "A limited trial, not a recurring free API. Current docs describe $5 after payment-method verification, expiring after 30 days."\n      },\n      "limits": {\n        "zh-CN": "需要验证付款方式；额度用完或到期后需要购买额度。此项作为免费政策变化提醒保留。",\n        "en": "A verified payment method is required. After credit expiry/exhaustion, purchased credits are needed. Kept as a policy-change notice."\n      },\n      "sources": [\n        {\n          "title": "Trial and rate limits",\n          "url": "https://inference-docs.cerebras.ai/support/rate-limits"\n        },\n        {\n          "title": "OpenAI compatibility",\n          "url": "https://inference-docs.cerebras.ai/resources/openai"\n        }\n      ],\n      "checked_at": "2026-09-06"\n    }\n  ]\n}')
