#!/usr/bin/env bash
# Готовый файл бота с GitHub, без сборки на этом компьютере (docs/pc.md, шаг 5).
#
#   scripts/download.sh
#
# Скачивает dist/doc-registry-bot-<версия>.tar.gz той версии, что в pyproject.toml (после
# git pull — свежей). Файл собирает GitHub Actions после зеленых проверок каждой отправки в
# master и выкладывает в Releases. Сверяет SHA-256 и кладет рядом файлы для сервера, как
# scripts/build.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "Ошибка: $*" >&2; exit 1; }

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || fail "не найдена версия в pyproject.toml"
name="doc-registry-bot-$version.tar.gz"
url="https://github.com/rvburov/doc-registry-bot/releases/download/v$version"

mkdir -p dist
echo "Скачивание $name (около 200 МБ)..."
curl -fL --retry 3 --connect-timeout 30 -o "dist/$name.part" "$url/$name" \
  || fail "файл не скачан. Проверьте, что в браузере открывается $url/$name; если открывается, скачайте его там и положите в папку dist проекта"
curl -fsSL --retry 3 -o "dist/$name.sha256" "$url/$name.sha256" || fail "не скачан $name.sha256"
[ "$(sha256sum "dist/$name.part" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "dist/$name.sha256")" ] \
  || { rm -f "dist/$name.part"; fail "файл скачался с ошибкой, повторите команду"; }
mv "dist/$name.part" "dist/$name"
cp deploy/compose.yaml deploy/update.sh deploy/backup.sh deploy/import.sh deploy/users.sh \
  deploy/contacts.sh deploy/.env.example dist/
echo "Скачано: dist/$name"
