#!/usr/bin/env bash
# Установка и обновление бота на сервере.
#
#   ./update.sh doc-registry-bot-1.2.0.tar.gz   установить версию из файла (первая установка тоже)
#   ./update.sh --rollback                      вернуть предыдущую версию
#   ./update.sh --restore ФАЙЛ.dump             восстановить базу из защитной копии (в /backups)
#   ./update.sh --restore ПУТЬ/база-....dump    из копии в папке backups или в папке BACKUP_DIR_N
#
# Перед обновлением делается защитная копия базы. Миграции схемы только добавляющие, поэтому
# прежняя версия бота работает и на обновленной схеме: откат меняет только образ.
set -euo pipefail
cd "$(dirname "$0")"

STATE=.update-state

fail() { echo "Ошибка: $*" >&2; exit 1; }

[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

current_version() { sed -n 's/^BOT_VERSION=//p' .env | tail -n 1; }

set_version() {
  if grep -q '^BOT_VERSION=' .env; then
    sed -i "s/^BOT_VERSION=.*/BOT_VERSION=$1/" .env
  else
    printf '\nBOT_VERSION=%s\n' "$1" >> .env
  fi
}

running() { [ -n "$(docker compose ps -q postgres 2>/dev/null)" ]; }

backup_now() {
  # Защитная копия образом копий версии $1 (по умолчанию нынешней): отдельным контейнером, а не
  # работающим. Работающий может быть прежней версии, а его «backup.sh now» удалял старые копии.
  local version=${1:-$(current_version)}
  if ! running; then
    echo "Бот еще не запущен: копию делать не из чего."
    return
  fi
  echo "Защитная копия базы перед обновлением..."
  if BOT_VERSION="$version" docker compose run --rm --no-deps -T backup protect </dev/null; then
    return
  fi
  # Образ копий прежней версии не знает protect: pg_dump напрямую, без удаления старых копий.
  # shellcheck disable=SC2016  # $(date ...) выполняется в контейнере
  BOT_VERSION="$version" docker compose run --rm --no-deps -T --entrypoint sh backup -c \
    'umask 077; pg_dump -Fc -f "/backups/doc-registry-$(date +%Y-%m-%d_%H%M%S).dump"' </dev/null \
    || fail "копия базы не сделана, обновление остановлено"
}

set_bot_dir() {
  # Папка бота на компьютере: по ней бот показывает и узнает в чате путь ./backups.
  local dir
  dir=$(pwd -P)
  if grep -q '^BOT_DIR=' .env; then
    sed -i "s|^BOT_DIR=.*|BOT_DIR=$dir|" .env
  else
    printf '\nBOT_DIR=%s\n' "$dir" >> .env
  fi
}

prepare() {
  # Папку копий из терминала создает пользователь, а не Docker: иначе она была бы от root.
  # Метка .doc-registry-copies говорит контейнеру копий, что это настоящая папка, а не пустая,
  # подставленная Docker вместо неподключенного диска.
  mkdir -p backups
  touch backups/.doc-registry-copies
  set_bot_dir
}

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

container_path() {
  # Путь копии на компьютере -> путь в контейнере копий. Только папки, подключенные к нему.
  local file=$1 real dir key value base
  case "$file" in
    */*) ;;
    *) echo "/backups/$file"; return ;;
  esac
  real=$(realpath -m -- "$file")
  for key in 0 1 2 3; do
    if [ "$key" = 0 ]; then value=./backups; else value=$(env_value "BACKUP_DIR_$key"); fi
    if [ "$key" = 1 ] && [ -z "$value" ]; then value=./backups; fi
    [ -n "$value" ] || continue
    base=$(realpath -m -- "$value")
    case "$real" in
      "$base"/*) echo "/copies/$key/${real#"$base"/}"; return ;;
    esac
  done
  dir=$(dirname -- "$real")
  fail "папка $dir не подключена к контейнеру копий: копия должна лежать в папке backups или в папке из BACKUP_DIR_1..3"
}

check_started() {
  local version=$1 state
  echo "Проверка запуска..."
  sleep 15
  state=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$(docker compose ps -q bot)")
  [ "${state%% *}" = "running" ] || fail "бот не запущен ($state). Журнал: docker compose logs bot"
  [ "${state##* }" = "0" ] || fail "бот перезапускается ($state). Журнал: docker compose logs bot"
  docker compose exec -T bot python -m doc_registry_bot version | grep -qx "$version" \
    || fail "запущена не та версия"
  echo "Бот работает, версия $version."
}

install() {
  local file=$1 loaded version previous
  [ -f "$file" ] || fail "нет файла $file"
  prepare
  docker compose config -q || fail "ошибка в compose.yaml или .env, обновление не начато"
  echo "Загрузка образов из $file..."
  loaded=$(docker load -i "$file")
  echo "$loaded"
  version=$(printf '%s\n' "$loaded" | sed -n 's/^Loaded image: doc-registry-bot://p' | tail -n 1)
  [ -n "$version" ] || fail "в файле нет образа doc-registry-bot"
  docker image inspect "doc-registry-backup:$version" >/dev/null 2>&1 \
    || fail "в файле нет образа doc-registry-backup:$version"
  # Настройки проверяет новая версия бота до любых изменений: ошибка в .env (например, путь
  # папки копий) не должна всплыть посреди обновления.
  echo "Проверка настроек..."
  BOT_VERSION="$version" docker compose run --rm --no-deps -T bot settings </dev/null \
    || fail "в .env ошибка (см. выше), обновление не начато, работает прежняя версия"
  previous=$(current_version)
  backup_now "$version"
  printf 'PREVIOUS=%s\n' "$previous" > "$STATE"
  set_version "$version"
  docker compose up -d
  check_started "$version"
}

rollback() {
  [ -f "$STATE" ] || fail "нет сведений о предыдущей версии ($STATE)"
  local previous
  previous=$(sed -n 's/^PREVIOUS=//p' "$STATE")
  [ -n "$previous" ] || fail "предыдущая версия неизвестна"
  docker compose config -q || fail "ошибка в compose.yaml или .env, откат не начат"
  docker image inspect "doc-registry-bot:$previous" >/dev/null 2>&1 \
    || fail "образа doc-registry-bot:$previous нет на сервере"
  printf 'PREVIOUS=%s\n' "$(current_version)" > "$STATE"
  set_version "$previous"
  prepare
  docker compose up -d
  check_started "$previous"
}

restore() {
  local dump=$1 path
  path=$(container_path "$dump")
  # </dev/null: команды в контейнере не должны забирать ввод, который ждет вопрос ниже.
  docker compose exec -T backup test -f "$path" </dev/null || fail "нет копии $dump"
  docker compose exec -T backup pg_restore -f /dev/null "$path" </dev/null \
    || fail "копия $dump не читается, восстановление остановлено"
  read -r -p "База будет заменена копией $dump. Записи после копии пропадут. Продолжить? (да/нет) " answer \
    || answer=""
  [ "$answer" = "да" ] || fail "отменено, база не менялась"
  backup_now
  stopped=$(date +%s)
  docker compose stop bot
  # База становится в точности такой, как в копии: схема очищается, копия загружается заново.
  # Таблицы новее копии бот создаст сам при запуске (миграции). Имя базы и пользователь — из
  # .env, их знает контейнер копий (PGDATABASE, PGUSER).
  docker compose exec -T -e DUMP="$path" backup sh -c \
    'psql -X -q -v ON_ERROR_STOP=1 -d "$PGDATABASE" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" && pg_restore --no-owner -d "$PGDATABASE" "$DUMP"' \
    || fail "восстановление не удалось: база может быть пустой. Повторите команду или восстановите защитную копию"
  # Сообщения, отправленные до остановки бота, выполнены прежней базой: дочитывание истории их
  # не повторит. Пришедшие, пока бот стоял, он дочитает.
  # $stopped — только цифры (date +%s). Базу и пользователя psql берет из окружения контейнера.
  docker compose exec -T backup psql -X -q -v ON_ERROR_STOP=1 -c \
    "INSERT INTO bot_state (key, value) VALUES ('restored_at', '$stopped') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" \
    </dev/null || fail "отметка восстановления не записана: не запускайте бота, повторите восстановление"
  docker compose start bot
  check_started "$(current_version)"
}

case "${1:-}" in
  --rollback) rollback ;;
  --restore) [ -n "${2:-}" ] || fail "укажите имя копии"; restore "$2" ;;
  ""|-h|--help) sed -n '2,10p' "$0" ;;
  *) install "$1" ;;
esac
