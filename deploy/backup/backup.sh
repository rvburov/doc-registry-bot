#!/usr/bin/env bash
# Контейнер копий: pg_dump базы бота и запись файлов копий в разрешенные папки.
#
#   backup.sh loop     — ждет заявки бота в таблице backup_jobs: копия по команде владельца в
#                        чате, копия из терминала (./backup.sh), проверка копии перед заменой
#                        журнала через чат
#   backup.sh protect  — одна копия базы в /backups (так ее делает update.sh перед обновлением)
#   backup.sh now      — то же, что protect (старое имя)
#
# Ночной копии по расписанию больше нет: раз в неделю бот напоминает владельцу сделать копию.
# Старые копии не удаляются: ни в /backups, ни в папках копий, ни в сетевой папке.
#
# Папки копий подключены в compose.yaml: /copies/0 — папка backups рядом с compose.yaml
# (копии из терминала), /copies/1..3 — BACKUP_DIR_1..3 из .env. Других папок компьютера
# контейнер не видит. В каждой папке должен быть файл-метка .doc-registry-copies: если диск не
# подключен, Docker подставил бы вместо папки пустую, и копия молча ушла бы не туда.
# Сетевая папка Windows (BACKUP_SHARE) — через smbclient, без подключения к Ubuntu.
set -uo pipefail
# Копии содержат весь журнал: читать их может только владелец файла.
umask 077

DIR=/backups
MARK=.doc-registry-copies
export TZ=${TZ:-Europe/Moscow}
# Русские буквы в сообщениях: обрезка по буквам, а не по байтам (иначе psql откажется от текста).
export LC_ALL=C.UTF-8
PSQL=(psql -X -A -t -q -v ON_ERROR_STOP=1)
# Ограничения по времени: зависшая команда не должна держать очередь копий.
DUMP_LIMIT=30m
SHARE_LIMIT=10m

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

short() {
  # Первые $2 букв строки $1 (по буквам: LC_ALL=C.UTF-8).
  local text=$1
  printf '%s' "${text:0:$2}"
}

clean() {
  short "$(printf '%s' "$1" | tr -d '"\\' | tr '\n\r\t' '   ' | sed 's/^ *//; s/ *$//')" 300
}

last() {
  # Последняя строка журнала команды, не длиннее $2 букв.
  short "$(tail -n 1 "$1")" "$2"
}

# ── Защитная копия перед обновлением (update.sh) ─────────────────────────────

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

share() {
  # share auth команда: smbclient с ограничением по времени.
  timeout "$SHARE_LIMIT" smbclient "$BACKUP_SHARE" -t 60 -A "$1" -c "$2"
}

protect() {
  mkdir -p "$DIR"
  local name part log auth share_ok=false message=""
  name="doc-registry-$(date +%Y-%m-%d_%H%M%S).dump"
  part="$DIR/$name.part"
  log=$(mktemp)
  if ! timeout "$DUMP_LIMIT" pg_dump -Fc --lock-wait-timeout=60s -f "$part" 2>"$log"; then
    rm -f "$part"
    status false false "pg_dump: $(last "$log" 200)" ""
    rm -f "$log"
    echo "Копия не сделана" >&2
    return 1
  fi
  mv "$part" "$DIR/$name"
  if [ -z "${BACKUP_SHARE:-}" ]; then
    message="сетевая папка не задана (BACKUP_SHARE)"
  else
    auth=$(auth_file)
    if share "$auth" "put \"$DIR/$name\" \"$name\"" >"$log" 2>&1; then
      share_ok=true
    else
      message=$(last "$log" 200)
    fi
    rm -f "$auth"
  fi
  rm -f "$log"
  status true "$share_ok" "$message" "$name"
  echo "Копия: $name, сетевая папка: $share_ok $message"
}

# ── Заявки бота ──────────────────────────────────────────────────────────────

sql() {
  # Запрос со стандартного ввода: только так psql подставляет переменные :'имя' с экранированием.
  "${PSQL[@]}" "$@"
}

