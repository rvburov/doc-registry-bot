#!/usr/bin/env bash
# Контакты учетной записи бота: ./contacts.sh
#
# Печатает «ID;ФИО» всех, кто есть в контактах бота. Так узнается свой userId для OWNER_IDS в .env
# и готовится файл для ./users.sh:
#   ./contacts.sh | grep Иванов                         найти человека
#   ./contacts.sh > /mnt/e/bot-files/контакты.csv       сохранить в файл (открывается в Excel)
# База не нужна: команда работает и до первого запуска бота, нужна только связь со службой
# мессенджера (сеть messenger и MESSENGER_URL в .env).
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "Ошибка: $*" >&2; exit 1; }

[ -f .env ] || fail "нет файла .env рядом с compose.yaml (образец: .env.example)"
command -v docker >/dev/null || fail "не установлен Docker"

docker compose --progress quiet run --rm -T --no-deps bot contacts
