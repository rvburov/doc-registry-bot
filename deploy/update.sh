#!/usr/bin/env bash
# Установка и обновление бота на сервере.
#
#   ./update.sh doc-registry-bot-1.2.0.tar.gz   установить версию из файла (первая установка тоже)
#   ./update.sh --rollback                      вернуть предыдущую версию
#   ./update.sh --restore ФАЙЛ.dump             восстановить базу из копии (в папке копий)
#
# Перед обновлением делается копия базы. Миграции схемы только добавляющие, поэтому прежняя
# версия бота работает и на обновленной схеме: откат меняет только образ.
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
  if running && [ -n "$(docker compose ps -q backup 2>/dev/null)" ]; then
    echo "Копия базы перед обновлением..."
    docker compose exec -T backup /usr/local/bin/backup.sh now \
      || fail "копия базы не сделана, обновление остановлено"
  else
    echo "Бот еще не запущен: копию делать не из чего."
  fi
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
  echo "Загрузка образов из $file..."
  loaded=$(docker load -i "$file")
  echo "$loaded"
  version=$(printf '%s\n' "$loaded" | sed -n 's/^Loaded image: doc-registry-bot://p' | tail -n 1)
  [ -n "$version" ] || fail "в файле нет образа doc-registry-bot"
  docker image inspect "doc-registry-backup:$version" >/dev/null 2>&1 \
    || fail "в файле нет образа doc-registry-backup:$version"
  previous=$(current_version)
  backup_now
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
  docker image inspect "doc-registry-bot:$previous" >/dev/null 2>&1 \
    || fail "образа doc-registry-bot:$previous нет на сервере"
  printf 'PREVIOUS=%s\n' "$(current_version)" > "$STATE"
  set_version "$previous"
  docker compose up -d
  check_started "$previous"
}

restore() {
  local dump=$1
  docker compose exec -T backup test -f "/backups/$dump" || fail "нет копии /backups/$dump"
  read -r -p "База будет заменена копией $dump. Записи после копии пропадут. Продолжить? (да/нет) " answer
  [ "$answer" = "да" ] || fail "отменено"
  backup_now
  docker compose stop bot
  docker compose exec -T backup pg_restore --clean --if-exists --no-owner -d doc_registry \
    "/backups/$dump"
  docker compose start bot
  check_started "$(current_version)"
}

case "${1:-}" in
  --rollback) rollback ;;
  --restore) [ -n "${2:-}" ] || fail "укажите имя копии"; restore "$2" ;;
  ""|-h|--help) sed -n '2,10p' "$0" ;;
  *) install "$1" ;;
esac