# Разделитель полей — не пробельный знак: иначе пустая подпапка слиплась бы с соседним полем.
SEP=$'\x1f'

claim() {
  sql -F "$SEP" <<'SQL'
UPDATE backup_jobs SET status = 'running', started_at = now()
WHERE id = (SELECT id FROM backup_jobs WHERE status = 'new' ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, kind, coalesce(target_id, 0), folder_key, subdir, dump_name, csv_name,
          coalesce(snapshot, ''), fingerprint, dump_sha256, coalesce(owner_uid, -1),
          coalesce(owner_gid, -1);
SQL
}

finish() {
  # finish id статус dump_ok csv_ok dump_size csv_size сообщение имя_dump имя_csv sha_dump путь
  if ! sql -v id="$1" -v st="$2" -v dok="$3" -v cok="$4" -v ds="$5" -v cs="$6" \
    -v msg="$(clean "$7")" -v dn="$8" -v cn="$9" -v dsha="${10}" -v hp="${11}" <<'SQL'
UPDATE backup_jobs
SET status = :'st', dump_ok = :'dok'::boolean, csv_ok = :'cok'::boolean,
    dump_size = :'ds'::bigint, csv_size = :'cs'::bigint, message = :'msg',
    dump_name = :'dn', csv_name = :'cn', dump_sha256 = :'dsha', host_path = :'hp',
    finished_at = now(), csv_data = NULL, snapshot = NULL
WHERE id = :'id'::bigint AND status = 'running';
SQL
  then
    # Итог не записался: заявка не должна остаться «в работе». Копия при этом не засчитывается.
    say "Итог заявки $1 не записан, заявка отмечена неудачной"
    sql -v id="$1" <<'SQL'
UPDATE backup_jobs
SET status = 'failed', dump_ok = false, csv_ok = false, finished_at = now(), csv_data = NULL,
    snapshot = NULL, message = 'контейнер копий не смог записать итог, подробности: docker compose logs backup'
WHERE id = :'id'::bigint AND status = 'running';
SQL
  fi
}

base_of() {
  # Папка в контейнере по ключу; пусто — такая папка не настроена.
  case "$1" in
    0) echo /copies/0 ;;
    1 | 2 | 3)
      local var="BACKUP_DIR_$1"
      if [ -n "${!var:-}" ]; then echo "/copies/$1"; fi
      ;;
  esac
}

