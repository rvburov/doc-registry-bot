# Регистрация КД: бот на сервере (Ubuntu на виртуальной машине)

Пошагово: подготовка сервера, связь со службой мессенджера, перенос всей базы с ПК, запуск,
проверка, выгрузка файлов, обновление и остановка.

Команды на сервере выполняются в терминале по SSH, команды «на ПК» — в терминале Ubuntu внутри
WSL ([pc.md](pc.md)). Где выполнять, написано в начале каждого шага.

| Где | Папка | Что |
|---|---|---|
| ПК | `~/doc-registry-bot` | исходный код, сборка |
| ПК | `~/kd-bot` | бот на ПК (до переезда) |
| ПК | `C:\bot-files` = `/mnt/c/bot-files` | обмен файлами с Windows |
| сервер | `/opt/doc-registry-bot` | рабочая папка бота: `.env`, скрипты, резервы |
| сервер | папка службы мессенджера | ее `compose.yaml` (шаг 4 покажет, где) |

В примерах `admin@10.0.0.5` — ваш пользователь и адрес сервера, `1.1.0` — версия бота из строки
«Собрано: …» при сборке.

## 0. Что нужно заранее

- адрес сервера и пользователь Ubuntu с правом `sudo`, пароль от него;
- ваш числовой userId в мессенджере и коды организации (те же, что в `.env` на ПК);
- сетевая папка для ночных копий: путь `//сервер/папка`, пользователь и пароль с правом записи.
  Если ее пока нет, бот работает и без нее;
- собранный на ПК бот ([pc.md](pc.md), шаг 5).

## 1. Вход на сервер (на ПК)

```bash
ssh admin@10.0.0.5
```

При первом входе ответьте `yes` на вопрос о ключе сервера и введите пароль. Выйти с сервера:
`exit`.

Чтобы не вводить пароль каждый раз (один раз, на ПК):

```bash
ssh-keygen -t ed25519
ssh-copy-id admin@10.0.0.5
```

На вопросы `ssh-keygen` отвечайте `Enter`.

## 2. Ubuntu (один раз, на сервере)

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo timedatectl set-timezone Europe/Moscow
timedatectl
```

В выводе последней команды: `Time zone: Europe/Moscow`.

## 3. Docker (один раз, на сервере)

Служба мессенджера уже работает в Docker, поэтому сначала проверьте, что он есть:

```bash
docker version
docker compose version
snap list docker
```

- Обе первые команды показали версии, а `snap list docker` ответил, что такого нет (или команды
  `snap` нет вовсе): Docker готов, переходите к концу шага (группа `docker`).
- `snap list docker` показал `docker`: Docker установлен из snap, и с папкой `/opt` он работать
  не будет. Остановитесь: этот случай инструкция не покрывает, Docker нужно ставить заново
  отдельно, вместе со службой мессенджера.
- `docker: command not found`: установите Docker:

  ```bash
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  ```

Группа `docker`, чтобы команды шли без `sudo`:

```bash
sudo usermod -aG docker "$USER"
exit
```

Зайдите снова (`ssh admin@10.0.0.5`) и проверьте: `docker ps` показывает список контейнеров без
ошибки `permission denied`.

## 4. Связь со службой мессенджера (один раз, на сервере)

Бот ходит к службе мессенджера через общую сеть Docker `messenger`.

```bash
docker network create messenger
docker ps --format '{{.Names}}'
```

Найдите в списке службу мессенджера. Дальше вместо `ИМЯ` — ее имя.

```bash
docker port ИМЯ
docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' ИМЯ
```

- `docker port` покажет строку вида `8888/tcp -> 0.0.0.0:8888`. Число слева — порт службы внутри
  Docker, он нужен для `MESSENGER_URL` (шаг 7).
- Вторая команда покажет папку, из которой служба запущена (ее `compose.yaml`). Пусто — служба
  запущена без `compose`.

**Служба запущена из compose (папка показана).** Подключение к сети записывается в ее
`compose.yaml` и переживает перезапуски и обновления службы:

```bash
cd ПАПКА_ИЗ_ВЫВОДА
cp compose.yaml compose.yaml.bak
nano compose.yaml
```

В описании службы добавьте сети (если у службы уже есть `networks:`, допишите в него строку
`- messenger`), а в конце файла — раздел `networks`:

```yaml
services:
  ИМЯ_СЛУЖБЫ_В_ФАЙЛЕ:
    # ... все, что уже есть, не трогать, строку ports с 8888 оставить
    networks:
      - default
      - messenger

networks:
  messenger:
    external: true
