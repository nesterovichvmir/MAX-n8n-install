#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
fail(){ echo -e "\nОШИБКА: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail "Запустите командой: sudo bash install.sh"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg jq nginx certbot python3-certbot-nginx openssl dnsutils netcat-openbsd python3
# Install official Russian Trusted CA certificates for MAX API TLS
CA_TMP="$(mktemp -d)"
trap 'rm -rf "$CA_TMP"' EXIT
curl -fLsS --retry 4 --retry-all-errors https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o "$CA_TMP/russian_trusted_root_ca.crt"
curl -fLsS --retry 4 --retry-all-errors https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt -o "$CA_TMP/russian_trusted_sub_ca.crt"
openssl x509 -in "$CA_TMP/russian_trusted_root_ca.crt" -noout >/dev/null
openssl x509 -in "$CA_TMP/russian_trusted_sub_ca.crt" -noout >/dev/null
openssl verify -CAfile "$CA_TMP/russian_trusted_root_ca.crt" "$CA_TMP/russian_trusted_sub_ca.crt" >/dev/null
install -m 644 "$CA_TMP/russian_trusted_root_ca.crt" /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
install -m 644 "$CA_TMP/russian_trusted_sub_ca.crt" /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt
update-ca-certificates
rm -rf "$CA_TMP"

python3 - <<'PYINPUT'
import getpass, os, re, socket, subprocess, sys
from pathlib import Path
sys.stdin = open('/dev/tty')
p=Path('.env'); cfg={}
if p.exists():
    for raw in p.read_text(encoding='utf-8').splitlines():
        s=raw.strip()
        if s and not s.startswith('#') and '=' in s:
            k,v=s.split('=',1)
            v=v.strip()
            if len(v)>=2 and v[0]==v[-1] and v[0] in ("'", '"'):
                q=v[0]; v=v[1:-1]
                if q=="'": v=v.replace("\\'", "'")
            cfg[k.strip()]=v
reuse=False
if cfg:
    ans=input('Найдена .env. Использовать сохраненные параметры? [Д/н]: ').strip().lower()
    reuse=ans in ('','д','да','y','yes')
def ask(k,label,default='',secret=False,required=True):
    old=cfg.get(k,'') if reuse else ''
    prompt=f'{label} [{"********" if secret else old or default}]: ' if (old or default) else f'{label}: '
    val=(getpass.getpass(prompt) if secret else input(prompt)).strip()
    if not val: val=old or default
    if required and not val: print(f'Необходимо заполнить: {label}',file=sys.stderr); sys.exit(2)
    return val
print('\n=== Настройка MAX + n8n ===')
domain=ask('DOMAIN','Домен n8n (например n8n.medorg.ru)')
if not re.fullmatch(r'(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}',domain):
    print('Некорректное доменное имя.',file=sys.stderr); sys.exit(2)
email=ask('EMAIL','Email администратора для Let\'s Encrypt')
if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): print('Некорректный email.',file=sys.stderr); sys.exit(2)
srv=ask('SQL_SERVER','Имя или IP SQL Server'); port=ask('SQL_PORT','TCP-порт SQL Server','1433')
if not port.isdigit() or not 1<=int(port)<=65535: print('Некорректный TCP-порт.',file=sys.stderr); sys.exit(2)
db=ask('SQL_DATABASE','Название базы данных МИС'); user=ask('SQL_USER','SQL логин (только чтение)')
pwd=ask('SQL_PASSWORD','SQL пароль',secret=True); token=ask('MAX_TOKEN','Токен MAX-бота',secret=True)
dns1=ask('DNS_PRIMARY','Основной DNS','9.9.9.9')
dns2=ask('DNS_SECONDARY','Резервный DNS','8.8.4.4')
tls=ask('SQL_TLS','Использовать TLS для SQL? true/false','true').lower()
if tls not in ('true','false'): print('Введите true или false.',file=sys.stderr); sys.exit(2)
allow='false'
if tls=='true':
    allow=ask('SQL_ALLOW_UNAUTHORIZED_CERTS','Разрешить недоверенный сертификат? true/false','false').lower()
    if allow not in ('true','false'): print('Введите true или false.',file=sys.stderr); sys.exit(2)
