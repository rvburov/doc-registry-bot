#!/usr/bin/env bash
# Доступ к боту всем из файла: ./users.sh пользователи.csv
#
# В файле два столбца, ID и ФИО, в первой строке заголовок. Разделитель «;» или «,».
# Инструкция не отправляется. Права администратора не меняются, у кого доступ уже есть,
# обновляется только ФИО. Ошибка хоть в одной строке: не загружается ничего.
set -euo pipefail

fail() { echo "Ошибка: $*" >&2; exit 1; }

[ -n "${1:-}" ] || { sed -n '2,7p' "$0"; exit 2; }
[ -f "$1" ] || fail "нет файла $1"
file=$(realpath -- "$1")
name=$(basename -- "$1")

cd "$(dirname "$0")"
[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

docker compose --progress quiet run --rm -T bot users - --name "$name" < "$file"
