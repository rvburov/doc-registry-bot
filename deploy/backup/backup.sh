#!/usr/bin/env bash
# Резервная копия базы бота: pg_dump в папку /backups на сервере, затем копия в сетевую папку.
#
#   backup.sh loop   — каждый день в BACKUP_TIME (по умолчанию 03:00 по московскому времени)
#   backup.sh now    — одна копия сейчас (так ее делает update.sh перед обновлением)
#
# Копии хранятся BACKUP_KEEP_DAYS дней (по умолчанию 30) и на сервере, и в сетевой папке.
# Итог каждой копии — в /backups/status.json: его читает бот и сообщает владельцу о сбое.
set -uo pipefail
# Копии содержат весь журнал: читать их может только владелец файла.
umask 077

DIR=/backups
KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
AT=${BACKUP_TIME:-03:00}
export TZ=${TZ:-Europe/Moscow}

clean() {
  printf '%s' "$1" | tr -d '"\\' | tr '\n\r\t' '   ' | cut -c1-300
}

status() {
  local ok=$1 share_ok=$2 message file=$4
  message=$(clean "$3")
  printf '{"time":"%s","ok":%s,"share_ok":%s,"message":"%s","file":"%s"}\n' \
    "$(date -Iseconds)" "$ok" "$share_ok" "$message" "$file" > "$DIR/status.json.part"
  # Отчет читает бот из своего контейнера: ему нужен доступ на чтение.
  chmod 644 "$DIR/status.json.part"
  mv "$DIR/status.json.part" "$DIR/status.json"
}

auth_file() {
  local file
  file=$(mktemp)
  chmod 600 "$file"
  {
    printf 'username = %s\n' "${BACKUP_SHARE_USER:-}"
    printf 'password = %s\n' "${BACKUP_SHARE_PASSWORD:-}"
    if [ -n "${BACKUP_SHARE_DOMAIN:-}" ]; then printf 'domain = %s\n' "$BACKUP_SHARE_DOMAIN"; fi
  } > "$file"
  printf '%s' "$file"
}

prune_share() {
  local auth=$1 border name day
  border=$(date -d "-$KEEP_DAYS days" +%Y-%m-%d)
  smbclient "$BACKUP_SHARE" -A "$auth" -c 'ls doc-registry-*.dump' 2>/dev/null \
    | grep -o 'doc-registry-[0-9-]*_[0-9]*\.dump' | sort -u | while read -r name; do
      day=${name#doc-registry-}
      day=${day%%_*}
      if [[ "$day" < "$border" ]]; then
        smbclient "$BACKUP_SHARE" -A "$auth" -c "del $name" >/dev/null 2>&1 || true
      fi
    done
}

run_backup() {
  mkdir -p "$DIR"
  local name part log auth share_ok=false message=""
  name="doc-registry-$(date +%Y-%m-%d_%H%M%S).dump"
  part="$DIR/$name.part"
  log=$(mktemp)
  if ! pg_dump -Fc -f "$part" 2>"$log"; then
    rm -f "$part"
    status false false "pg_dump: $(tail -n 1 "$log")" ""
    rm -f "$log"
    echo "Копия не сделана" >&2
    return 1
  fi
  mv "$part" "$DIR/$name"
  find "$DIR" -name 'doc-registry-*.dump' -mtime "+$KEEP_DAYS" -delete
  if [ -z "${BACKUP_SHARE:-}" ]; then
    message="сетевая папка не задана (BACKUP_SHARE)"
  else
    auth=$(auth_file)
    if smbclient "$BACKUP_SHARE" -A "$auth" -c "put \"$DIR/$name\" \"$name\"" >"$log" 2>&1; then
      share_ok=true
      prune_share "$auth"
    else
      message=$(tail -n 1 "$log")
    fi
    rm -f "$auth"
  fi
  rm -f "$log"
  status true "$share_ok" "$message" "$name"
  echo "Копия: $name, сетевая папка: $share_ok $message"
}

seconds_until() {
  local now target
  now=$(date +%s)
  target=$(date -d "today $AT" +%s)
  if [ "$target" -le "$now" ]; then target=$(date -d "tomorrow $AT" +%s); fi
  echo $((target - now))
}

case "${1:-loop}" in
  now) run_backup ;;
  loop)
    while true; do
      sleep "$(seconds_until)"
      run_backup || true
    done
    ;;
  *) echo "Использование: backup.sh [loop|now]" >&2; exit 2 ;;
esac
