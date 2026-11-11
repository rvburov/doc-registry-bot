#!/usr/bin/env bash
# Сборка на компьютере пользователя (Windows: терминал Ubuntu в WSL, Docker Desktop; docs/pc.md).
#
#   scripts/build.sh
#
# Проверки → образы бота и ночной копии → один файл dist/doc-registry-bot-<версия>.tar.gz
# с образами бота, копии и PostgreSQL. Файл переносится на сервер по SSH вместе с
# deploy/compose.yaml и deploy/update.sh и ставится командой ./update.sh <файл>.
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || { echo "Ошибка: не найдена версия в pyproject.toml" >&2; exit 1; }

echo "Проверки..."
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q

echo "Образы $version..."
docker build -t "doc-registry-bot:$version" .
docker build -t "doc-registry-backup:$version" deploy/backup
docker image inspect postgres:16 >/dev/null 2>&1 || docker pull postgres:16

mkdir -p dist
out="dist/doc-registry-bot-$version.tar.gz"
docker save "doc-registry-bot:$version" "doc-registry-backup:$version" postgres:16 | gzip > "$out"
cp deploy/compose.yaml deploy/update.sh deploy/backup.sh deploy/import.sh deploy/users.sh \
  deploy/contacts.sh deploy/.env.example dist/
echo "Собрано: $out"
echo "На сервер: $out и файлы dist/*.sh, dist/compose.yaml (.env.example при первой установке)."
