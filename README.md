# Kampuskeikat — практические работы университета

Веб-приложение для физических IT-работ: прокладка кабелей, монтаж компьютерных классов, установка сетевого оборудования и серверов. Преподаватель создаёт задания и назначает студентов; студент также может записаться сам. Статусы: **записан → в работе → ожидает подтверждения → выполнено**. Оценок и GitHub нет. Текстовый отчёт и до 5 фото по 5 МБ необязательны.

## Рекомендуемая схема доступа

Используйте Nginx на порту 80, а FastAPI оставьте доступным только внутри Docker-хоста (`127.0.0.1:8000`). Первоначальный адрес будет `http://IP-СЕРВЕРА`. Внутреннее DNS-имя добавляйте только через администратора университетской DNS-зоны. Не создавайте отдельную DNS-службу и не используйте суффикс `.local`: он зарезервирован для mDNS и может конфликтовать с клиентами.

## Установка на Ubuntu Server 22.04

### Перед началом

Понадобятся:

- учётная запись с `sudo`;
- IP сервера, маска сети, шлюз и внутренний DNS-сервер;
- свободный постоянный IP или DHCP reservation;
- разрешённый локальной политикой входящий TCP-порт 80;
- при использовании домена — согласованная DNS-запись, например `tasks.<существующий-домен-университета>`.

Не меняйте IP удалённо, пока не знаете правильные параметры сети. Ошибка в Netplan оборвёт SSH.

### 1. Узнайте текущую сеть

```bash
ip -br address
ip route
resolvectl status
hostname -I
```

Запишите имя интерфейса (`ens18`, `eno1` и т. п.), IP, шлюз и DNS. Для сервера предпочтительно попросить администратора сети сделать DHCP reservation. Если выдан статический адрес, Ubuntu Server настраивает его через Netplan.

Пример — **не копируйте адреса вслепую**:

```bash
sudo nano /etc/netplan/99-static.yaml
```

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    ens18:
      dhcp4: false
      addresses:
        - 10.20.30.40/24
      routes:
        - to: default
          via: 10.20.30.1
      nameservers:
        addresses:
          - 10.20.0.10
          - 10.20.0.11
```

Проверьте и примените безопаснее через `try`:

```bash
sudo chmod 600 /etc/netplan/99-static.yaml
sudo netplan generate
sudo netplan try
```

Если связь осталась — подтвердите конфигурацию. Затем снова проверьте `ip -br address`, `ip route` и `resolvectl status`.

### 2. Обновите сервер

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y ca-certificates curl gnupg unzip ufw
sudo reboot
```

После перезапуска снова подключитесь по SSH.

### 3. Установите Docker

Добавьте официальный репозиторий Docker:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

Используйте команды через `sudo docker`. Добавление пользователя в группу `docker` фактически даёт привилегии уровня root, поэтому для университетского сервера это лучше согласовать отдельно.

### 4. Скопируйте проект

На своём компьютере распакуйте архив, затем:

```bash
scp -r practical-tasks USER@IP-СЕРВЕРА:/tmp/
```

На сервере:

```bash
sudo mkdir -p /opt/practical-tasks
sudo cp -a /tmp/practical-tasks/. /opt/practical-tasks/
sudo chown -R root:root /opt/practical-tasks
cd /opt/practical-tasks
```

### 5. Создайте настройки

```bash
cd /opt/practical-tasks
sudo cp .env.example .env
sudo nano .env
```

Обязательно замените:

```dotenv
INVITE_CODE=длинный-секретный-код-группы
ADMIN_NAME=Имя преподавателя
ADMIN_EMAIL=реальная-почта-преподавателя
ADMIN_PASSWORD=длинный-уникальный-пароль
COOKIE_SECURE=false
```

Сгенерировать значения можно так:

```bash
openssl rand -base64 24
```

Защитите файл:

```bash
sudo chown root:root .env
sudo chmod 600 .env
```

Администратор создаётся только при первом запуске пустой базы. Последующее изменение `ADMIN_PASSWORD` в `.env` не меняет пароль существующей учётной записи.

### 6. Запустите приложение

```bash
cd /opt/practical-tasks
sudo docker compose config
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 app
sudo docker compose logs --tail=100 nginx
```

Проверьте локально:

```bash
curl -I http://127.0.0.1/
curl http://127.0.0.1/health
```

Откройте с компьютера в той же сети: `http://IP-СЕРВЕРА`.

### 7. Ограничьте доступ файрволом

