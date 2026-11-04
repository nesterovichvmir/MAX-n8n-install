#!/usr/bin/env python3
"""Configure n8n from the public API. No secrets are written to logs."""
import getpass, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def read_env(path):
    out = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        s = raw.strip()
        if not s or s.startswith('#') or '=' not in s: continue
        k, v = s.split('=', 1); v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            quote = v[0]
            v = v[1:-1]
            if quote == "'":
                v = v.replace("\\'", "'")
        out[k.strip()] = v
    return out

def die(msg):
    print(f"\nОШИБКА: {msg}", file=sys.stderr); sys.exit(1)

def request(url, method='GET', data=None, headers=None, timeout=45):
    h = {'Accept':'application/json'}
    if headers: h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw: return r.status, {}
            try: return r.status, json.loads(raw.decode('utf-8'))
            except Exception: return r.status, raw.decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try: detail = json.loads(raw)
        except Exception: detail = raw[:1000]
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from None
    except Exception as e:
        raise RuntimeError(f"Не удалось выполнить {method} {url}: {e}") from None

def unwrap(obj):
    if isinstance(obj, dict) and isinstance(obj.get('data'), dict): return obj['data']
    return obj

def api(base, key, path, method='GET', data=None):
    return unwrap(request(base + path, method, data, {'X-N8N-API-KEY': key})[1])

def items_from(obj):
    if isinstance(obj, list): return obj
    if isinstance(obj, dict):
        for k in ('data','items','credentials','workflows','dataTables'):
            if isinstance(obj.get(k), list): return obj[k]
    return []

def wait_n8n(base):
    for _ in range(30):
        try:
            request(base.replace('/api/v1','') + '/healthz', timeout=5); return
        except Exception: time.sleep(2)

def credential_schema(base, key, typ):
    return api(base, key, '/credentials/schema/' + urllib.parse.quote(typ, safe=''))

def props_of(schema):
    if not isinstance(schema, dict): return {}
    return schema.get('properties') or schema.get('data', {}).get('properties') or {}

def make_credential(base, key, name, typ, data):
    existing = items_from(api(base, key, '/credentials?limit=250'))
    for c in existing:
        if c.get('name') == name and c.get('type') == typ:
            return str(c['id'])
    result = api(base, key, '/credentials', 'POST', {'name':name, 'type':typ, 'data':data})
    if not isinstance(result, dict) or not result.get('id'):
        die(f"n8n не вернул ID для Credentials «{name}». Ответ API: {result}")
    return str(result['id'])

def get_or_create_table(base, key):
    tables = items_from(api(base, key, '/data-tables?limit=250'))
    table = next((x for x in tables if x.get('name') == 'report_recipients'), None)
    if table is None:
        table = api(base, key, '/data-tables', 'POST', {'name':'report_recipients','columns':[]})
    table = unwrap(table)
    tid = table.get('id') if isinstance(table, dict) else None
    if not tid: die(f"Не удалось создать таблицу report_recipients. Ответ: {table}")
    # Fetch current columns, then add only missing columns. API is intentionally additive.
    cols_obj = api(base, key, f'/data-tables/{tid}/columns')
    cols = items_from(cols_obj)
    known = {c.get('name') for c in cols}
    for name, typ in [('fio','string'), ('max_user_id','number')]:
        if name not in known:
            api(base, key, f'/data-tables/{tid}/columns', 'POST', {'name':name, 'type':typ})
    # Confirm resulting structure before importing workflow.
    cols = items_from(api(base, key, f'/data-tables/{tid}/columns'))
    got = {c.get('name'):c.get('type') for c in cols}
    if got.get('fio') != 'string' or got.get('max_user_id') != 'number':
        die(f"Таблица создана, но структура колонок отличается от ожидаемой: {got}")
    return str(tid)

