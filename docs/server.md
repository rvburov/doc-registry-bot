# Регистрация КД: бот на сервере (Ubuntu на виртуальной машине)

Пошагово: проверка и подготовка сервера, связь со службой мессенджера, перенос всей базы с ПК,
запуск, проверка, выгрузка файлов, обновление и остановка. **Каждый шаг подготовки начинается с
проверки:** что уже есть, то не ставится заново. Шаг 2 проверяет весь сервер сразу и говорит,
какие шаги пропустить.

Команды на сервере выполняются в терминале по SSH, команды «на ПК» — в терминале Ubuntu внутри
WSL ([pc.md](pc.md)). Где выполнять, написано в начале каждого шага.

| Где | Папка | Что |
|---|---|---|
| ПК | `~/doc-registry-bot` | исходный код, сборка |
| ПК | `~/kd-bot` | бот на ПК (до переезда) |
| ПК | `E:\bot-files` = `/mnt/e/bot-files` | ваши файлы в Windows (диск E:) |
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

**Вход без пароля** (по желанию). Если `ssh admin@10.0.0.5` уже пускает без пароля, это сделано,
пропустите. Иначе на ПК проверьте, есть ли ключ:

```bash
ls ~/.ssh/id_ed25519.pub
```

- `No such file or directory`: ключа нет, создайте его (на вопросы отвечайте `Enter`):

  ```bash
  ssh-keygen -t ed25519
  ```

- Файл есть: ключ уже создан, `ssh-keygen` не запускайте (он предложит затереть ключ).

Затем передайте ключ серверу (спросит пароль один последний раз):

```bash
ssh-copy-id admin@10.0.0.5
```

## 2. Проверка сервера и Ubuntu (на сервере)

Вставьте блок целиком:

```bash
. /etc/os-release; echo "Ubuntu: $VERSION_ID"
docker version --format 'Docker: {{.Server.Version}}'
docker compose version
snap list docker 2>/dev/null | grep -w docker
id -nG | grep -qw docker && echo "Группа docker: есть" || echo "Группа docker: нет"
date +'Время: %Z'
docker network inspect messenger --format 'Сеть messenger: {{range .Containers}}{{.Name}} {{end}}'
docker ps --format 'Контейнер: {{.Names}}'
ls -d /opt/doc-registry-bot
```

| Строка вывода | В порядке, если | Иначе |
|---|---|---|
| `Ubuntu: …` | `22.04` или новее | обновите Ubuntu до 22.04 или 24.04 (вне этой инструкции) |
| `Docker: …` | есть версия | `command not found` — шаг 3, установка; `permission denied` — шаг 3, группа |
| `Docker Compose version …` | версия `v2…` или новее | шаг 3, установка |
| строка со словом `docker` от snap | такой строки нет | шаг 3, Docker из snap |
| `Группа docker: …` | `есть` | шаг 3, группа |
| `Время: …` | `MSK` | ниже в этом шаге |
| `Сеть messenger: …` | есть имя службы мессенджера | шаг 4 |
| `Контейнер: …` | среди строк есть служба мессенджера | служба не запущена: запустите ее в ее папке (`docker compose up -d`) |
| `ls: /opt/doc-registry-bot` | папка есть, если бот уже ставился | шаг 5 |

Строки с `command not found` или `No such …` — этого нет, нужен шаг из таблицы.

**Обновления Ubuntu** (по желанию, раз в месяц):

```bash
sudo apt-get update
sudo apt-get upgrade -y
```

Если в обновлении есть пакеты Docker, Docker перезапустится, а вместе с ним на минуту
перезапустятся контейнеры, в том числе служба мессенджера и бот.

**Время не `MSK`:**

```bash
sudo timedatectl set-timezone Europe/Moscow
```

Повторите проверку: `Время: MSK`.

## 3. Docker (только если нет по шагу 2)

Служба мессенджера уже работает в Docker, поэтому обычно Docker есть, и нужна только группа.

**Docker из snap** (в шаге 2 была строка `docker` от snap): с папкой `/opt` такой Docker работать
не будет. Остановитесь: этот случай инструкция не покрывает, Docker нужно ставить заново
отдельно, вместе со службой мессенджера.

**Установка** (в шаге 2 `docker: command not found`):