**Сначала разрешите SSH, иначе можно потерять удалённый доступ.** Замените `10.20.30.0/24` на реальную внутреннюю подсеть:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow from 10.20.30.0/24 to any port 80 proto tcp
sudo ufw enable
sudo ufw status numbered
```

Если университетская сеть состоит из нескольких подсетей/VLAN, добавьте правило для каждой разрешённой сети. Docker публикует порт 80, поэтому ограничение доступа лучше дополнительно обеспечить университетским ACL/VLAN или публиковать Nginx только на конкретном IP сервера. Для второго варианта замените в `compose.yaml` строку `80:80`, например на `10.20.30.40:80:80`, и выполните `sudo docker compose up -d`.

### 8. Добавьте внутренний домен

Лучший вариант — попросить администратора университетской DNS создать запись:

```text
tasks.<существующий-внутренний-домен>  A  10.20.30.40
```

Это не мешает доменной сети: создаётся обычный дочерний хост в уже существующей DNS-зоне. Не поднимайте параллельный DNS-сервер на Ubuntu и не используйте случайную новую зону. Не используйте `.local`, потому что клиенты обрабатывают её через mDNS.

Проверьте с клиентского компьютера:

```bash
nslookup tasks.<домен-университета>
ping tasks.<домен-университета>
```

Сайт станет доступен по `http://tasks.<домен-университета>`. Приложению менять конфигурацию не требуется: Nginx принимает любое внутреннее имя. Для HTTPS нужен сертификат внутреннего центра сертификации университета; самоподписанный сертификат вызовет предупреждения браузера.

Если DNS-запись получить нельзя, используйте IP. Записи в `hosts` годятся только для краткого теста, поскольку их придётся настраивать на каждом компьютере вручную.

## Рабочий процесс

### Преподаватель

1. Откройте раздел заданий.
2. Создайте работу: тип, место, аудитория, время, инструкция и число исполнителей.
3. Оставьте самозапись открытой или назначьте студента вручную.
4. В разделе состояния работ откройте запись студента.
5. Просмотрите текст и фотографии.
6. Нажмите «Отметить выполненным» — работа появится в истории студента.

### Студент

1. Зарегистрируйтесь с групповым кодом.
2. Запишитесь на открытую работу.
3. Нажмите «Начать работу».
4. При необходимости добавьте заметку и фотографии.
5. Нажмите «Отправить на подтверждение».
6. После подтверждения преподавателя работа переместится в историю выполненных.

## Обновление

Перед обновлением сделайте резервную копию:

```bash
cd /opt/practical-tasks
sudo ./scripts/backup.sh
```

Замените исходники новой версией, **не заменяя `.env`**, затем:

```bash
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100
```

## Резервное копирование

Ручная копия базы и фото:

```bash
cd /opt/practical-tasks
sudo ./scripts/backup.sh
ls -lh backups/
```

Для ночной копии откройте `sudo crontab -e` и добавьте:

```cron
15 2 * * * cd /opt/practical-tasks && ./scripts/backup.sh >> /var/log/practical-tasks-backup.log 2>&1
```

Скрипт хранит локальные архивы 30 дней. Важные копии перенесите на другой сервер или носитель: копия на том же физическом сервере не защищает от отказа диска.

### Восстановление вручную

```bash
cd /opt/practical-tasks
sudo docker compose down
sudo mkdir -p /tmp/practical-restore
sudo tar -xzf backups/practical-tasks_ДАТА.tar.gz -C /tmp/practical-restore
sudo docker compose up -d app
```

Найдите имена распакованной базы и каталога фотографий, затем выполните копирование в `/data/app.db` и `/data/uploads` контейнера `app` через `sudo docker compose cp`, перезапустите `app` и выполните `sudo docker compose up -d`.

## Диагностика

```bash
sudo docker compose ps
sudo docker compose logs -f --tail=200
sudo ss -lntp | grep ':80'
curl http://127.0.0.1/health
sudo ufw status numbered
df -h
sudo docker system df
```

- `413 Request Entity Too Large`: проверьте `client_max_body_size 27m` в `nginx/default.conf` и выполните `sudo docker compose restart nginx`.
- `502 Bad Gateway`: смотрите `sudo docker compose logs app`. Частая причина первого запуска — неизменённый `ADMIN_PASSWORD`.
- Сайт работает на сервере, но не с ПК: проверьте UFW, VLAN/ACL, маршрут и публикацию порта 80.
- Домен не работает, IP работает: проблема DNS; `nslookup` должен вернуть IP сервера.
- Фото не загружается: разрешены реальные JPEG/PNG/WebP, максимум 5 МБ каждое и 5 штук на одну работу.
- Заканчивается диск: проверьте `df -h`, резервные копии и Docker-образы. Не запускайте `docker system prune -a` без понимания последствий.

## Ограничения и безопасность

- Версия рассчитана на одну небольшую группу и один экземпляр приложения.
- SQLite подходит для этого объёма; для нескольких групп и высокой параллельной нагрузки лучше PostgreSQL.
- HTTP во внутренней сети не шифрует пароли и фотографии. Если сеть не считается доверенной, запросите внутренний DNS и TLS-сертификат, затем включите `COOKIE_SECURE=true`.
- Фотографии могут содержать людей, серийные номера и схему помещений. Согласуйте правила хранения с университетом и не фотографируйте пароли, ключи или секретные наклейки.
