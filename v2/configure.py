#!/usr/bin/env python3
import getpass,json,os,re,secrets,socket,subprocess,sys,time,urllib.error,urllib.parse,urllib.request,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
BASE=os.environ.get("N8N_BASE_URL","").rstrip("/")
CONTAINER=os.environ.get("CONTAINER","n8n")
MODULES=os.environ.get("MODULES","").split()
CURRENT_MODULE=""

def die(msg):
    print("\nОШИБКА: "+str(msg),file=sys.stderr); raise SystemExit(1)
def ask(label,default=None,secret=False,required=True):
    scope=("["+CURRENT_MODULE+"] ") if CURRENT_MODULE else ""
    p=scope+label+(" ["+str(default)+"]" if default not in (None,"") else "")+": "
    v=getpass.getpass(p) if secret else input(p)
    v=v.strip() if v else (str(default) if default is not None else "")
    if required and not v: die("Не заполнено поле: "+label)
    return v
def request(url,method="GET",data=None,headers=None,timeout=45):
    h={"Accept":"application/json"}
    if headers: h.update(headers)
    body=None
    if data is not None: body=json.dumps(data,ensure_ascii=False).encode(); h["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=body,headers=h,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read()
            if not raw: return r.status,{}
            try: return r.status,json.loads(raw.decode())
            except Exception: return r.status,raw.decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8","replace")
        try: detail=json.loads(raw)
        except Exception: detail=raw[:500]
        raise RuntimeError("HTTP "+str(e.code)+" "+method+" "+url+": "+str(detail)) from None
    except Exception as e: raise RuntimeError("Ошибка запроса "+method+" "+url+": "+str(e)) from None
def unwrap(x): return x["data"] if isinstance(x,dict) and isinstance(x.get("data"),dict) else x
def items(x):
    if isinstance(x,list): return x
    if isinstance(x,dict):
        for k in ("data","items","credentials","workflows","dataTables","columns"):
            if isinstance(x.get(k),list): return x[k]
    return []
def api(key,path,method="GET",data=None):
    return unwrap(request(BASE+"/api/v1"+path,method,data,{"X-N8N-API-KEY":key})[1])
def wait_n8n(key):
    for _ in range(90):
        try: request(BASE+"/healthz",timeout=5); api(key,"/workflows?limit=1"); return
        except Exception: time.sleep(2)
    die("n8n не восстановился после перезапуска контейнера "+CONTAINER)
def workflow_names(key): return {str(w.get("name")) for w in items(api(key,"/workflows?limit=250"))}
def props(typ,key):
    s=api(key,"/credentials/schema/"+urllib.parse.quote(typ,safe=""))
    return s.get("properties") or s.get("data",{}).get("properties") or {}
def field_hint(p,*hints):
    matches=[k for k,v in p.items() if any(h in (k+" "+str(v.get("displayName",""))).lower() for h in hints)]
    if len(matches)==1: return matches[0]
    if len(matches)>1: die("Неоднозначное поле Credentials: "+str(matches))
    return None
def token_field(p,label):
    found=[k for k,v in p.items() if "token" in (k+" "+str(v.get("displayName",""))).lower()]
    if len(found)==1: return found[0]
    for k in ("accessToken","access_token","token","apiKey","key"):
        if k in p: return k
    die("Не удалось определить поле токена в Credentials "+label+": "+str(list(p)))
def credential(key,name,typ,data):
    old=items(api(key,"/credentials?limit=250"))
    for c in old:
        if c.get("name")==name:
            if c.get("type")!=typ: die("Credential "+name+" существует другого типа; он не изменён.")
            print("Использую существующий Credential без изменения: "+name); return str(c["id"])
    c=api(key,"/credentials","POST",{"name":name,"type":typ,"data":data})
    if not isinstance(c,dict) or not c.get("id"): die("Не удалось создать Credential "+name+": "+str(c))
    print("Создан Credential: "+name); return str(c["id"])
def ensure_packages(key,required):
    installed=items(api(key,"/community-packages")); restart=False
    for name,version in required:
        old=next((p for p in installed if p.get("packageName")==name),None)
        if old:
            got=str(old.get("installedVersion",""))
            if got!=version: die("Пакет "+name+" уже установлен в версии "+got+"; требуется "+version+". Автоматическое обновление запрещено.")
            if old.get("failedLoading"): die("Пакет "+name+" установлен, но n8n сообщает об ошибке загрузки. Установщик не будет импортировать workflow.")
        else:
            api(key,"/community-packages","POST",{"name":name,"version":version}); restart=True
            print("Установка community node: "+name+"@"+version)
    if restart:
        r=subprocess.run(["docker","restart",CONTAINER],capture_output=True,text=True,timeout=120)
        if r.returncode: die("Не удалось перезапустить n8n: "+r.stderr)
        wait_n8n(key)
        installed=items(api(key,"/community-packages"))
        for name,version in required:
            loaded=next((p for p in installed if p.get("packageName")==name),None)
            if not loaded or str(loaded.get("installedVersion",""))!=version or loaded.get("failedLoading"):
                die("После перезапуска n8n не подтвердил успешную загрузку "+name+"@"+version+". Workflow не импортирован.")
def prepare(path,name):
    w=json.loads(path.read_text(encoding="utf-8")); w["name"]=name; w.pop("active",None)
    for k in ("id","versionId","meta","tags","nodeGroups"): w.pop(k,None)
    for n in w.get("nodes",[]):
        n["id"]=str(uuid.uuid4()); n.pop("webhookId",None)
    return w
def import_activate(key,w):
    c=api(key,"/workflows","POST",w)
    if not isinstance(c,dict) or not c.get("id"): die("Не удалось импортировать workflow: "+str(c))
    wid=str(c["id"]); api(key,"/workflows/"+wid+"/activate","POST",{})
    d=api(key,"/workflows/"+wid)
    if not isinstance(d,dict) or d.get("active") is not True: die("n8n не подтвердил активацию "+w["name"])
    print("Импортирован и активирован workflow: "+w["name"])
def host_command(args,input_text=None):
    r=subprocess.run(args,input=input_text,capture_output=True,text=True)
    if r.returncode: die("Команда на хосте завершилась ошибкой: "+" ".join(args)+"; "+r.stderr.strip())
    return r.stdout.strip()
def prepare_vk_ssh():
    if os.geteuid()!=0: die("Для подготовки SSH-доступа к yt-dlp запустите установщик от root.")
    host_command(["apt-get","install","-y","openssh-server"])
    host_command(["systemctl","enable","--now","ssh"])
    pwfile=Path("/root/.n8n-vk-ssh-password")
    user_exists=subprocess.run(["id","n8n-vk"],capture_output=True).returncode==0
    if pwfile.exists():
        password=pwfile.read_text(encoding="utf-8").strip()
        if not password: die("Файл /root/.n8n-vk-ssh-password пуст.")
        if not user_exists:
            host_command(["useradd","--create-home","--shell","/bin/bash","n8n-vk"])
            host_command(["chpasswd"],"n8n-vk:"+password+"\n")
    else:
        if user_exists: die("Пользователь n8n-vk уже существует, но файл его пароля отсутствует. Пароль не будет сбрасываться.")
        host_command(["useradd","--create-home","--shell","/bin/bash","n8n-vk"])
        password=secrets.token_urlsafe(36)
        pwfile.write_text(password,encoding="utf-8"); pwfile.chmod(0o600)
        host_command(["chpasswd"],"n8n-vk:"+password+"\n")
    groups=host_command(["id","-nG","n8n-vk"]).split()
    if "sudo" in groups: die("Учётная запись n8n-vk входит в группу sudo; использование запрещено.")
    sshcfg=Path("/etc/ssh/sshd_config")
    config=sshcfg.read_text(encoding="utf-8")
    changed=False
    if not re.search(r"^Match\s+User\s+n8n-vk(?:\s|$)",config,re.M):
        backup=Path("/etc/ssh/sshd_config.max-n8n-v2.bak")
        if not backup.exists(): backup.write_text(config,encoding="utf-8"); backup.chmod(0o600)
        sshcfg.write_text(config.rstrip()+"\n\nMatch User n8n-vk\n  PasswordAuthentication yes\n",encoding="utf-8")
        try: host_command(["sshd","-t"])
        except SystemExit:
            sshcfg.write_text(config,encoding="utf-8")
            raise
        changed=True
    if changed: host_command(["systemctl","restart","ssh"])
    ytdlp=Path("/usr/local/bin/yt-dlp")
    ytdlp_ok=False
    if ytdlp.exists() and os.access(ytdlp,os.X_OK):
        test=subprocess.run([str(ytdlp),"--version"],capture_output=True,text=True,timeout=20)
        ytdlp_ok=(test.returncode==0 and bool(test.stdout.strip()))
    if not ytdlp_ok:
        tmp=Path("/usr/local/bin/yt-dlp.new")
        host_command(["curl","-fL","--retry","4","--retry-all-errors",
                      "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
                      "-o",str(tmp)])
        tmp.chmod(0o755)
        test=subprocess.run([str(tmp),"--version"],capture_output=True,text=True,timeout=30)
        if test.returncode or not test.stdout.strip():
            tmp.unlink(missing_ok=True)
            die("Не удалось проверить standalone yt-dlp_linux. Существующий yt-dlp не заменён.")
        tmp.replace(ytdlp)
        print("Установлен совместимый standalone yt-dlp: "+test.stdout.strip())
    else:
        print("Найден рабочий yt-dlp: "+test.stdout.strip())
    inspect='{{range .NetworkSettings.Networks}}{{println .Gateway}}{{end}}'
    gateways=host_command(["docker","inspect","-f",inspect,CONTAINER]).splitlines()
    host=next((x.strip() for x in gateways if x.strip()),"")
    if not host: host=host_command(["docker","network","inspect","bridge","-f",'{{(index .IPAM.Config 0).Gateway}}'])
    if not host: die("Не удалось определить Docker gateway для SSH.")
    js='const net=require("net");const s=net.createConnection({host:process.env.VK_SSH_HOST,port:22});const done=(c)=>{s.destroy();process.exit(c)};s.setTimeout(5000);s.on("connect",()=>done(0));s.on("timeout",()=>done(1));s.on("error",()=>done(1));'
    host_command(["docker","exec","-e","VK_SSH_HOST="+host,CONTAINER,"node","-e",js])
    return host,"n8n-vk",password
def install_vk(key):
    name="VK → MAX (v2)"
    if name in workflow_names(key): die("Workflow "+name+" уже существует; перезапись запрещена.")
    print()
    print("Сейчас будут запрошены настройки сообщества ВКонтакте и группового чата MAX.")
    print("До продолжения добавьте MAX-бота в нужный групповой чат и назначьте его администратором.")
    print("Модуль VK → MAX отправляет публикации только в групповой чат.")
    vk_token=ask("Токен сообщества VK (скрытый ввод)",secret=True)
    max_token=ask("Токен MAX-бота (скрытый ввод)",secret=True)
    chat_id=ask("Числовой ID группового чата MAX (обычно отрицательное число)")
    if not re.fullmatch(r"-?[0-9]+",chat_id):
        die("ID группового чата MAX должен быть числовым.")
    vp=props("vkApi",key); hp=props("httpHeaderAuth",key); sp=props("sshPassword",key)
    if not vp or not hp or not sp: die("Не получены схемы Credentials vkApi/httpHeaderAuth/sshPassword.")
    if not {"name","value"}.issubset(hp): die("Неожиданная схема httpHeaderAuth.")
    if not {"host","username","password","port"}.issubset(sp): die("Неожиданная схема sshPassword.")
    vk_data={token_field(vp,"VK"):vk_token}
    confirmation_key=field_hint(vp,"confirmation")
    if not confirmation_key: die("В схеме VK Community API не найдено обязательное поле Confirmation String.")
    vk_data[confirmation_key]=ask("Строка подтверждения Callback API VK (скрытый ввод)",secret=True)
    version_key=field_hint(vp,"api version","version")
    if version_key: vk_data[version_key]="5.199"
    secret_key=field_hint(vp,"secret key")
    if secret_key:
        secret_value=ask("Секретный ключ Callback API VK (если не задан — Enter)",secret=True,required=False)
        if secret_value: vk_data[secret_key]=secret_value
    host,user,password=prepare_vk_ssh()
    ssh={"host":host,"username":user,"password":password,"port":22}
    cv=credential(key,"V2 - VK Community","vkApi",vk_data)
    ch=credential(key,"V2 - MAX Header Auth (VK)","httpHeaderAuth",{"name":"Authorization","value":max_token})
    cs=credential(key,"V2 - SSH yt-dlp","sshPassword",ssh)
    w=prepare(ROOT/"workflow.vk.template.json",name)
    for n in w["nodes"]:
        nm=n.get("name")
        if nm=="VK Community Trigger": n["credentials"]={"vkApi":{"id":cv,"name":"V2 - VK Community"}}
        elif nm in ("Get Upload URL","Get Upload URL1","HTTP Request2","HTTP Request4","HTTP Request5"):
            n["credentials"]={"httpHeaderAuth":{"id":ch,"name":"V2 - MAX Header Auth (VK)"}}
        elif nm=="Execute a command": n["credentials"]={"sshPassword":{"id":cs,"name":"V2 - SSH yt-dlp"}}
        if nm in ("HTTP Request2","HTTP Request4","HTTP Request5"):
            n["parameters"]["url"]="https://platform-api2.max.ru/messages?chat_id="+chat_id
        if nm=="HTTP Request5":
            n["parameters"]["jsonBody"]="={{ JSON.stringify({ text: $json.object.text || '' }) }}"
    import_activate(key,w)
    print("Следующий шаг: откройте VK Community Trigger в workflow и скопируйте Production URL.")
    print("В VK добавьте отдельный Callback API сервер, вставьте URL и нажмите Подтвердить.")
    print("ОБЯЗАТЕЛЬНО: Типы событий → Записи на стене → Добавление (wall_post_new). Без этой галочки VK не отправляет новые публикации.")
    print("Проверьте, что MAX-бот добавлен в указанный групповой чат и ему разрешено участие в группах.")
def sql_check(host,port):
    try:
        with socket.create_connection((host,int(port)),timeout=6): pass
    except Exception as e: die("Нет TCP-доступа к SQL Server "+host+":"+str(port)+": "+str(e))
def table_id(key):
    t=next((x for x in items(api(key,"/data-tables?limit=250")) if x.get("name")=="report_recipients"),None)
    if t is None: t=unwrap(api(key,"/data-tables","POST",{"name":"report_recipients","columns":[{"name":"fio","type":"string"},{"name":"max_user_id","type":"number"}]}))
    tid=t.get("id") if isinstance(t,dict) else None
    if not tid: die("Не удалось создать/найти report_recipients: "+str(t))
    cols=items(api(key,"/data-tables/"+str(tid)+"/columns")); known={x.get("name") for x in cols}
    for name,typ in (("fio","string"),("max_user_id","number")):
        if name not in known: api(key,"/data-tables/"+str(tid)+"/columns","POST",{"name":name,"type":typ})
    cols=items(api(key,"/data-tables/"+str(tid)+"/columns")); got={x.get("name"):x.get("type") for x in cols}
    if got.get("fio")!="string" or got.get("max_user_id")!="number": die("Неверная схема report_recipients: "+str(got))
    return str(tid)
def callbacks(domain,token):
    h={"Authorization":token}; url="https://platform-api2.max.ru/subscriptions"
    _,r=request(url,headers=h,timeout=30)
    current=r.get("subscriptions",[]) if isinstance(r,dict) else []
    callback="https://"+domain+"/webhook/max-bot-started-semd"
    old=next((x for x in current if x.get("url")==callback),None)
    if old and set(old.get("update_types") or [])!={"bot_started"}:
        die("Подписка MAX для "+callback+" уже существует с другим набором событий. Она не изменена; проверьте её в настройках MAX.")
    if not old:
        _,r=request(url,"POST",{"url":callback,"update_types":["bot_started"]},h,30)
        if isinstance(r,dict) and r.get("success") is False: die("MAX отклонил webhook "+callback+": "+str(r))
    _,r=request(url,headers=h,timeout=30)
    got=r.get("subscriptions",[]) if isinstance(r,dict) else []
    x=next((x for x in got if x.get("url")==callback),None)
    if not x or set(x.get("update_types") or [])!={"bot_started"}:
        die("MAX не подтвердил webhook "+callback)
def install_semd(key):
    name="Report СЭМД"
    if name in workflow_names(key): die("Workflow Report СЭМД уже существует. v2 его не перезаписывает; если СЭМД уже работает, выберите только VK → MAX.")
    print("\nСейчас будут запрошены параметры подключения к SQL Server, MAX-боту и получателю отчётов.")
    domain=urllib.parse.urlparse(BASE).hostname
    host=ask("Имя/IP SQL Server"); port=ask("TCP-порт SQL Server","1433")
    if not port.isdigit() or not 1<=int(port)<=65535: die("Некорректный TCP-порт.")
    db=ask("База данных МИС"); user=ask("SQL логин"); pwd=ask("SQL пароль (скрытый ввод)",secret=True)
    tls=ask("Использовать TLS для SQL? true/false","true").lower()
    if tls not in ("true","false"): die("Допустимы true/false.")
    allow="false"
    if tls=="true":
        allow=ask("Разрешить недоверенный сертификат SQL? true/false","false").lower()
        if allow not in ("true","false"): die("Допустимы true/false.")
    token=ask("Токен MAX-бота СЭМД (скрытый ввод)",secret=True)
    mode=ask("Общий отчёт: none/chat/user","none").lower()
    if mode not in ("none","chat","user"): die("Допустимы none, chat, user.")
    target=ask("ID чата/пользователя MAX",required=False) if mode!="none" else ""
    if mode!="none" and not target: print("ID не указан: общий отчёт отключён."); mode="none"
    if target and not re.fullmatch(r"-?[0-9]+",target): die("ID чата/пользователя MAX должен быть числовым.")
    if mode=="user" and target.startswith("-"): die("Для режима user нужен положительный ID пользователя MAX.")
    sql_check(host,port)
    sp=props("microsoftSql",key); mp=props("maxApi",key); hp=props("httpHeaderAuth",key)
    if not sp or not mp or not hp: die("Не получены схемы SQL/MAX Credentials.")
    values={"server":host,"database":db,"user":user,"password":pwd,"port":int(port),
            "tls":tls=="true","allowUnauthorizedCerts":allow=="true",
            "connectTimeout":15000,"requestTimeout":15000,"tdsVersion":"7_4"}
    sd={k:v for k,v in values.items() if k in sp}
    if not {"server","database","user","password"}.issubset(sd): die("Неожиданная схема microsoftSql.")
    if not {"name","value"}.issubset(hp): die("Неожиданная схема httpHeaderAuth.")
    csql=credential(key,"V2 - Microsoft SQL (СЭМД)","microsoftSql",sd)
    cmax=credential(key,"V2 - MAX API (СЭМД)","maxApi",{token_field(mp,"MAX"):token})
    ch=credential(key,"V2 - MAX Header Auth (СЭМД)","httpHeaderAuth",{"name":"Authorization","value":token})
    tid=table_id(key); w=prepare(ROOT/"workflow.semd.template.json",name)
    for n in w["nodes"]:
        nm=n.get("name")
        if nm in ("Microsoft SQL 9:00","Microsoft SQL1 15:00"): n["credentials"]={"microsoftSql":{"id":csql,"name":"V2 - Microsoft SQL (СЭМД)"}}
        elif nm in ("Max Message","Max Message2","Max Message3","Max Message4"): n["credentials"]={"maxApi":{"id":cmax,"name":"V2 - MAX API (СЭМД)"}}
        elif nm in ("HTTP Request","HTTP Request1"): n["credentials"]={"httpHeaderAuth":{"id":ch,"name":"V2 - MAX Header Auth (СЭМД)"}}
        if nm in ("Get row(s)","Get row(s)1","Update row(s)"):
            n["parameters"]["dataTableId"]={"__rl":True,"value":tid,"mode":"list","cachedResultName":"report_recipients"}
        if nm=="Update row(s)":
            n["parameters"]["columns"]["schema"]=[
             {"id":"fio","displayName":"fio","required":False,"defaultMatch":False,"display":True,"type":"string","readOnly":False,"removed":False},
             {"id":"max_user_id","displayName":"max_user_id","required":False,"defaultMatch":False,"display":True,"type":"number","readOnly":False,"removed":False}]
        if nm=="HTTP Request":
            n["parameters"]["jsonBody"]=json.dumps({"url":BASE+"/webhook/max-semd-bot-message","update_types":["message_created"]},ensure_ascii=False,indent=2)
        if nm=="Max Message4":
            if mode=="none": n["disabled"]=True
            elif mode=="chat":
                n["disabled"]=False; n["parameters"]["sendTo"]="chat"; n["parameters"]["chatId"]=str(target); n["parameters"].pop("userId",None)
            else:
                n["disabled"]=False; n["parameters"]["sendTo"]="user"; n["parameters"]["userId"]=str(target); n["parameters"].pop("chatId",None)
    callbacks(domain,token); import_activate(key,w)
    print("Создана/проверена пустая Data Table report_recipients (fio, max_user_id). Заполните вручную только fio.")
def module_banner(number,total,title,description):
    global CURRENT_MODULE
    CURRENT_MODULE=title
    print("\n"+"="*68)
    print("МОДУЛЬ "+str(number)+" ИЗ "+str(total)+" — "+title)
    print(description)
    print("="*68)

def main():
    if not BASE.startswith("https://"): die("Не задан корректный HTTPS N8N_BASE_URL.")
    if not MODULES or any(m not in ("semd","vk") for m in MODULES): die("Не задан корректный список модулей.")
    print("Создайте Owner (для новой установки), затем откройте Settings → n8n API → Create an API key.")
    keyfile=Path("/root/.n8n-api-key")
    key=""
    if keyfile.exists():
        try:
            key=keyfile.read_text(encoding="utf-8").strip()
            if key: api(key,"/workflows?limit=1")
            else: key=""
        except Exception:
            key=""
    if key:
        print("Используется сохранённый n8n API Key (/root/.n8n-api-key).")
    else:
        key=getpass.getpass("n8n API Key (ввод скрыт): ").strip()
        if not key: die("API Key не введён.")
        api(key,"/workflows?limit=1")
        if os.geteuid()!=0: die("Для безопасного сохранения API Key запустите установщик через sudo.")
        keyfile.write_text(key+"\n",encoding="utf-8")
        keyfile.chmod(0o600)
        print("n8n API Key сохранён в /root/.n8n-api-key с правами 600.")
    selected=list(MODULES)
    all_workflows=items(api(key,"/workflows?limit=250"))
    for module,workflow in (("semd","Report СЭМД"),("vk","VK → MAX (v2)")):
        existing=next((w for w in all_workflows if w.get("name")==workflow),None)
        if module in selected and existing:
            if existing.get("active") is not True:
                die("Workflow "+workflow+" уже существует, но не активен. Он не изменён; проверьте его Credentials и активируйте вручную либо разберите незавершённую установку.")
            print("Модуль "+workflow+" уже установлен и активен. Он пропускается без изменений.")
            selected.remove(module)
    if not selected:
        print("Все выбранные модули уже установлены. Никакие изменения не внесены.")
        return
    required=[]
    if "semd" in selected: required.append(("n8n-nodes-max","1.1.2"))
    if "vk" in selected: required.append(("n8n-nodes-vk-community","0.1.2"))
    ensure_packages(key,required)
    total=len(selected)
    position=0
    if "semd" in selected:
        position+=1
        module_banner(position,total,"СЭМД","Настройка ежедневных отчётов о неподписанных СЭМД через MAX.")
        install_semd(key)
    if "vk" in selected:
        position+=1
        module_banner(position,total,"VK → MAX","Настройка автоматической пересылки новых публикаций сообщества ВКонтакте в групповой чат MAX.")
        install_vk(key)
    CURRENT_MODULE=""
    print("\nУСТАНОВКА ВЫБРАННЫХ МОДУЛЕЙ ЗАВЕРШЕНА. Существующие Credentials и workflow не перезаписывались.")
    print("Проверьте тестовые сценарии до ввода в эксплуатацию.")
if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: die("Операция прервана пользователем.")
    except Exception as e: die(e)
