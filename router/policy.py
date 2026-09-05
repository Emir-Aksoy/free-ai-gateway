"""Task policy resolution and safe, structured failure classification."""
TASKS = ('fast','balanced','thinking','code','writing','agent')

def effective_policy(routing, task):
    result={'mode':'scored','use_latency':True,'use_success_rate':True}
    result.update({k:v for k,v in routing.items() if k in result})
    result.update((routing.get('tasks') or {}).get(task, {}))
    return result

def failure_category(error):
    status=getattr(error,'status',None)
    if status in (401,403) or getattr(error,'code',None)=='missing_key': return 'provider_auth'
    if status in (402,429): return 'rate_limited'
    if status in (400,404,405,413,415,422): return 'request_incompatible'
    return 'reliability'
