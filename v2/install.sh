#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$ROOT"
fail(){ echo -e "\nОШИБКА: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail "Запустите через sudo внутри screen: sudo screen -S max-install bash $ROOT/install.sh"
[[ -f "$ROOT/configure.py" && -f "$ROOT/workflow.vk.template.json" && -f "$ROOT/workflow.semd.template.json" ]] || fail "Не найдены файлы v2."
if [[ -z "${STY:-}" ]]; then
  echo "ВНИМАНИЕ: установщик запущен не внутри screen."
  echo "Команду screen запускает администратор: sudo screen -S max-install bash $ROOT/install.sh"
  read -r -p "Продолжить без screen? [y/N]: " go
  [[ "$go" =~ ^[YyДд]$ ]] || exit 1
fi
. /etc/os-release
[[ "$ID" == "ubuntu" && ( "$VERSION_ID" == "22.04" || "$VERSION_ID" == "24.04" ) ]] || fail "В этой версии официально поддерживается только Ubuntu Server 22.04/24.04 LTS. Обнаружено: $PRETTY_NAME"
echo "1. Только СЭМД"; echo "2. Только VK → MAX"; echo "3. СЭМД + VK → MAX"
read -r -p "Выберите вариант [1-3]: " choice
case "$choice" in 1) MODULES="semd";; 2) MODULES="vk";; 3) MODULES="semd vk";; *) fail "Нужно выбрать 1, 2 или 3.";; esac
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git jq openssl python3 dnsutils netcat-openbsd
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
N8N_CONTAINERS="$(docker ps -a --format '{{.Names}} {{.Image}}' | awk '$1 == "n8n" || $2 ~ /(^|\/)n8nio\/n8n(:|$)/ {print $1}' | sort -u)"
N8N_COUNT="$(printf '%s\n' "$N8N_CONTAINERS" | awk 'NF {n++} END {print n+0}')"
[[ "$N8N_COUNT" -le 1 ]] || fail "Обнаружено несколько контейнеров n8n: $N8N_CONTAINERS. Установщик не будет выбирать контейнер автоматически."
CONTAINER="$(printf '%s\n' "$N8N_CONTAINERS" | sed -n '1p')"
FRESH="false"
if [[ -n "$CONTAINER" ]]; then
  [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == "true" ]] || fail "Контейнер n8n найден, но остановлен. Установщик не будет создавать новую установку поверх него. Сначала вручную запустите и проверьте существующий n8n."
  N8N_VERSION="$(docker exec "$CONTAINER" n8n --version 2>/dev/null | tr -d "\r\n")" || fail "Не удалось определить версию существующего n8n."
  [[ "$N8N_VERSION" == "2.42.6" ]] || fail "Для v2 требуется n8n 2.42.6; обнаружена версия: $N8N_VERSION. Установщик не будет обновлять или пересоздавать существующий контейнер."
  echo "Найден работающий n8n $N8N_VERSION: $CONTAINER"
  read -r -p "Добавить выбранные модули в существующую установку? [Д/н]: " ans
  [[ "$ans" =~ ^([Дд]|[Yy]|)$ ]] || fail "Операция отменена."
  N8N_ENV="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER")"
  DETECTED_HOST="$(printf '%s\n' "$N8N_ENV" | sed -n 's/^N8N_HOST=//p' | head -n1)"
  DETECTED_PROTOCOL="$(printf '%s\n' "$N8N_ENV" | sed -n 's/^N8N_PROTOCOL=//p' | head -n1)"
  DETECTED_URL=""
  if [[ -n "$DETECTED_HOST" ]]; then
    DETECTED_PROTOCOL="${DETECTED_PROTOCOL:-https}"
    DETECTED_URL="$DETECTED_PROTOCOL://$DETECTED_HOST"
  fi
  read -r -p "Публичный URL n8n [${DETECTED_URL:-https://n8n.example.ru}]: " N8N_BASE_URL
  N8N_BASE_URL="${N8N_BASE_URL:-$DETECTED_URL}"
  [[ "$N8N_BASE_URL" =~ ^https?:// ]] || N8N_BASE_URL="https://$N8N_BASE_URL"
  N8N_BASE_URL="$(printf '%s' "$N8N_BASE_URL" | sed 's:/*$::')"
  [[ "$N8N_BASE_URL" =~ ^https://[^/]+$ ]] || fail "Нужен полный HTTPS URL без пути."
  DB_TYPE="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | sed -n 's/^DB_TYPE=//p' | head -n1)"
  [[ -z "$DB_TYPE" || "$DB_TYPE" == "sqlite" ]] || fail "Обнаружена внешняя БД n8n ($DB_TYPE). Сначала создайте её отдельную резервную копию; установщик не продолжит без неё."
  install -d -m 700 /var/backups/max-n8n-v2
  BACKUP="/var/backups/max-n8n-v2/n8n-$CONTAINER-$(date +%Y%m%d-%H%M%S).tar.gz"
  docker exec "$CONTAINER" sh -c 'tar czf - -C /home/node/.n8n .' > "$BACKUP" || fail "Резервное копирование не удалось."
  [[ -s "$BACKUP" ]] || fail "Архив резервной копии пуст; дальнейшая установка отменена."
  chmod 600 "$BACKUP"
  KEY_VALUE="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | sed -n 's/^N8N_ENCRYPTION_KEY=//p' | head -n1)"
  if [[ -n "$KEY_VALUE" ]]; then printf '%s' "$KEY_VALUE" > "$BACKUP.key"; chmod 600 "$BACKUP.key"; fi
  echo "Резервная копия создана: $BACKUP"
  [[ ! -f "$BACKUP.key" ]] || echo "Отдельно сохранён N8N_ENCRYPTION_KEY: $BACKUP.key (хранить вместе с архивом и не передавать посторонним)."
else
  FRESH="true"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin не найден."
  apt-get install -y nginx certbot python3-certbot-nginx
  CA_TMP="$(mktemp -d)"
  curl -fLsS --retry 4 --retry-all-errors https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o "$CA_TMP/root.crt"
  curl -fLsS --retry 4 --retry-all-errors https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt -o "$CA_TMP/sub.crt"
  openssl x509 -in "$CA_TMP/root.crt" -noout >/dev/null
  openssl x509 -in "$CA_TMP/sub.crt" -noout >/dev/null
  install -m 644 "$CA_TMP/root.crt" /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
  install -m 644 "$CA_TMP/sub.crt" /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt
  update-ca-certificates
  rm -rf "$CA_TMP"
  read -r -p "Домен n8n: " DOMAIN
  [[ "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] || fail "Некорректный домен."
  read -r -p "Email администратора для Let's Encrypt: " EMAIL
  [[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || fail "Некорректный email."
  getent ahostsv4 "$DOMAIN" >/dev/null || fail "Домен не разрешается через DNS."
  read -r -p "TCP 80/443 проброшены на этот сервер? [y/N]: " confirm
  [[ "$confirm" =~ ^[YyДд]$ ]] || fail "Сначала настройте DNS и проброс TCP 80/443."
  N8N_BASE_URL="https://$DOMAIN"; CONTAINER="n8n"
  N8N_ENCRYPTION_KEY="$(openssl rand -hex 32)"
  printf "DOMAIN='%s'\nEMAIL='%s'\nN8N_ENCRYPTION_KEY='%s'\nDNS_PRIMARY='9.9.9.9'\nDNS_SECONDARY='8.8.4.4'\n" "$DOMAIN" "$EMAIL" "$N8N_ENCRYPTION_KEY" > "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
  sed "s/__DOMAIN__/$DOMAIN/g" "$ROOT/nginx-n8n.conf.template" > "/etc/nginx/sites-available/n8n-$DOMAIN"
  ln -sfn "/etc/nginx/sites-available/n8n-$DOMAIN" "/etc/nginx/sites-enabled/n8n-$DOMAIN"
  nginx -t >/dev/null; systemctl enable --now nginx
  docker compose --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml" pull
  docker compose --env-file "$ROOT/.env" -f "$ROOT/docker-compose.yml" up -d
  ready=0
  for i in $(seq 1 90); do curl -fsS http://127.0.0.1:5678/healthz >/dev/null 2>&1 && { ready=1; break; }; sleep 3; done
  [[ "$ready" == "1" ]] || fail "n8n не запустился. Проверьте docker logs n8n."
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect
  systemctl enable --now certbot.timer || true
  echo "n8n доступен: $N8N_BASE_URL"
  echo "Создайте Owner в браузере, затем Settings → n8n API → Create an API key."
fi
export N8N_BASE_URL CONTAINER MODULES FRESH
python3 "$ROOT/configure.py"
echo "Установка выбранных модулей завершена."