```bash
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Нет только `docker compose` (в шаге 2 Docker есть, а Compose нет):

```bash
sudo apt-get install -y docker-compose-plugin
```

**Группа `docker`** (в шаге 2 `Группа docker: нет` или `permission denied`), чтобы команды шли
без `sudo`:

```bash
sudo usermod -aG docker "$USER"
exit
```

Зайдите снова (`ssh admin@10.0.0.5`) и повторите проверку из шага 2.

## 4. Связь со службой мессенджера (на сервере)

Бот ходит к службе мессенджера через общую сеть Docker `messenger`. Что показала строка
`Сеть messenger:` в шаге 2:

| Что видно | Что делать |
|---|---|
| имя службы мессенджера | связь уже есть: только узнайте порт (`docker port ИМЯ`, ниже) и переходите к шагу 5 |
| `Error: No such network` | создайте сеть: `docker network create messenger`, дальше весь шаг |
| пусто или имени службы нет | дальше весь шаг, кроме создания сети |

Имена контейнеров: `docker ps --format '{{.Names}}'`. Найдите службу мессенджера. Дальше вместо
`ИМЯ` — ее имя.

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

Если в файле уже есть строка `- messenger` в описании службы, файл менять не нужно: выполните
только `docker compose up -d` ниже. Иначе в описании службы добавьте сети (если у службы уже есть
`networks:`, допишите в него строку
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

## 5. Рабочая папка бота (на сервере)

Если в шаге 2 папка `/opt/doc-registry-bot` уже есть, шаг пропустите. Иначе:

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

## 7. Настройки `.env` (на сервере)

```bash
cd /opt/doc-registry-bot
cp -n .env.example .env
chmod 600 .env
chmod +x *.sh
nano .env
```

`cp -n` не трогает `.env`, если он уже есть: тогда только проверьте строки по таблице.

Пароль для базы сервера сгенерируйте заранее и скопируйте выведенную строку:

```bash
openssl rand -hex 24
```

| Строка | Что вписать | Откуда взять |
|---|---|---|
| `BOT_VERSION` | версию, например `1.1.0` | имя файла образов из шага 6 |
| `MESSENGER_URL` | `http://ИМЯ:ПОРТ` | имя службы и порт из шага 4 |
| `MESSENGER_NETWORK` | `messenger` | не менять |
| `OWNER_IDS` | ваш userId | как в `.env` на ПК; или `./contacts.sh \| grep Фамилия` после шага 4 |
| `ORG_CODES` | коды организации | как в `.env` на ПК |
| `POSTGRES_DB`, `POSTGRES_USER` | оставить `doc_registry` | |
| `POSTGRES_PASSWORD` | новый пароль базы сервера | строка от `openssl rand -hex 24`; пароль с ПК не нужен |
| `BACKUP_TIME`, `BACKUP_KEEP_DAYS` | оставить | |
| `BACKUP_SHARE` | сетевая папка `//сервер/папка` | у администратора сети; нет папки — сотрите значение |
| `BACKUP_SHARE_USER`, `BACKUP_SHARE_PASSWORD`, `BACKUP_SHARE_DOMAIN` | учетная запись с правом записи в папку; домен, если нужен | у администратора сети |

Сохраните: `Ctrl+O`, `Enter`, `Ctrl+X`.

**База PostgreSQL** ставится не отдельно, а создается Docker сама в шаге 9
(`docker compose up -d --wait postgres`): пустая база `POSTGRES_DB`, пользователь `POSTGRES_USER`,
пароль `POSTGRES_PASSWORD` из этого `.env`. Копия с ПК потом загружается в нее, поэтому пароль на
сервере свой. Данные лежат в томе Docker `doc-registry-bot_db-data`, стирает их только
`docker compose down -v`. Строки `POSTGRES_*` заполните до шага 9, в пароле не должно быть `$` и
пробелов, `.env` не теряйте: без пароля бот к базе не подключится.

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
scp admin@10.0.0.5:/opt/doc-registry-bot/backups/журнал-2026-11-12_101500.csv /mnt/e/bot-files/
```

Файл появится в `E:\bot-files`, открывается в Excel двойным щелчком.

**Загрузить журнал с ПК на сервер** (заменяет весь журнал). На ПК:

```bash
scp /mnt/e/bot-files/журнал.csv admin@10.0.0.5:/opt/doc-registry-bot/
```

На сервере:

```bash
cd /opt/doc-registry-bot
./backup.sh
./import.sh журнал.csv
```

**Пользователи списком:** так же, файлом `пользователи.csv` и командой `./users.sh
пользователи.csv`. Заготовку со всеми контактами бота дает `./contacts.sh > контакты.csv`. Подробно о замене журнала и списке пользователей: [admin.md](admin.md),
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
| бот пишет `password authentication failed` | пароль в `.env` поменяли после шага 9: верните прежний |
| «Бот перезапускается» после `update.sh` | `docker compose logs bot`: обычно ошибка в `.env` |
| бот работает, но не отвечает | `docker compose logs bot`; имя и порт в `MESSENGER_URL`; проверка шага 4 |
| служба мессенджера перестала работать после шага 4 | верните файл: `cp compose.yaml.bak compose.yaml`, `docker compose up -d` |
| `scp: No such file or directory` | проверьте имя файла (из вывода `backup.sh`) и папку |