host_of() {
  # Путь папки на компьютере, как он задан в .env: для отчета владельцу. ./ — внутри папки бота.
  local value
  case "$1" in
    0) value="./backups" ;;
    1 | 2 | 3)
      local var="BACKUP_DIR_$1"
      value="${!var:-}"
      ;;
    share) value="${BACKUP_SHARE:-}" ;;
  esac
  if [[ "$value" == ./* && "${BOT_DIR:-}" == /* ]]; then value="${BOT_DIR%/}/${value#./}"; fi
  echo "$value"
}

bad_subdir() {
  # Подпапку уже проверил бот; здесь вторая проверка: ни «..», ни абсолютного пути.
  local sub=$1
  [[ "$sub" == /* || "/$sub/" == */../* || "/$sub/" == */./* || "$sub" == *$'\n'* ]]
}

owner_of() {
  # Кому отдать файлы: пользователю из заявки (копия из терминала), владельцу метки папки, а
  # если метку создали от root (sudo) — владельцу метки папки бота: ее создает установщик.
  local uid=$1 gid=$2 base=$3 owner
  if [ "$uid" -ge 0 ] && [ "$gid" -ge 0 ]; then
    echo "$uid:$gid"
    return
  fi
  owner=$(stat -c '%u:%g' "$base/$MARK" 2>/dev/null || stat -c '%u:%g' "$base" 2>/dev/null || echo "")
  if [ "${owner%%:*}" = 0 ]; then
    owner=$(stat -c '%u:%g' "/copies/0/$MARK" 2>/dev/null || echo "$owner")
  fi
  echo "$owner"
}

make_dir() {
  # make_dir папка подпапка владелец: создать подпапки по одной и отдать их владельцу.
  local path=$1 sub=$2 owner=$3 part
  local -a parts
  [ -n "$sub" ] || return 0
  IFS=/ read -r -a parts <<<"$sub"
  for part in "${parts[@]}"; do
    path="$path/$part"
    if [ ! -d "$path" ]; then
      mkdir "$path" || return 1
      chmod 700 "$path"
      if [ -n "$owner" ]; then chown "$owner" "$path" 2>/dev/null || true; fi
    fi
  done
}

sha() { sha256sum "$1" | cut -d' ' -f1; }

dump_ok() {
  # Архив читается целиком (не только оглавление): обрезанный файл не пройдет.
  timeout "$DUMP_LIMIT" pg_restore -f /dev/null "$1"
}

folder_problem() {
  # Почему в папку с ключом нельзя писать; пусто — можно.
  local key=$1 base
  base=$(base_of "$key")
  if [ -z "$base" ]; then
    echo "папка $key не подключена к контейнеру копий: впишите BACKUP_DIR_$key в .env и выполните docker compose up -d"
  elif [ ! -d "$base" ]; then
    echo "папка не подключена к контейнеру копий: выполните docker compose up -d"
  elif [ ! -e "$base/$MARK" ]; then
    echo "папка $(host_of "$key") недоступна контейнеру копий: в ней нет файла-метки $MARK (диск или сетевая папка не подключены, или папка не подготовлена). Проверьте, что диск подключен (ls $(host_of "$key")); если подключили после запуска контейнеров: docker compose restart backup; если папка новая: создайте в ней метку $MARK"
  fi
}

do_backup() {
  local id=$1 key=$2 sub=$3 dump=$4 csv=$5 snap=$6 fp=$7 uid=$8 gid=$9
  local base dir work="" owner="" log message="" dok=false cok=false ds=0 cs=0 dsha="" auth problem
  local host
  host=$(host_of "$key")
  log=$(mktemp)
  if [ "$key" = share ]; then
    if [ -z "${BACKUP_SHARE:-}" ]; then
      finish "$id" failed false false 0 0 "сетевая папка не задана в .env (BACKUP_SHARE)" "$dump" "$csv" "" "$host"
      return
    fi
    work=$(mktemp -d)
    dir=$work
  else
    problem=$(folder_problem "$key")
    if [ -n "$problem" ]; then
      finish "$id" failed false false 0 0 "$problem" "$dump" "$csv" "" "$host"
      return
    fi
    base=$(base_of "$key")
    if bad_subdir "$sub"; then
      finish "$id" failed false false 0 0 "недопустимая подпапка «$sub»" "$dump" "$csv" "" "$host"
      return
    fi
    owner=$(owner_of "$uid" "$gid" "$base")
    if ! make_dir "$base" "$sub" "$owner" 2>"$log"; then
      finish "$id" failed false false 0 0 "не удалось создать папку: $(last "$log" 150)" "$dump" "$csv" "" "$host"
      return
    fi
    dir=$base
    if [ -n "$sub" ]; then dir="$base/$sub"; fi
    if ! touch "$dir/.проверка-$id" 2>"$log"; then
      finish "$id" failed false false 0 0 "в папку нельзя записать: $(last "$log" 150)" "$dump" "$csv" "" "$host"
      return
    fi
    rm -f "$dir/.проверка-$id"
    # Файлы не затираются: если имя занято, к нему добавляется номер заявки.
    if [ -e "$dir/$dump" ] || [ -e "$dir/$csv" ]; then
      dump="${dump%.dump}-$id.dump"
      csv="${csv%.csv}-$id.csv"
    fi
  fi

  # DUMP всей базы снимком бота — первым: если снимок уже недействителен (бот перезапустился),
  # копия не получится и CSV тоже писать незачем. Затем архив читается целиком.
  if timeout "$DUMP_LIMIT" pg_dump -Fc --lock-wait-timeout=60s --snapshot="$snap" \
    -f "$dir/$dump.part" 2>"$log" && dump_ok "$dir/$dump.part" 2>>"$log"; then
    mv "$dir/$dump.part" "$dir/$dump"
    dok=true
    ds=$(stat -c %s "$dir/$dump")
    dsha=$(sha "$dir/$dump")
  else
    rm -f "$dir/$dump.part"
    message="DUMP: $(last "$log" 200)"
  fi

  # CSV журнала: байты, выгруженные ботом из того же снимка базы.
  if $dok; then
    if sql -v id="$id" <<<"SELECT encode(csv_data, 'base64') FROM backup_jobs WHERE id = :'id'::bigint;" \
      2>"$log" | base64 -d >"$dir/$csv.part" 2>>"$log" && [ "$(sha "$dir/$csv.part")" = "$fp" ]; then
      mv "$dir/$csv.part" "$dir/$csv"
      cok=true
      cs=$(stat -c %s "$dir/$csv")
    else
      rm -f "$dir/$csv.part"
      message="CSV: $(last "$log" 150) (или не совпал отпечаток)"
    fi
  fi

  if [ -n "$owner" ]; then
    if $cok; then chown "$owner" "$dir/$csv" 2>/dev/null || true; fi
    if $dok; then chown "$owner" "$dir/$dump" 2>/dev/null || true; fi
  fi

  if [ "$key" = share ]; then
    auth=$(auth_file)
    # В сетевой папке файлы тоже не затираются: занятое имя получает номер заявки.
    if share "$auth" "ls \"$dump\"" >/dev/null 2>&1 || share "$auth" "ls \"$csv\"" >/dev/null 2>&1; then
      if $dok; then mv "$dir/$dump" "$dir/${dump%.dump}-$id.dump"; fi
      if $cok; then mv "$dir/$csv" "$dir/${csv%.csv}-$id.csv"; fi
      dump="${dump%.dump}-$id.dump"
      csv="${csv%.csv}-$id.csv"
    fi
    if $dok && ! share "$auth" "put \"$dir/$dump\" \"$dump\"" >"$log" 2>&1; then
      dok=false
      message="$message DUMP в сетевую папку: $(last "$log" 150)"
    fi
    if $cok && ! share "$auth" "put \"$dir/$csv\" \"$csv\"" >"$log" 2>&1; then
      cok=false
      message="$message CSV в сетевую папку: $(last "$log" 150)"
    fi
    rm -f "$auth"
    rm -rf "$work"
  fi
  rm -f "$log"

  if $dok && $cok; then
    finish "$id" "done" true true "$ds" "$cs" "" "$dump" "$csv" "$dsha" "$host"
    say "Копия $id: $dump, $csv"
  else
    finish "$id" failed "$dok" "$cok" "$ds" "$cs" "$message" "$dump" "$csv" "$dsha" "$host"
    say "Копия $id не сделана: $message"
  fi
}

do_verify() {
  # Проверка копии перед заменой журнала: оба файла на месте и те же, что при копии (контрольные
  # суммы), DUMP читается целиком.
  local id=$1 key=$2 sub=$3 dump=$4 csv=$5 fp=$6 want=$7
  local base dir work="" log message="" dok=false cok=false ds=0 cs=0 auth problem host
  host=$(host_of "$key")
  log=$(mktemp)
  if [ "$key" = share ]; then
    work=$(mktemp -d)
    dir=$work
    auth=$(auth_file)
    share "$auth" "get \"$dump\" \"$dir/$dump\"" >"$log" 2>&1
    share "$auth" "get \"$csv\" \"$dir/$csv\"" >>"$log" 2>&1
    rm -f "$auth"
  else
    problem=$(folder_problem "$key")
    if [ -n "$problem" ]; then
      finish "$id" failed false false 0 0 "$problem" "$dump" "$csv" "" "$host"
      return
    fi
    base=$(base_of "$key")
    dir=$base
    if [ -n "$sub" ]; then dir="$base/$sub"; fi
  fi
  if [ ! -f "$dir/$csv" ]; then
    message="нет файла CSV копии $csv"
  elif [ "$(sha "$dir/$csv")" != "$fp" ]; then
    message="CSV копии изменен после копирования"
  else
    cok=true
    cs=$(stat -c %s "$dir/$csv")
  fi
  if [ ! -f "$dir/$dump" ]; then
    message="$message нет файла DUMP копии $dump"
  elif [ -n "$want" ] && [ "$(sha "$dir/$dump")" != "$want" ]; then
    message="$message DUMP копии изменен или поврежден после копирования"
  elif ! dump_ok "$dir/$dump" 2>"$log"; then
    message="$message DUMP копии не читается: $(last "$log" 150)"
  else
    dok=true
    ds=$(stat -c %s "$dir/$dump")
  fi
  rm -f "$log"
  if [ -n "$work" ]; then rm -rf "$work"; fi
  if $dok && $cok; then
    finish "$id" "done" true true "$ds" "$cs" "" "$dump" "$csv" "$want" "$host"
  else
    finish "$id" failed "$dok" "$cok" "$ds" "$cs" "$message" "$dump" "$csv" "$want" "$host"
  fi
}

process_one() {
  # 0 — заявка выполнена, 1 — заявок нет (или база еще не готова).
  local row id kind _target key sub dump csv snap fp dsha uid gid
  if ! row=$(claim 2>/tmp/claim.err); then
    if [ -z "${CLAIM_WARNED:-}" ]; then
      say "Заявки пока не читаются: $(last /tmp/claim.err 200)"
      CLAIM_WARNED=1
    fi
    return 1
  fi
  CLAIM_WARNED=
  [ -n "$row" ] || return 1
  IFS=$SEP read -r id kind _target key sub dump csv snap fp dsha uid gid <<<"$row"
  case "$kind" in
    backup) do_backup "$id" "$key" "$sub" "$dump" "$csv" "$snap" "$fp" "$uid" "$gid" ;;
    verify) do_verify "$id" "$key" "$sub" "$dump" "$csv" "$fp" "$dsha" ;;
    *) finish "$id" failed false false 0 0 "неизвестный вид заявки $kind" "$dump" "$csv" "" "" ;;
  esac
  return 0
}

recover() {
  # Заявки, оборванные прошлым запуском контейнера: убрать недописанные файлы и отметить.
  # 0 — сделано (база доступна), 1 — база еще недоступна.
  local rows id key sub dump csv base dir
  rows=$(sql -F "$SEP" <<'SQL'
SELECT id, folder_key, subdir, dump_name, csv_name FROM backup_jobs WHERE status = 'running';
SQL
) || return 1
  while IFS=$SEP read -r id key sub dump csv; do
    [ -n "$id" ] || continue
    base=$(base_of "$key")
    if [ -n "$base" ]; then
      dir=$base
      if [ -n "$sub" ] && ! bad_subdir "$sub"; then dir="$base/$sub"; fi
      rm -f "$dir/$dump.part" "$dir/$csv.part" "$dir/${dump%.dump}-$id.dump.part" \
        "$dir/${csv%.csv}-$id.csv.part"
    fi
  done <<<"$rows"
  sql <<<"UPDATE backup_jobs SET status = 'failed', finished_at = now(), csv_data = NULL, snapshot = NULL, message = 'контейнер копий перезапустился во время работы' WHERE status = 'running';" || return 1
  return 0
}

heartbeat() {
  # Отметка «контейнер копий жив» раз в 10 секунд: по ней бот отличает занятый контейнер от
  # остановленного.
  while true; do
    sql <<<"INSERT INTO bot_state (key, value) VALUES ('backup_worker', now()::text) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;" >/dev/null 2>&1
    sleep 10
  done
}

loop() {
  say "Ночная копия по расписанию отключена. Копии делаются по команде владельца в чате, из терминала (./backup.sh) и перед обновлением (update.sh). Старые копии не удаляются."
  trap 'say "Остановка контейнера копий"; exit 0' TERM INT
  heartbeat &
  until recover 2>/dev/null; do sleep 5; done
  while true; do
    process_one || sleep 2
  done
}

case "${1:-loop}" in
  protect | now) protect ;;
  loop) loop ;;
  *) echo "Использование: backup.sh [loop|protect]" >&2; exit 2 ;;
esac
