#!/usr/bin/env bash
# Резервная копия из терминала: ./backup.sh
#
# Сохраняет в папку backups/ рядом с этим файлом, одним снимком базы:
#   база-ГГГГ-ММ-ДД_ЧЧММСС.dump    полная копия базы: журнал, доступы, журнал действий;
#   журнал-ГГГГ-ММ-ДД_ЧЧММСС.csv   весь журнал КД (файл делается и для пустого журнала).
#
# Файлы пишет контейнер копий (pg_dump той же версии, что база), копия записывается в базу.
# После копии 30 минут можно заменить журнал: здесь ./import.sh ФАЙЛ или в чате «Заменить журнал».
# Выгрузить журнал без замены тоже можно этой командой: возьмите файл журнал-....csv.
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "Ошибка: $*" >&2; exit 1; }

[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

umask 077
# Папку создает пользователь до запуска контейнеров: иначе Docker создал бы ее от root.
# Метка .doc-registry-copies говорит контейнеру копий, что папка настоящая.
mkdir -p backups
[ -w backups ] || fail "в папку backups нельзя записать: sudo chown $(id -un) backups"
touch backups/.doc-registry-copies
# Папка бота на компьютере: по ней бот показывает в чате путь ./backups.
if ! grep -q "^BOT_DIR=$(pwd -P)\$" .env; then
  if grep -q '^BOT_DIR=' .env; then
    sed -i "s|^BOT_DIR=.*|BOT_DIR=$(pwd -P)|" .env
  else
    printf '\nBOT_DIR=%s\n' "$(pwd -P)" >> .env
  fi
fi

out=$(mktemp)
log=$(mktemp)
trap 'rm -f "$out" "$log"' EXIT

echo "Запуск базы..."
if ! docker compose --progress quiet up -d --wait postgres >"$log" 2>&1; then
  cat "$log" >&2
  fail "база не запустилась, резерв не сделан (docker compose ps)"
fi

old_backup() {
  # Образ бота прежней версии (после отката на версию до 1.2.0): копия по-старому, без
  # контейнера копий и без записи в базу. Для ./import.sh годится, для замены через чат — нет.
  local stamp journal database
  stamp=$(date +%Y-%m-%d_%H%M%S)
  journal="журнал-$stamp.csv"
  database="база-$stamp.dump"
  if ! docker compose --progress quiet run --rm -T bot export - >"backups/$journal" 2>"$log"; then
    cat "$log" >&2
    rm -f "backups/$journal"
    fail "журнал не выгружен, резерв не сделан"
  fi
  printf 'Журнал: %s\nБаза: %s\nЗаписей: %s\nОтпечаток: %s\n' "$journal" "$database" \
    "$(sed -n 's/^Выгружено записей: //p' "$log" | tail -n 1)" \
    "$(sed -n 's/^Отпечаток: //p' "$log" | tail -n 1)" >"$out"
  # shellcheck disable=SC2016  # переменные раскрывает shell в контейнере PostgreSQL
  if ! docker compose exec -T postgres \
    sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"backups/$database"; then
    rm -f "backups/$database" "backups/$journal"
    fail "копия базы не сделана, резерв не сделан"
  fi
  echo "Копия сделана по-старому, без контейнера копий: для ./import.sh она годится, для замены журнала через чат нет."
  OLD=1
}

OLD=

echo "Резервная копия..."
if ! docker compose --progress quiet up -d --wait backup >"$log" 2>&1; then
  cat "$log" >&2
  echo "Контейнер копий не запустился: копия делается по-старому, без него." >&2
  old_backup
elif docker compose --progress quiet run --rm -T bot backup --uid "$(id -u)" --gid "$(id -g)" \
  >"$out" 2>"$log"; then
  :
elif grep -q "^Настройка " "$log"; then
  # Ошибка в .env: старый способ ее не обойдет, сначала исправить настройку.
  cat "$log" >&2
  fail "в .env ошибка, резерв не сделан"
elif grep -q "Неизвестная команда" "$log"; then
  echo "Образ бота прежней версии (после отката): копия делается по-старому." >&2
  old_backup
else
  cat "$log" >&2
  echo "Копия через контейнер копий не получилась: копия делается по-старому." >&2
  old_backup
fi
journal=$(sed -n 's/^Журнал: //p' "$out" | tail -n 1)
database=$(sed -n 's/^База: //p' "$out" | tail -n 1)
rows=$(sed -n 's/^Записей: //p' "$out" | tail -n 1)
fingerprint=$(sed -n 's/^Отпечаток: //p' "$out" | tail -n 1)
if [ -z "$journal" ] || [ -z "$database" ] || [ -z "$fingerprint" ]; then
  cat "$out" "$log" >&2
  fail "нет итога копии, резерв не сделан"
fi

# Отметка для ./import.sh: когда сделан резерв и каким был журнал в эту минуту.
printf 'TIME=%s\nFINGERPRINT=%s\nJOURNAL=%s\n' "$(date +%s)" "$fingerprint" "backups/$journal" \
  > backups/.last-backup

echo "Резерв готов, записей в журнале: $rows."
echo "  backups/$journal"
echo "  backups/$database"
if [ -n "$OLD" ]; then
  echo "В течение 30 минут можно заменить журнал: ./import.sh ФАЙЛ"
else
  echo "В течение 30 минут можно заменить журнал: ./import.sh ФАЙЛ (или в чате: Заменить журнал)"
fi