```

`default` обязателен: без него служба потеряет связь с соседями по своему `compose`. Если раздел
`networks:` в конце файла уже есть, добавьте в него только `messenger` с `external: true`.

Сохраните (`Ctrl+O`, `Enter`, `Ctrl+X`) и примените:

```bash
docker compose up -d
```

**Служба запущена без compose (пусто):**

```bash
docker network connect messenger ИМЯ
```

Если контейнер службы когда-нибудь пересоздадут (обновят), повторите эту команду.

**Проверка:**

```bash
docker network inspect messenger --format '{{range .Containers}}{{.Name}} {{end}}'
```

В выводе есть имя службы.

Порт 8888 службы остается открытым: к службе могут подключаться другие программы с компьютеров
сети. Боту порт не нужен. Как посмотреть, кто подключен, и как закрыть порт потом, — шаг 13.

## 5. Рабочая папка бота (один раз, на сервере)

```bash
sudo mkdir -p /opt/doc-registry-bot
sudo chown "$USER": /opt/doc-registry-bot
```

## 6. Файлы бота на сервер (на ПК)

```bash
cd ~/doc-registry-bot
scp dist/doc-registry-bot-1.1.0.tar.gz dist/*.sh dist/compose.yaml dist/.env.example admin@10.0.0.5:/opt/doc-registry-bot/
```

Файл образов около 200 МБ, копирование занимает несколько минут.

## 7. Настройки `.env` (один раз, на сервере)

```bash
cd /opt/doc-registry-bot
cp .env.example .env
chmod 600 .env
chmod +x *.sh
nano .env
```

| Строка | Что вписать |
|---|---|
| `BOT_VERSION` | версию (`1.1.0`) |
| `MESSENGER_URL` | `http://ИМЯ:8888`: имя службы и порт из шага 4 |
| `OWNER_IDS` | ваш userId (как на ПК) |
| `ORG_CODES` | коды организации (как на ПК) |
| `POSTGRES_PASSWORD` | новый длинный пароль базы сервера (с ПК копировать не обязательно) |
| `BACKUP_SHARE` | сетевая папка `//сервер/папка`; нет папки — оставьте пустым |
| `BACKUP_SHARE_USER`, `BACKUP_SHARE_PASSWORD`, `BACKUP_SHARE_DOMAIN` | учетная запись для папки; домен, если нужен |

Сохраните: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 8. Образы (на сервере)

```bash
cd /opt/doc-registry-bot
docker load -i doc-registry-bot-1.1.0.tar.gz
```

В конце три строки `Loaded image: …`: бот, копия, PostgreSQL.

## 9. Перенос всей базы с ПК

Переносится все: журнал, пользователи с доступом, администраторы, журнал действий и место, до
которого бот прочитал сообщения. Шаги 9 и 10 делайте подряд: с остановки бота на ПК до запуска
на сервере бот никому не отвечает. Сообщения за это время бот на сервере прочитает сам после
запуска (за последние 24 часа).

**На ПК** остановите бота и сделайте копию:

```bash
cd ~/kd-bot
docker compose stop bot
./backup.sh
```

Запомните из вывода число «записей в журнале» и имя файла `backups/база-<дата_время>.dump`.
Отправьте копию на сервер (имя файла — из вывода):

```bash
scp backups/база-2026-11-12_101500.dump admin@10.0.0.5:/opt/doc-registry-bot/
docker compose down
```

`docker compose down` выключает бота на ПК совсем. **Больше его не запускайте:** два бота на
одной учетной записи дают по два ответа и по две регистрации.

**На сервере** восстановите копию в базу сервера:

```bash
cd /opt/doc-registry-bot
docker compose up -d --wait postgres
docker compose exec -T postgres sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < база-2026-11-12_101500.dump
./backup.sh
```

`./backup.sh` должен показать то же число «записей в журнале», что на ПК. Если что-то пошло не
так, команду `pg_restore` можно повторить: она каждый раз заменяет базу копией целиком.

## 10. Запуск (на сервере)

```bash
cd /opt/doc-registry-bot
./update.sh doc-registry-bot-1.1.0.tar.gz
```

Строка «Бот работает, версия 1.1.0.» означает, что бот запущен. В мессенджер вам придет «Бот
регистрации КД запущен, версия 1.1.0.».

## 11. Проверка

В мессенджере:

1. «Помощь»: список команд.
2. «Пользователи»: те же люди, что были на ПК.
3. «Выгрузить журнал»: файл с тем же числом записей.

На сервере, ночная копия вручную (заодно проверка сетевой папки):

```bash
cd /opt/doc-registry-bot
docker compose exec backup /usr/local/bin/backup.sh now
```

Строка `Копия: doc-registry-….dump, сетевая папка: true` означает, что копия есть и на сервере, и
в сетевой папке. `false` и текст ошибки: проверьте `BACKUP_SHARE*` в `.env` и примените
`docker compose up -d`.

Перезагрузка сервера (один раз, чтобы убедиться, что бот поднимается сам):

```bash
sudo reboot
```

Через минуту-две вам придет «Бот регистрации КД запущен, версия 1.1.0.».

## 12. Файлы: выгрузка и загрузка

**Выгрузить через мессенджер:** «Выгрузить журнал», «Журнал действий».

**Выгрузить через терминал.** На сервере:

```bash
cd /opt/doc-registry-bot
./backup.sh
```

На ПК (имя файла — из вывода `backup.sh`):

```bash
scp admin@10.0.0.5:/opt/doc-registry-bot/backups/журнал-2026-11-12_101500.csv /mnt/c/bot-files/
```

Файл появится в `C:\bot-files`, открывается в Excel двойным щелчком.

**Загрузить журнал с ПК на сервер** (заменяет весь журнал). На ПК:

```bash
scp /mnt/c/bot-files/журнал.csv admin@10.0.0.5:/opt/doc-registry-bot/
```

На сервере:

```bash
cd /opt/doc-registry-bot
./backup.sh
./import.sh журнал.csv
```

**Пользователи списком:** так же, файлом `пользователи.csv` и командой `./users.sh
пользователи.csv`. Подробно о замене журнала и списке пользователей: [admin.md](admin.md),
раздел 5.

## 13. Порт 8888 службы мессенджера

Кто сейчас подключен к службе (на сервере, вместо `ИМЯ` — имя службы):

```bash
sudo nsenter -t "$(docker inspect -f '{{.State.Pid}}' ИМЯ)" -n ss -tn state established '( sport = :8888 )'
```

В столбце `Peer Address` адреса подключенных. `172.…` — бот и другие контейнеры Docker,
остальные адреса — компьютеры сети.

Закрыть порт, когда снаружи к службе никто не подключается: в `compose.yaml` службы удалите строку
с `8888` в разделе `ports` (сделайте копию файла, как в шаге 4) и выполните `docker compose up -d`
в ее папке. Бот продолжит работать: он ходит через сеть `messenger`.

## 14. Каждый день

- Бот и копии поднимаются сами после перезагрузки сервера.
- Журнал работы бота: `cd /opt/doc-registry-bot && docker compose logs -f bot` (выйти:
  `Ctrl+C`).
- Состояние: `cd /opt/doc-registry-bot && docker compose ps`.
- Владельцу приходят: «бот запущен» при старте, «связь восстановлена» после перерыва дольше 5
  минут, сообщения о сбое ночной копии.
- Поменяли `.env`: `docker compose up -d` (простой `restart` новый `.env` не перечитает).

## 15. Обновление

На ПК:

```bash
cd ~/doc-registry-bot
git pull
scripts/build.sh
scp dist/doc-registry-bot-1.2.0.tar.gz dist/*.sh dist/compose.yaml admin@10.0.0.5:/opt/doc-registry-bot/
```

На сервере:

```bash
cd /opt/doc-registry-bot
./update.sh doc-registry-bot-1.2.0.tar.gz
```

`update.sh` сам делает копию базы, загружает образы, перезапускает бота и проверяет версию. Откат
на прежнюю версию: `./update.sh --rollback`.

Новую версию до сервера на ПК не запускайте с рабочей учетной записью бота: на сервере в это
время работает бот, ответов будет два. Сборки и тестов (`scripts/build.sh`) это не касается.

## 16. Остановка

```bash
cd /opt/doc-registry-bot
./backup.sh                    # журнал со всеми выданными номерами, на всякий случай
docker compose stop bot        # остановить бота, база и данные остаются
docker compose start bot       # запустить снова
```

Восстановление базы из ночной копии (только при беде): [admin.md](admin.md), раздел 7.

## 17. Если не получается

| Что видно | Что сделать |
|---|---|
| `permission denied` и `docker.sock` | шаг 3, группа `docker`: `exit` и войти снова |
| `network messenger ... not found` | шаг 4: `docker network create messenger` |
| `Ошибка: нет файла .env` | вы не в рабочей папке: `cd /opt/doc-registry-bot` |
| `задайте POSTGRES_PASSWORD в .env` и похожие | заполните строку в `.env` (шаг 7) |
| «Бот перезапускается» после `update.sh` | `docker compose logs bot`: обычно ошибка в `.env` |
| бот работает, но не отвечает | `docker compose logs bot`; имя и порт в `MESSENGER_URL`; проверка шага 4 |
| служба мессенджера перестала работать после шага 4 | верните файл: `cp compose.yaml.bak compose.yaml`, `docker compose up -d` |
| `scp: No such file or directory` | проверьте имя файла (из вывода `backup.sh`) и папку |