def patch_workflow(tid, cred_sql, cred_max, cred_header, cfg):
    w = json.loads((ROOT/'workflow.template.json').read_text(encoding='utf-8'))
    w['name'] = 'Report СЭМД'
    w.pop('id', None); w.pop('versionId', None); w.pop('meta', None); w.pop('tags', None)
    w['active'] = False
    for n in w['nodes']:
        name = n['name']
        if name in ('Microsoft SQL 9:00','Microsoft SQL1 15:00'):
            n['credentials'] = {'microsoftSql': {'id':cred_sql, 'name':'Microsoft SQL account'}}
        elif name in ('Max Message','Max Message2','Max Message3','Max Message4'):
            n['credentials'] = {'maxApi': {'id':cred_max, 'name':'Max account (SEMD)'}}
        elif name in ('HTTP Request','HTTP Request1'):
            n['credentials'] = {'httpHeaderAuth': {'id':cred_header, 'name':'Header Auth account'}}
        if name in ('Get row(s)','Get row(s)1','Update row(s)'):
            n['parameters']['dataTableId'] = {'__rl':True, 'value':tid, 'mode':'list', 'cachedResultName':'report_recipients'}
        if name == 'Update row(s)':
            # Keep the original operation and expressions, but align its editor schema
            # with the intentionally minimal table (fio + max_user_id).
            n['parameters']['columns']['schema'] = [
                {'id':'fio','displayName':'fio','required':False,'defaultMatch':False,'display':True,'type':'string','readOnly':False,'removed':False},
                {'id':'max_user_id','displayName':'max_user_id','required':False,'defaultMatch':False,'display':True,'type':'number','readOnly':False,'removed':False},
            ]
        if name == 'HTTP Request':
            n['parameters']['jsonBody'] = json.dumps({
                'url': f"https://{cfg['DOMAIN']}/webhook/max-semd-bot-message",
                'update_types':['message_created']
            }, ensure_ascii=False, indent=2)
        if name == 'Max Message4':
            mode = cfg['REPORT_TARGET_TYPE']
            if mode == 'none':
                n['disabled'] = True
            elif mode == 'chat':
                n['disabled'] = False
                n['parameters']['sendTo'] = 'chat'
                n['parameters']['chatId'] = str(cfg['REPORT_TARGET_ID'])
                n['parameters'].pop('userId', None)
            elif mode == 'user':
                n['disabled'] = False
                n['parameters']['sendTo'] = 'user'
                n['parameters']['userId'] = str(cfg['REPORT_TARGET_ID'])
                n['parameters'].pop('chatId', None)
    # Preserve all original node JavaScript and expressions; only integration references above are changed.
    return {k:w[k] for k in ('name','nodes','connections','settings') if k in w}

