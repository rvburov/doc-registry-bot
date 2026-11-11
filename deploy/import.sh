#!/usr/bin/env bash
# Замена всего журнала КД файлом: ./import.sh журнал.csv (подходит и .xlsx)
#
# Работает только после резерва ./backup.sh, сделанного не раньше чем 30 минут назад, и только
# если журнал с тех пор не менялся (никто не зарегистрировал номер). Иначе отказ, база не
# трогается. Ошибка хоть в одной строке файла: не загружается ничего.
#
# Доступы пользователей и журнал действий не меняются: заменяются только записи журнала КД.
# Вернуть журнал из резерва: ./backup.sh, затем ./import.sh backups/журнал-....csv
set -euo pipefail

fail() { echo "Ошибка: $*" >&2; exit 1; }

MAX_AGE=1800

[ -n "${1:-}" ] || { sed -n '2,9p' "$0"; exit 2; }
[ -f "$1" ] || fail "нет файла $1"
file=$(realpath -- "$1")
name=$(basename -- "$1")

cd "$(dirname "$0")"
[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

marker=backups/.last-backup
[ -f "$marker" ] || fail "резерва нет. Сначала сделайте резерв: ./backup.sh"
made=$(sed -n 's/^TIME=//p' "$marker")
fingerprint=$(sed -n 's/^FINGERPRINT=//p' "$marker")
saved=$(sed -n 's/^JOURNAL=//p' "$marker")
[ -n "$made" ] && [ -n "$fingerprint" ] || fail "отметка резерва повреждена. Сделайте резерв: ./backup.sh"
age=$(( $(date +%s) - made ))
if [ "$age" -gt "$MAX_AGE" ]; then
  fail "резерв сделан $((age / 60)) мин назад, а нужен не старше 30 минут. Сделайте резерв: ./backup.sh"
fi

echo "Проверка файла $name..."
docker compose --progress quiet run --rm -T bot check - --name "$name" < "$file" || exit 1

read -r -p "Весь журнал КД будет заменен файлом $name. Резерв: $saved. Продолжить? (да/нет) " answer \
  || answer=""
[ "$answer" = "да" ] || fail "отменено, журнал не изменен"

docker compose --progress quiet run --rm -T bot \
  import - --name "$name" --expect "$fingerprint" < "$file" || exit 1
# Резерв описывал прежний журнал: для следующей замены нужен новый.
rm -f "$marker"
echo "Вернуть прежний журнал: ./backup.sh, затем ./import.sh $saved"
