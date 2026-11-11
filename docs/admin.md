# Регистрация КД: инструкция администратора

Один путь по шагам: сборка на ПК, подготовка сервера, переключение со SmartBase, обновление,
откат и резервные копии. Решения, на которых все построено, — в [decisions.md](decisions.md).

Команды на ПК выполняются в Git Bash, на сервере — в терминале по SSH.

## 1. Что нужно

- **ПК (Windows):** Docker Desktop, Git Bash, `uv` (`pip install uv`), клон репозитория.
- **Сервер:** виртуальная машина Ubuntu с доступом по SSH и интернетом для установки Docker и
  обновлений Ubuntu. Служба мессенджера работает на этом же сервере в своем Docker.
- **Сведения:** ваш числовой `userId` в мессенджере, коды организации, сетевая папка для копий
  и учетная запись с правом записи в нее.

## 2. Сборка на ПК

```bash
cd doc-registry-bot
git pull
scripts/build.sh
```

Скрипт прогоняет проверки и тесты, собирает образы и пишет в `dist/`:

- `doc-registry-bot-<версия>.tar.gz` — образы бота, копии и PostgreSQL одним файлом;
- `compose.yaml`, `update.sh`, `.env.example`.

Тесты базы нуждаются в PostgreSQL: перед первой сборкой
`docker compose -f tests/compose.yaml up -d`.

## 3. Подготовка сервера (один раз)

Docker Engine:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
sudo timedatectl set-timezone Europe/Moscow
```

После `usermod` выйдите из SSH и зайдите снова.

Общая сеть со службой мессенджера:

```bash
docker network create messenger
```

В `compose` службы мессенджера подключите ее к этой сети и уберите публикацию порта 8888 наружу:
у службы нет авторизации, и кто до нее достучится, пишет от имени бота.

```yaml
services:
  <имя службы>:
    # строку ports с 8888 удалить
    networks:
      - messenger

networks:
  messenger:
    external: true
```

Затем перезапустите службу мессенджера (`docker compose up -d` в ее папке) и запишите имя ее
контейнера: `docker ps --format '{{.Names}}'`.

Папка бота и файлы с ПК:

```bash
sudo mkdir -p /opt/doc-registry-bot
sudo chown "$USER" /opt/doc-registry-bot
```

На ПК:

```bash
scp dist/doc-registry-bot-1.0.0.tar.gz dist/compose.yaml dist/update.sh dist/.env.example пользователь@сервер:/opt/doc-registry-bot/
```

На сервере:

```bash
cd /opt/doc-registry-bot
cp .env.example .env
chmod 600 .env
chmod +x update.sh
nano .env
```

В `.env` заполните `MESSENGER_URL` (имя контейнера службы и порт 8888), `OWNER_IDS`,
`ORG_CODES`, базу (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`) и сетевую папку
`BACKUP_SHARE*`. Строки базы заполняются до первого запуска: потом PostgreSQL их не перечитывает.

## 4. День переключения

Бот и сценарий SmartBase не должны отвечать одновременно: на одно сообщение будет две
регистрации.

1. **SmartBase:** «Настройки → Интеграции → Сценарии», выключите сценарий «Регистрация КД по
   сообщению».
2. **SmartBase:** выгрузите коллекцию журнала в Excel целиком (со своими столбцами) и сохраните
   файл как архив. Перенесите его на сервер: `scp журнал.xlsx пользователь@сервер:/opt/doc-registry-bot/`.
3. **Сервер:** загрузите образы и журнал:

   ```bash
   cd /opt/doc-registry-bot
   docker load -i doc-registry-bot-1.0.0.tar.gz
   docker compose run --rm -v "$PWD/журнал.xlsx:/data/journal.xlsx:ro" bot import /data/journal.xlsx
   ```

   Ответ «Журнал загружен: N записей.» — все строки в базе. Если есть ошибки (повтор
   обозначения, неразобранная дата), не загружается ничего: исправьте журнал в SmartBase,
   выгрузите заново и повторите команду.
4. **Сервер:** запустите бота:

   ```bash
   ./update.sh doc-registry-bot-1.0.0.tar.gz
   ```

   Строка «Бот работает, версия 1.0.0.» — бот запущен. Вам в мессенджер придет «Бот регистрации КД
   запущен, версия 1.0.0.».