def main():
    cfg = read_env(ROOT/'.env')
    for k in ('DOMAIN','SQL_SERVER','SQL_DATABASE','SQL_USER','SQL_PASSWORD','MAX_TOKEN'):
        if not cfg.get(k) or cfg[k] == 'CHANGE_ME': die(f"Заполните {k} в .env")
    cfg['REPORT_TARGET_TYPE'] = cfg.get('REPORT_TARGET_TYPE') or 'none'
    cfg['REPORT_TARGET_ID'] = cfg.get('REPORT_TARGET_ID','')
    if cfg['REPORT_TARGET_TYPE'] not in ('none','chat','user'): die('REPORT_TARGET_TYPE должен быть none/chat/user')
    if cfg['REPORT_TARGET_TYPE'] != 'none' and not cfg['REPORT_TARGET_ID']:
        print('ID адресата общего отчёта пуст — устанавливаю режим none.')
        cfg['REPORT_TARGET_TYPE'] = 'none'
    base = f"https://{cfg['DOMAIN']}/api/v1"
    print('\nВведите API Key, созданный в Settings → n8n API. Ввод скрыт.')
    key = getpass.getpass('n8n API Key: ').strip()
    if not key: die('API Key не введён.')
    try:
        api(base, key, '/workflows?limit=1')
    except Exception as e: die(f"API Key не прошёл проверку. Проверьте ключ и права доступа. Детали: {e}")
    print('[1/8] Проверка n8n API — OK')

    # Install exactly the pinned community package. Restart n8n before reading its credential schema.
    packages = items_from(api(base, key, '/community-packages'))
    installed = next((p for p in packages if p.get('packageName') == 'n8n-nodes-max'), None)
    if not installed or installed.get('installedVersion') != '1.1.2':
        api(base, key, '/community-packages', 'POST', {'name':'n8n-nodes-max','version':'1.1.2'})
        print('[2/8] Установка n8n-nodes-max@1.1.2 выполнена. Перезапуск n8n...')
        request('http://127.0.0.1:5678/rest/owner', timeout=5) if False else None
        # Restart through Docker CLI, then wait for API to return.
        os.system('docker restart n8n >/dev/null')
        wait_n8n(base)
        # API key remains valid after a normal container restart.
        api(base, key, '/workflows?limit=1')
    else:
        print('[2/8] n8n-nodes-max@1.1.2 уже установлен')

    # Use the installed credential schemas to avoid guessing credential field names.
    sql_schema = credential_schema(base, key, 'microsoftSql')
    max_schema = credential_schema(base, key, 'maxApi')
    header_schema = credential_schema(base, key, 'httpHeaderAuth')
    sql_props = props_of(sql_schema); max_props = props_of(max_schema); hdr_props = props_of(header_schema)
    if not sql_props: die(f"Не удалось получить схему microsoftSql: {sql_schema}")
    if not max_props: die(f"Не удалось получить схему maxApi после установки community node: {max_schema}")
    sql_data = {'server':cfg['SQL_SERVER'], 'database':cfg['SQL_DATABASE'], 'user':cfg['SQL_USER'],
                'password':cfg['SQL_PASSWORD'], 'port':int(cfg.get('SQL_PORT') or 1433),
                'tls':cfg.get('SQL_TLS','true').lower() == 'true',
                'allowUnauthorizedCerts':cfg.get('SQL_ALLOW_UNAUTHORIZED_CERTS','false').lower() == 'true', 'connectTimeout':15000, 'requestTimeout':15000, 'tdsVersion':'7_4'}
    sql_data = {k:v for k,v in sql_data.items() if k in sql_props}
    token_fields = [k for k,v in max_props.items() if 'token' in k.lower() or 'token' in str(v.get('displayName','')).lower()]
    if not token_fields:
        token_fields = [k for k,v in max_props.items() if k.lower() in ('accesskey','apikey','key') or 'api key' in str(v.get('displayName','')).lower()]
    if len(token_fields) != 1: die(f"Не удалось однозначно определить поле токена MAX по схеме Credentials: {list(max_props)}")
    max_data = {token_fields[0]:cfg['MAX_TOKEN']}
    # Keep only known schema keys and required values.
    if 'name' not in hdr_props or 'value' not in hdr_props: die(f"Неожиданная схема httpHeaderAuth: {header_schema}")
    header_data = {'name':'Authorization', 'value':cfg['MAX_TOKEN']}
    cred_sql = make_credential(base,key,'Microsoft SQL account','microsoftSql',sql_data)
    cred_max = make_credential(base,key,'Max account (SEMD)','maxApi',max_data)
    cred_header = make_credential(base,key,'Header Auth account','httpHeaderAuth',header_data)
    print('[3/8] Credentials SQL, MAX и Header Auth созданы/проверены')

    tid = get_or_create_table(base,key)
    print('[4/8] Пустая таблица report_recipients с fio и max_user_id создана/проверена')
    wf = patch_workflow(tid,cred_sql,cred_max,cred_header,cfg)
    existing = items_from(api(base,key,'/workflows?limit=250'))
    old = next((x for x in existing if x.get('name') == 'Report СЭМД'),None)
    if old and old.get('id'):
        wid = str(old['id'])
        api(base,key,f'/workflows/{wid}','PUT',wf)
        print('[5/8] Найден существующий workflow «Report СЭМД» — обновлён конфигурацией пакета')
    else:
        created = api(base,key,'/workflows','POST',wf)
        if not isinstance(created,dict) or not created.get('id'): die(f"n8n не вернул ID workflow: {created}")
        wid = str(created['id'])
        print('[5/8] Workflow «Report СЭМД» импортирован')

    # MAX allows one callback URL per subscription; preserve the source workflow's two webhook paths.
    max_headers = {'Authorization':cfg['MAX_TOKEN']}
    callbacks = [
        (f"https://{cfg['DOMAIN']}/webhook/max-bot-started-semd", ['bot_started']),
        (f"https://{cfg['DOMAIN']}/webhook/max-semd-bot-message", ['message_created']),
    ]
    for url, types in callbacks:
        _, result = request('https://platform-api2.max.ru/subscriptions','POST',
                            {'url':url,'update_types':types},max_headers,timeout=30)
        if isinstance(result,dict) and result.get('success') is False:
            die(f"MAX отклонил регистрацию webhook {url}: {result.get('message','без пояснения')}")
    print('[6/8] Две подписки MAX зарегистрированы: bot_started и message_created')

    # Activate only after callbacks, credentials and table are ready.
    api(base,key,f'/workflows/{wid}/activate','POST',{})
    print('[7/8] Workflow активирован')
    # Verify active workflow exists and table columns remain correct.
    detail = api(base,key,f'/workflows/{wid}')
    if not isinstance(detail,dict) or not detail.get('id'): die('Не удалось проверить импортированный workflow через API.')
    cols = items_from(api(base,key,f'/data-tables/{tid}/columns'))
    if not {'fio','max_user_id'}.issubset({c.get('name') for c in cols}): die('Проверка колонок таблицы не пройдена.')
    print('[8/8] Контрольная проверка — OK')

    print('\n' + '='*64)
    print('УСТАНОВКА ЗАВЕРШЕНА')
    print(f'n8n: https://{cfg["DOMAIN"]}')
    print('Workflow: Report СЭМД (активен)')
    print('Таблица: report_recipients (создана пустой)')
    print('='*64)
    print('\nЧТО ОСТАЛОСЬ СДЕЛАТЬ АДМИНИСТРАТОРУ:')
    print('1. В n8n откройте Data tables → report_recipients.')
    print('2. Добавьте в колонку fio ФИО сотрудников в том же формате,')
    print('   в котором они будут отправлять его боту (например, Иванов И.И.).')
    print('3. Попросите каждого сотрудника открыть бота MAX и нажать «Начать».')
    print('4. Сотрудник отправляет ФИО. Бот сам получает MAX user_id,')
    print('   проверяет ФИО и записывает max_user_id в таблицу.')
    print('\nКолонку max_user_id вручную заполнять НЕ НУЖНО.')
    print('Проверьте, что бот добавлен/доступен сотрудникам и токен действителен.')
    print('\nРезервная копия: сохраните .env и N8N_ENCRYPTION_KEY отдельно и безопасно.')

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: die('Установка прервана пользователем.')
    except Exception as e: die(str(e))
