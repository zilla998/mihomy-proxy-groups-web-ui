# Web UI для mihomo-proxy-ros — менеджер групп

Независимый веб-интерфейс для управления группами проксирования
[mihomo-proxy-ros](https://github.com/Medium1992/mihomo-proxy-ros)
(автор — [@Medium1992](https://github.com/Medium1992)) через MikroTik
REST API. Распространяется как отдельный репозиторий, в основной проект
не входит и работает только в связке с уже установленным контейнером
`mihomo-proxy-ros` на RouterOS.

Автоматизирует то, что раньше делалось руками:

1. Обновление ENV `GROUP` контейнера mihomo-proxy-ros на роутере.
2. Добавление ENV `<NAME>_GEOSITE` (или `_DOMAIN` / `_SUFFIX` / `_KEYWORD` / `_GEOIP`).
3. Для `rule_kind=GEOSITE` — загрузка `<rule_value>.list` из ветки `meta`
   репозитория
   [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat/tree/meta/geo/geosite),
   генерация `.rsc` со статикой `/ip dns static` (`type=FWD`,
   `match-subdomain=yes`) и его выполнение на роутере.
   Для `rule_kind=DOMAIN` и `rule_kind=SUFFIX` такая же DNS-FWD статика
   генерируется из inline `rule_value`.
4. Перезапуск контейнера `mihomo-proxy-ros` и сброс DNS кэша роутера.

UI показывает прогресс каждого шага в реальном времени (Server-Sent Events):
иконки `⏳` / `✅` / `❌`, текст ошибки если шаг провалился.

---

## Что это и зачем

`mihomo-proxy-ros` использует ENV-переменную `GROUP` для перечисления групп
проксирования (`youtube`, `telegram`, …), а ENV вида `<NAME>_GEOSITE`
определяет, какие домены/IP попадают в каждую группу. Чтобы добавить новую
группу руками, нужно:

- зайти в WinBox / WebFig,
- найти контейнер mihomo-proxy-ros, отредактировать его envlist,
- собрать `.rsc` с правилами DNS-форвардинга по списку доменов,
- импортировать его в RouterOS,
- остановить и снова запустить контейнер,
- сбросить DNS кэш.

Этот web UI делает всё то же самое за один клик и показывает, на каком шаге
что-то пошло не так.

---

## Требования

- Любая Linux-машина в LAN с установленным `podman` ≥ 4.4 (для Quadlet)
  или Docker с Compose plugin и сетевым доступом к роутеру. Тестировалось
  на Ubuntu Server 24.04 LTS, но подойдёт любой современный дистрибутив с
  актуальным контейнерным runtime.
- Установленный и сконфигурированный контейнер `mihomo-proxy-ros` на
  RouterOS 7.x — установка, RouterOS-скрипты и настройка самого
  контейнера описаны в основном проекте
  [Medium1992/mihomo-proxy-ros](https://github.com/Medium1992/mihomo-proxy-ros).
- На RouterOS включён REST API (см. ниже).
- У контейнера `mihomo-proxy-ros` выставлен `comment=MihomoProxyRoS`
  (такой comment ставит инсталляционный скрипт основного проекта) — по
  нему web UI находит контейнер на роутере.

---

## Включение REST API на MikroTik

REST API появился в RouterOS 7.1. Включается одной командой:

```routeros
# HTTP (порт 80, в доверенной LAN)
/ip/service/enable www
/ip/service/set www port=80

# либо HTTPS (порт 443, требует сертификата)
/ip/service/enable www-ssl
/ip/service/set www-ssl certificate=<имя сертификата>
```

Проверка с Linux-хоста:

```sh
curl -u admin:changeme http://192.168.88.1/rest/system/identity
# или
curl -k -u admin:changeme https://192.168.88.1/rest/system/identity
```

Должен вернуться JSON вида `{"name":"MikroTik"}`. Если приходит 401 — проверьте
логин/пароль; если 404 — REST API не включён.

Рекомендуется завести отдельного пользователя для web UI:

```routeros
/user/add name=mihomo-webui group=full password=<сильный пароль>
```

Минимально достаточные права — `read,write,policy` (нужны
`/container`, `/system/script`, `/ip/dns/cache`), но `full` проще.

---

## Установка podman на Ubuntu 24.04

`podman` есть в стандартном репозитории Ubuntu 24.04:

```sh
sudo apt update
sudo apt install -y podman
podman --version    # ожидается 4.9.x или новее
```

Quadlet-юниты (формат `.container`) поддерживаются начиная с podman 4.4 — в
24.04 LTS он по умолчанию подходит.

---

## Сборка образа

Готового образа в публичных реестрах нет — собирайте локально на хосте,
где будет работать UI:

```sh
git clone <url-этого-репозитория>.git mihomo-webui
cd mihomo-webui
# подставьте свою архитектуру: linux/amd64, linux/arm64, linux/arm/v7
podman build --platform linux/arm64 -t localhost/mihomo-webui:latest .
```

Сборка занимает несколько минут (httpx и pydantic-core могут компилироваться
из исходников, если для musllinux вашей архитектуры нет pre-built wheels).

> Если podman ругается на `short-name "caddy:2-alpine" did not resolve`,
> добавьте Docker Hub в список реестров поиска:
>
> ```sh
> echo 'unqualified-search-registries = ["docker.io"]' \
>     | sudo tee -a /etc/containers/registries.conf
> ```

---

## Запуск через systemd Quadlet (рекомендуемый путь)

`deploy/mihomo-webui.container` — это unit-файл нового формата
[Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).
systemd сам генерирует обычный `.service` из него при `daemon-reload`.

> ⚠️ Quadlet-юниты — это сгенерированные на лету `.service` файлы, поэтому
> `systemctl enable` на них **не работает** (получите ошибку
> `Unit … is transient or generated`). Команда `start` запускает контейнер
> вручную, а автостарт при загрузке системы обеспечивает секция `[Install]`
> внутри `.container` файла, которую читает генератор. Для rootless ещё
> нужен `loginctl enable-linger`, иначе user-инстанс systemd завершится
> при logout.

### System-wide (с правами root)

```sh
sudo cp deploy/mihomo-webui.container /etc/containers/systemd/
sudo cp deploy/mihomo-webui.env.example /etc/mihomo-webui.env
sudo nano /etc/mihomo-webui.env          # заполнить креды MikroTik
sudo systemctl daemon-reload
sudo systemctl start mihomo-webui.service
```

### Rootless (от обычного пользователя)

```sh
mkdir -p ~/.config/containers/systemd
cp deploy/mihomo-webui.container ~/.config/containers/systemd/
cp deploy/mihomo-webui.env.example ~/.config/mihomo-webui.env
nano ~/.config/mihomo-webui.env
sed -i 's|/etc/mihomo-webui.env|'"$HOME"'/.config/mihomo-webui.env|' \
    ~/.config/containers/systemd/mihomo-webui.container
systemctl --user daemon-reload
systemctl --user start mihomo-webui.service
loginctl enable-linger "$USER"           # чтобы сервис стартовал без логина
```

### Проверка

```sh
systemctl status mihomo-webui.service        # либо --user
podman logs mihomo-webui                     # имя задано ContainerName= в unit
curl -fsS http://localhost:8080/api/health
```

---

## Альтернатива: ручной запуск

Без Quadlet можно поднять контейнер вручную (например, для отладки):

```sh
podman run -d \
    --name mihomo-webui \
    --restart=unless-stopped \
    --env-file /etc/mihomo-webui.env \
    -p 8080:80 \
    localhost/mihomo-webui:latest

podman logs -f mihomo-webui
```

Остановка / удаление:

```sh
podman stop mihomo-webui
podman rm mihomo-webui
```

---

## Запуск через Docker Compose

Compose использует тот же `Containerfile`, тот же Caddy/FastAPI runtime и те же
переменные окружения, что Podman-вариант. Секреты не храните в репозитории:
создайте локальный env-файл из примера и заполните его своими значениями.

```sh
cp deploy/mihomo-webui.env.example deploy/mihomo-webui.env
nano deploy/mihomo-webui.env              # заполнить креды MikroTik
docker compose up -d --build
```

Проверка:

```sh
docker compose ps
docker compose logs -f mihomo-webui
curl -fsS http://localhost:8080/api/health
```

Остановка:

```sh
docker compose down
```

`compose.yaml` повторяет важные ограничения Quadlet: read-only filesystem,
tmpfs для `/tmp`, `/run`, `/data`, `/config`, `no-new-privileges`, сброшенные
capabilities и отдельное `NET_BIND_SERVICE` для Caddy на порту 80 внутри
контейнера. `stop_grace_period: 1020s` нужен по той же причине, что
`TimeoutStopSec` в Quadlet: backend должен успеть дождаться завершения
запущенного workflow перед остановкой процесса.

---

## Конфигурация ENV

Все переменные читаются из env-файла при запуске. Для Quadlet обычно
используется `/etc/mihomo-webui.env`, для Docker Compose —
`deploy/mihomo-webui.env`. Полный пример — `deploy/mihomo-webui.env.example`.

| Переменная | Обязательна | Дефолт | Описание |
|---|---|---|---|
| `MIKROTIK_HOST` | да | — | URL RouterOS со схемой, например `http://192.168.88.1` или `https://192.168.88.1`. |
| `MIKROTIK_USER` | да | — | Логин для REST API. |
| `MIKROTIK_PASSWORD` | да | — | Пароль для REST API. |
| `MIKROTIK_VERIFY_TLS` | нет | `false` | Проверять ли TLS сертификат (для self-signed — `false`). |
| `MIKROTIK_TIMEOUT` | нет | `10` | Таймаут одного REST-запроса (секунды). |
| `RUN_SCRIPT_TIMEOUT` | нет | `600` | Таймаут (секунды) шага `run_router_script` — отдельный, бо́льший лимит для запуска генерируемого из meta-rules-dat `.list` скрипта DNS-FWD. Крупные категории (например `amazon`) разворачиваются в сотни `/ip dns static add` строк и не успевают в стандартный `MIKROTIK_TIMEOUT=10s`. Применяется только к POST `/rest/system/script/run`; остальные REST-вызовы по-прежнему используют `MIKROTIK_TIMEOUT`. |
| `WAIT_STOPPED_TIMEOUT` | нет | `60` | Таймаут (секунды) шага `wait_stopped` — сколько ждать перехода контейнера в состояние `stopped` после `stop_container`. Останов обычно занимает считанные секунды, поэтому 60 с с запасом. |
| `WAIT_RUNNING_TIMEOUT` | нет | `180` | Таймаут (секунды) шага `wait_running`. Если задан `MIHOMO_API_URL`, шаг опрашивает корневой эндпойнт mihomo (`GET <MIHOMO_API_URL>/`) и ждёт ответа с JSON-телом — RouterOS REST может удерживать `status=<empty>` всё окно ожидания на холодном старте, а HTTP-listener mihomo поднимается раньше, чем RouterOS успевает обновить поле `status`. Если `MIHOMO_API_URL` пуст, шаг строго ждёт `running` через RouterOS REST. Холодный старт mihomo-proxy-ros (распаковка образа, подписки, rule-providers) на медленных роутерах легко превышает 60 с, поэтому дефолт значительно больше, чем для остановки. |
| `CONTAINER_WAIT_TIMEOUT` | нет | — | Legacy fallback для `WAIT_STOPPED_TIMEOUT` и `WAIT_RUNNING_TIMEOUT`. Если задан, его значение применяется к обоим шагам, чтобы существующие env-файлы продолжали работать без изменений. Явные `WAIT_STOPPED_TIMEOUT`/`WAIT_RUNNING_TIMEOUT` имеют приоритет. |
| `MIKROTIK_CONTAINER_COMMENT` | нет | `MihomoProxyRoS` | По какому `comment=` искать контейнер на роутере. |
| `MIKROTIK_ENVS_LIST` | нет | `MihomoProxyRoS` | Имя envlist'а с переменными mihomo-proxy-ros. |
| `MIHOMO_API_URL` | нет | — | URL mihomo external-controller (например `http://192.168.255.2:9090`). Если задан, после `wait_running` UI ждёт, пока mihomo внутри контейнера загрузит провайдеры правил, и только потом сбрасывает DNS-кэш роутера. Пусто — шаг `wait_mihomo_ready` отсутствует, `flush_dns` запускается сразу за `wait_running`. |
| `MIHOMO_API_SECRET` | нет | — | Bearer-секрет для mihomo external-controller (если в его конфиге задан `secret:`). Уходит в заголовок `Authorization: Bearer <secret>`. |
| `MIHOMO_READY_TIMEOUT` | нет | `90` | Таймаут (секунды) шага `wait_mihomo_ready` — у каждого провайдера правил с `vehicleType ≠ Inline` появился ненулевой `updatedAt` (правила скачаны и применены). Проверка «mihomo HTTP отвечает» уже сделана в `wait_running` через `GET /`, поэтому этот шаг сразу поллит `/providers/rules`. |
| `WEBUI_USER` | нет | — | Логин для basic auth (если пусто — авторизация выключена). |
| `WEBUI_PASSWORD_HASH` | нет | — | bcrypt-хеш пароля basic auth (генерация — см. ниже). Вставляется в env-файл как есть, без экранирования. |
| `WEBUI_PORT` | нет | `80` | Порт Caddy внутри контейнера. При смене обновите и `PublishPort=` в `mihomo-webui.container`, иначе хост опубликует старый порт. |
| `BACKEND_HOST` / `BACKEND_PORT` | нет | `127.0.0.1` / `8000` | Где Caddy ищет uvicorn-backend внутри контейнера. Менять только при отладке. |

### Безопасность / basic auth

По умолчанию web UI **не защищён** — кто угодно с доступом к
`http://<host>:8080` сможет переключать группы. Для домашней LAN это часто
приемлемо, но если хост доступен из внешней сети — обязательно включите basic
auth:

```sh
podman run --rm caddy:2-alpine \
    caddy hash-password --plaintext 'мой-пароль'
# вывод: $2a$14$abcdef...
```

Скопируйте полученный хеш в env-файл как есть — и systemd `EnvironmentFile=`,
и `podman --env-file` читают значения построчно верботим, без подстановки
переменных, поэтому символы `$` экранировать не нужно:

```
WEBUI_USER=admin
WEBUI_PASSWORD_HASH=$2a$14$abcdef...
```

После этого перезапустите сервис:

```sh
sudo systemctl restart mihomo-webui.service
```

Caddy будет требовать basic auth на всех путях, включая `/api/*`.

> Замечание: web UI хранит пароль RouterOS в env-файле в открытом виде.
> Если это критично, ограничьте доступ к файлу: `chmod 600 /etc/mihomo-webui.env`
> и `chown root:root` (system-wide) либо проверьте права на
> `~/.config/mihomo-webui.env` (rootless).

---

## Использование

После старта откройте `http://<ip-хоста>:8080` (или `https://`, если перед UI
стоит ваш собственный TLS-прокси).

UI состоит из трёх блоков:

- **Текущие группы** — что уже прописано в `GROUP=` контейнера и какие
  `<NAME>_*` ENV переменные ему соответствуют. Рядом с каждой группой —
  кнопка «Удалить».
- **Добавить группу** — дропдаун категорий `geosite`/`geoip` из
  `MetaCubeX/meta-rules-dat`, опциональное поле «своё имя» (если пусто, имя
  группы берётся из выбранной категории) и выбор типа правила
  (`GEOSITE` / `GEOIP` / `DOMAIN` / `SUFFIX` / `KEYWORD`). Дропдаун
  категорий активен только для `GEOSITE` и `GEOIP` и реактивно перезагружается
  при смене типа; для `DOMAIN`/`SUFFIX`/`KEYWORD` он скрыт и используется
  свободное поле «значение правила». Категории берутся из ветки `meta`
  репозитория `MetaCubeX/meta-rules-dat`, файлы `geo/geosite/*.mrs` и
  `geo/geoip/*.mrs` — это тот же источник, который использует
  mihomo-proxy-ros внутри контейнера, поэтому имена в дропдауне гарантированно
  есть на стороне контейнера. Если введено имя, которого нет в выбранной
  категории, UI показывает не блокирующее предупреждение «категория не
  найдена в meta-rules-dat — правило не сработает».
- **Прогресс** — модалка, которая открывается на «Добавить» / «Удалить» и
  показывает по шагам, что происходит на роутере. По завершении —
  сообщение об успехе либо красная плашка с описанием упавшего шага.

### Что значит каждый шаг прогресса

При добавлении группы шаги (в порядке выполнения):

1. **`update_group_env`** — обновляем `GROUP` env mihomo-proxy-ros (читаем
   текущий, добавляем имя через запятую если ещё нет).
2. **`add_rule_env`** — добавляем `<NAME>_GEOSITE` (либо `_DOMAIN` / `_SUFFIX`
   / `_KEYWORD` / `_GEOIP`) с выбранным значением.
3. **`fetch_geosite_list`** — для `rule_kind=GEOSITE` скачиваем
   `geo/geosite/<rule_value>.list` из ветки `meta` репозитория
   `MetaCubeX/meta-rules-dat` и собираем `.rsc` со статикой
   `/ip dns static` (`type=FWD`, `match-subdomain=yes`) — по строке
   `:if ([:len [find name="<домен>"]] = 0) do={ add … }` на каждый домен из
   `.list` (поддерживается `+.X` и plain `X`; `regexp:`/`keyword:`/`include:`
   и пустые/`#`-строки пропускаются). Для `rule_kind=DOMAIN` и
   `rule_kind=SUFFIX` `.rsc` собирается из inline `rule_value`
   (comma-separated значения поддерживаются; `!`-исключения пропускаются).
   Для `KEYWORD`/`GEOIP` шаг помечается `ok` с сообщением
   `skipped (rule_kind=<kind>)`. Если `.list` для запрошенной категории
   отсутствует (404), шаг тоже `ok` с сообщением
   `no geosite list for '<rule_value>' in meta-rules-dat, skipped` —
   workflow продолжается. Любые другие ошибки GitHub (5xx, rate-limit,
   network) завершают workflow с ошибкой.
4. **`run_router_script`** — импортируем сгенерированный `.rsc` в RouterOS
   через `/system/script/add` → `run` → `remove`. Если на шаге 3 `.rsc` не
   был сгенерирован (skipped или 404), шаг помечается `ok` с сообщением
   `skipped (no .rsc generated)`.
5. **`stop_container`** — останавливаем контейнер mihomo-proxy-ros.
6. **`wait_stopped`** — ждём пока контейнер перешёл в состояние `stopped`
   (таймаут `WAIT_STOPPED_TIMEOUT`, по умолчанию 60 секунд; в качестве
   legacy-fallback используется `CONTAINER_WAIT_TIMEOUT`, если он задан).
   RouterOS возвращает для полностью остановленного контейнера
   `status=None` — это тоже трактуется как `stopped`. При таймауте в
   сообщении об ошибке выводится последовательность наблюдённых
   статусов с временными метками (`observed: running@0.0s, stopping@2.1s, …`),
   чтобы было видно, на каком переходе контейнер «застрял».
7. **`start_container`** — стартуем контейнер обратно.
8. **`wait_running`** — ждём пока контейнер действительно поднялся (таймаут
   `WAIT_RUNNING_TIMEOUT`, по умолчанию 180 секунд; legacy
   `CONTAINER_WAIT_TIMEOUT` используется как fallback). Если задан
   `MIHOMO_API_URL`, шаг опрашивает корневой эндпойнт mihomo напрямую
   (`GET <MIHOMO_API_URL>/`) и считается успешным, когда тело ответа после
   обрезки начальных пробелов начинается с `{` — это welcome-JSON mihomo
   (например, `{"hello":"clash.meta"}`). Если в конфиге mihomo задан
   `secret:`, маршрут `/` тоже под bearer-аутентификацией; ответы 401/403
   тоже считаются «контейнер поднялся» (HTTP-listener отвечает, просто
   bearer не подходит) — благодаря этому шаг не висит до полного таймаута
   при некорректном `MIHOMO_API_SECRET`, а уже следующий вызов API
   (`/providers/rules` в `wait_mihomo_ready`) явно поднимет ошибку 401.
   Это надёжнее, чем ждать поле `status` в RouterOS REST, которое на
   холодном старте может оставаться пустым (`<empty>`) всё окно
   ожидания, пока контейнер уже работает. Если `MIHOMO_API_URL` пуст, шаг
   строго ждёт `running` через RouterOS REST до полного таймаута.
9. **`wait_mihomo_ready`** *(только если задан `MIHOMO_API_URL`)* — поллим
   `GET /providers/rules`, пока у всех провайдеров с `vehicleType ≠ Inline`
   не появится ненулевой `updatedAt` (правила скачаны и применены). Таймаут —
   `MIHOMO_READY_TIMEOUT` (по умолчанию 90 секунд). Проверка «mihomo HTTP
   отвечает» уже сделана в предыдущем шаге `wait_running` через `GET /`,
   поэтому здесь нет отдельного опроса `/version`. Если переменная не задана,
   шаг отсутствует — это допустимо, но `flush_dns` ниже отработает до того,
   как mihomo фактически готов, и резолвер может пару секунд возвращать
   fake-ip, для которого правила ещё не применены.
10. **`flush_dns`** — сбрасываем DNS-кэш RouterOS.

Если любой шаг после `stop_container` упал, web UI делает best-effort
попытку выполнить `start_container`, чтобы не оставить контейнер в состоянии
`stopped` (отдельная строка прогресса с пометкой `recovery`).

При удалении группы шаги аналогичные, без шагов с GitHub: `update_group_env`
→ `remove_rule_envs` → `stop_container` → `wait_stopped` → `start_container`
→ `wait_running` → (`wait_mihomo_ready`, если настроен) → `flush_dns`.

---

## HTTP API

Все эндпоинты возвращают JSON, кроме `/api/groups/add` и `/api/groups/remove`,
которые отдают `text/event-stream` (SSE) с событиями `init` / `step` / `done`.

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/health` | Проверка доступности RouterOS REST API. Если задан `MIHOMO_API_URL`, в ответ добавляется блок `"mihomo": {"ok": true, "version": {...}}` либо `"mihomo": {"ok": false, "error": "..."}` — это soft-сигнал, неудача mihomo-пробы НЕ понижает HTTP-код (200). |
| `GET` | `/api/groups/current` | Текущий `GROUP=` контейнера + связанные `<NAME>_*` ENV. |
| `GET` | `/api/rules/categories?kind=GEOSITE\|GEOIP[&force_refresh=true]` | Имена категорий (basenames `*.mrs` без расширения) из ветки `meta` репозитория `MetaCubeX/meta-rules-dat` — папки `geo/geosite/` и `geo/geoip/`. Это тот же источник, который использует mihomo-proxy-ros при загрузке geosite/geoip, и тот же источник, из которого берётся `geo/geosite/<rule_value>.list` для генерации DNS-FWD `.rsc`. Кэшируется в памяти процесса с TTL 24 часа (категорий несколько сотен, обновляются редко, а GitHub лимит 60 req/h без авторизации). `force_refresh=true` сбрасывает кэш. На неподдерживаемом `kind` — 400. |
| `POST` | `/api/groups/add` | SSE-стрим workflow добавления группы. Тело: `{"name": "...", "rule_kind": "GEOSITE", "rule_value": "..."}`. |
| `POST` | `/api/groups/remove` | SSE-стрим workflow удаления группы. Тело: `{"name": "..."}`. |

> Листинг `/api/rules/categories` идёт через [Git Trees API](https://docs.github.com/en/rest/git/trees) (`GET /repos/{repo}/git/trees/{branch}:{path}`), а не через Contents API. Contents API молча обрезает выдачу на 1000 элементов, из-за чего `geo/geosite/` (>1700 файлов) выглядел «только до буквы A»; у Trees API такого лимита нет.

---

## Troubleshooting

### Сервис не стартует

```sh
systemctl status mihomo-webui.service
journalctl -u mihomo-webui.service -n 50
podman logs mihomo-webui
```

Частые причины:
- Образ не собран: `Image=localhost/mihomo-webui:latest` отсутствует в
  `podman images` (надо запустить `podman build` из раздела «Сборка образа»).
- Quadlet-файл лежит не в той папке (system-wide → `/etc/containers/systemd/`,
  rootless → `~/.config/containers/systemd/`).
- Не выполнен `systemctl daemon-reload` после копирования файла.
- Использовали `systemctl enable` вместо `start` (см. предупреждение выше —
  Quadlet-юниты включать через `enable` нельзя).

### `/api/health` возвращает 5xx

Backend стартовал, но не может достучаться до RouterOS REST API:
- 401 в логах — неверный `MIKROTIK_USER` / `MIKROTIK_PASSWORD`.
- 404 — `MIKROTIK_HOST` указывает на адрес без REST API (`/ip/service` `www`
  не включён).
- timeout — нет сетевого маршрута хост → роутер, либо firewall.

Проверка с хоста:

```sh
curl -fsS -u $MIKROTIK_USER:$MIKROTIK_PASSWORD \
    "$MIKROTIK_HOST/rest/system/identity"
```

### Шаг «Импорт .rsc в RouterOS» падает

`.rsc` скрипт упал при выполнении на роутере. RouterOS возвращает текст
ошибки в теле ответа `/system/script/run` — он пробрасывается в UI как
`message` упавшего шага. Чаще всего это:
- Конфликт имён (DNS-static с тем же доменом уже существует — проверьте `/ip/dns/static`).
- Недостаточно прав у пользователя REST API (нужна группа `full` или
  явное `read,write,policy,sensitive`).

### Контейнер не запускается обратно после стопа

Проверьте `podman logs` mihomo-proxy-ros на роутере (через WinBox: `Containers`
→ ваш контейнер → `Log`). Часто это значит, что одна из новых ENV переменных
имеет некорректное значение и mihomo не может распарсить config.yaml. Откатить:

```routeros
# показать ENV-переменные mihomo-proxy-ros (envlist по умолчанию — MihomoProxyRoS)
/container/envs/print where list=MihomoProxyRoS
# удалить добавленную переменную
/container/envs/remove [find list=MihomoProxyRoS key=NEWGROUP_GEOSITE]
# и поправить GROUP=
/container/envs/set [find list=MihomoProxyRoS key=GROUP] value=youtube,telegram
```

После этого запустите контейнер снова из UI или WinBox.

### Quadlet не подхватывается

```sh
# проверка валидности unit'а
/usr/lib/systemd/system-generators/podman-system-generator --dryrun   # system-wide
/usr/libexec/podman/quadlet --user --dryrun                            # rootless (Ubuntu 24.04)
```

Должен вывести содержимое сгенерированного `.service` без ошибок. Если ругается
на `Image=` — проверьте что указанный образ существует (`podman images`).

---

## Разработка

Бэкенд покрыт pytest-тестами (моки httpx через respx, без живых сетевых
вызовов). Запуск:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m pytest backend/tests/ -q
```

Конфиг pytest лежит в `backend/pyproject.toml` (`asyncio_mode = "auto"`,
`testpaths = ["backend/tests"]`). pytest нужно запускать **из корня
репозитория** — тогда rootdir определится как `backend/` и testpaths
разрешится корректно. Если запустить из `backend/tests/`, тесты могут
собраться без `asyncio_mode`, и async-тесты молча пропустятся.

Тесты обязательны при изменении любого из: `backend/{mikrotik,github,workflow,app}.py`,
ENV-схемы (`config.py` + `deploy/mihomo-webui.env.example`), формата SSE-событий
(контракт между `workflow.py` и `frontend/app.js`).

---

## Связанные документы

- [Medium1992/mihomo-proxy-ros](https://github.com/Medium1992/mihomo-proxy-ros) —
  основной проект, без которого этот web UI не имеет смысла. Установка
  контейнера на RouterOS, RouterOS-скрипты и управление настройками самого
  mihomo описаны там.