print('\nОбщий отчет: none / chat / user')
mode=ask('REPORT_TARGET_TYPE','Режим общего отчета','none').lower()
if mode not in ('none','chat','user'): print('Допустимы none, chat, user.',file=sys.stderr); sys.exit(2)
target=ask('REPORT_TARGET_ID','ID чата/пользователя MAX',required=False) if mode!='none' else ''
if mode!='none' and not target: print('ID не указан — общий отчет отключен.'); mode='none'
try:
    ips=sorted({x[4][0] for x in socket.getaddrinfo(domain,443,type=socket.SOCK_STREAM)})
    print('DNS:',', '.join(ips))
except Exception as e: print(f'Домен не разрешается через DNS: {e}',file=sys.stderr); sys.exit(2)
try:
    with socket.create_connection((srv,int(port)),timeout=6): pass
except Exception as e:
    print(f'Нет TCP-доступа к SQL Server {srv}:{port}: {e}',file=sys.stderr)
    print('Проверка TCP не подтверждает SQL-аутентификацию.',file=sys.stderr); sys.exit(2)
key=cfg.get('N8N_ENCRYPTION_KEY','') if reuse else ''
if not key: key=subprocess.check_output(['openssl','rand','-hex','32'],text=True).strip()
vals={'DOMAIN':domain,'EMAIL':email,'TIMEZONE':'Europe/Moscow','SQL_SERVER':srv,'SQL_PORT':port,'SQL_DATABASE':db,'SQL_USER':user,'SQL_PASSWORD':pwd,'SQL_TLS':tls,'SQL_ALLOW_UNAUTHORIZED_CERTS':allow,'MAX_TOKEN':token,'REPORT_TARGET_TYPE':mode,'REPORT_TARGET_ID':target,'N8N_ENCRYPTION_KEY':key,'DNS_PRIMARY':dns1,'DNS_SECONDARY':dns2}
with p.open('w',encoding='utf-8') as f:
    f.write('# Создано установщиком. Содержит секреты.\n')
    for k,v in vals.items():
        if '\n' in v or '\r' in v: print(f'Недопустимый перевод строки в параметре {k}.',file=sys.stderr); sys.exit(2)
        # Single-quoted dotenv values prevent Compose from expanding '$' in secrets.
        f.write(f"{k}='{v.replace(chr(39), chr(92)+chr(39))}'\n")
os.chmod(p,0o600)
print('Параметры сохранены в .env с правами 600; пароли и токены не отображались.')
PYINPUT
ENV_JSON="$(python3 - <<'PYENV'
import json
p={}
for raw in open('.env',encoding='utf-8'):
 s=raw.strip()
 if s and not s.startswith('#') and '=' in s:
  k,v=s.split('=',1)
  v=v.strip()
  if len(v)>=2 and v[0]==v[-1] and v[0] in ("'", '"'):
   q=v[0]; v=v[1:-1]
   if q=="'": v=v.replace("\\'", "'")
  p[k.strip()]=v
print(json.dumps(p))
PYENV
)"
DOMAIN="$(jq -r '.DOMAIN' <<<"$ENV_JSON")"; EMAIL="$(jq -r '.EMAIL' <<<"$ENV_JSON")"
if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh; fi
docker compose version >/dev/null 2>&1 || fail "Не найден Docker Compose plugin"
install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
sed "s/__DOMAIN__/$DOMAIN/g" nginx-n8n.conf.template > "/etc/nginx/sites-available/n8n-$DOMAIN"
ln -sfn "/etc/nginx/sites-available/n8n-$DOMAIN" "/etc/nginx/sites-enabled/n8n-$DOMAIN"
nginx -t >/dev/null; systemctl enable --now nginx
docker compose pull; docker compose up -d
ready=0
for i in $(seq 1 80); do if curl -fsS http://127.0.0.1:5678/healthz >/dev/null 2>&1; then ready=1; break; fi; sleep 3; done
[[ $ready -eq 1 ]] || fail "n8n не запустился. Логи: docker compose logs --tail=100 n8n"
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect
systemctl enable --now certbot.timer || true
chmod 600 .env
cat <<TXT
\nn8n установлен: https://$DOMAIN
Откройте домен, создайте первую учетную запись Owner, затем Settings → n8n API → Create an API key.
Вернитесь в терминал: API Key будет запрошен скрытым вводом.
TXT
python3 configure.py
