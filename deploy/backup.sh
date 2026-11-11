#!/usr/bin/env bash
# Резерв журнала: ./backup.sh
#
# Сохраняет в папку backups/ рядом с этим файлом:
#   журнал-ГГГГ-ММ-ДД_ЧЧММСС.csv   весь журнал КД (файл делается и для пустого журнала);
#   база-ГГГГ-ММ-ДД_ЧЧММСС.dump    полная копия базы: журнал, доступы, журнал действий.
#
# После резерва 30 минут можно заменить журнал файлом: ./import.sh ФАЙЛ.
# Выгрузить журнал без замены тоже можно этой командой: возьмите файл журнал-....csv.
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "Ошибка: $*" >&2; exit 1; }

[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

umask 077
mkdir -p backups
stamp=$(date +%Y-%m-%d_%H%M%S)
journal="backups/журнал-$stamp.csv"
database="backups/база-$stamp.dump"
log=$(mktemp)
trap 'rm -f "$log"' EXIT

echo "Выгрузка журнала..."
if ! docker compose --progress quiet run --rm -T bot export - > "$journal" 2> "$log"; then
  cat "$log" >&2
  rm -f "$journal"
  fail "журнал не выгружен, резерв не сделан"
fi
fingerprint=$(sed -n 's/^Отпечаток: //p' "$log" | tail -n 1)
rows=$(sed -n 's/^Выгружено записей: //p' "$log" | tail -n 1)
if [ -z "$fingerprint" ]; then
  cat "$log" >&2
  rm -f "$journal"
  fail "у выгрузки нет отпечатка, резерв не сделан"
fi

echo "Полная копия базы..."
# Имя базы и пользователь известны контейнеру PostgreSQL из .env.
if ! docker compose exec -T postgres \
  sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$database"; then
  rm -f "$database" "$journal"
  fail "копия базы не сделана, резерв не сделан"
fi

# Отметка для ./import.sh: когда сделан резерв и каким был журнал в эту минуту.
printf 'TIME=%s\nFINGERPRINT=%s\nJOURNAL=%s\n' "$(date +%s)" "$fingerprint" "$journal" \
  > backups/.last-backup

echo "Резерв готов, записей в журнале: $rows."
echo "  $journal"
echo "  $database"
echo "В течение 30 минут можно заменить журнал: ./import.sh ФАЙЛ"