5. **Проверка:** отправьте боту «Выгрузить журнал» и сверьте число записей и последние номера нескольких
   серий с журналом SmartBase.
6. **Доступ:** выдайте доступ сотрудникам командой «Выдать доступ: ФИО, ФИО, ...» (полное ФИО
   или ID) и ответьте «Без инструкции». Новым сотрудникам в будущем отвечайте «Да»: им уйдет
   инструкция, а бот сообщит, кому она не дошла.

Коллекцию журнала в SmartBase не удаляйте: это архив.

**Если что-то пошло не так:** `docker compose stop bot`, включите сценарий в SmartBase. Номера,
выданные ботом за это время, получите командой «Выгрузить журнал» (до остановки) и допишите в журнал
SmartBase, иначе SmartBase выдаст их повторно.

## 5. Обновление

На ПК: `git pull`, `scripts/build.sh`, затем

```bash
scp dist/doc-registry-bot-1.1.0.tar.gz пользователь@сервер:/opt/doc-registry-bot/
```

На сервере:

```bash
cd /opt/doc-registry-bot
./update.sh doc-registry-bot-1.1.0.tar.gz
```

Скрипт делает копию базы, загружает образы, перезапускает бота и проверяет, что поднялась нужная
версия. Если изменились `compose.yaml` или `update.sh`, скопируйте их с ПК тоже.

**Откат на предыдущую версию:** `./update.sh --rollback`. Данные не теряются: схема базы меняется
только добавлением, и прежняя версия на ней работает.

## 6. Резервные копии

- Каждую ночь в `BACKUP_TIME` (по умолчанию 03:00) делается копия базы. Хранится
  `BACKUP_KEEP_DAYS` дней (по умолчанию 30) на сервере и в сетевой папке.
- Если копия не сделана или не попала в сетевую папку, бот пишет владельцу.
- Копия вручную: `docker compose exec backup /usr/local/bin/backup.sh now`.
- Список копий на сервере: `docker compose exec backup ls -l /backups`.
- **Восстановление** (только при беде, записи после копии пропадут):

  ```bash
  docker compose cp doc-registry-2026-11-10_030000.dump backup:/backups/   # если копия из сетевой папки
  ./update.sh --restore doc-registry-2026-11-10_030000.dump
  ```

## 7. Присмотр

- Журнал программы: `docker compose logs -f bot`.
- Состояние контейнеров: `docker compose ps`.
- Владельцу приходят: «бот запущен» при старте, «связь восстановлена» после перерыва дольше
  5 минут, сообщения о сбое копии.
- Изменили `.env` — примените командой `docker compose up -d` (простой `restart` новый `.env` не
  перечитает).

## 8. Проверка на ПК

Тот же порядок, что на сервере, в Docker Desktop: служба мессенджера в своем контейнере, общая
сеть `messenger`, `.env` с рабочей учетной записью бота. **На время проверки выключите сценарий
бота в SmartBase**, после проверки остановите бота (`docker compose down`) и включите сценарий
обратно.

## 9. Команды в боте

Управление — сообщениями боту в мессенджере. Полный список с примерами бот присылает сам:
напишите ему «Инструкция администратора». Коротко:

| Команда | Что делает |
|---|---|
| `Выдать доступ: ФИО или ID, ...` | выдать доступ; ответ «Да», «Без инструкции» или «Нет» |
| `Удалить доступ: ФИО или ID` | снять доступ после «Да» |
| `Отправить инструкцию: ФИО или ID` | повторно отправить инструкцию тому, у кого есть доступ |
| `Пользователи` | у кого есть доступ |
| `Выгрузить журнал` | журнал КД файлом CSV |
| `Журнал действий` | кто что делал, файлом CSV за год |
| `Отправить сообщение: ФИО или ID, ...` и со следующей строки `Сообщение: текст` | сообщение с файлами после «Да»; «всем» — всем с доступом |
| `Назначить администратора: ФИО или ID` | только владелец |
| `Снять администратора: ФИО или ID` | только владелец |
| `Администраторы` | только владелец |

Людей указывайте полным ФИО или ID: часть ФИО бот не примет. Если полное ФИО совпало у двух
человек, бот покажет их ID и попросит повторить команду с ID.
