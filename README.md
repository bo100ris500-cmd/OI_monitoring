# OI Telegram Bot — полная инструкция

Мониторинг открытого интереса (OI) по perpetual-контрактам с сигналами в Telegram.

**Стек:** Python 3.12 · aiogram 3 · SQLAlchemy 2 · SQLite (WAL) · YAML-конфиг

**Биржи:** Binance, Bybit, Bitget, Hyperliquid, Aster (только USDT/USDC perpetual)

---

## 1. Что где выполняется

| Место | Что делать | Что запускать |
|---|---|---|
| **Локально (ПК)** | разработка, тесты, первый push в GitHub, офлайн-парсинг CSV | `python main.py` (тест), `tests/test_smoke.py`, `parse_oi_to_csv.py` |
| **Сервер (VPS)** | **прод-бот 24/7**: polling Telegram, сбор бирж, сигналы, post-factum | `main.py` через **systemd** |
| **GitHub** | хранение кода (без секретов и БД) | ничего не запускает |

### Что делает процесс `main.py` (и локально, и на сервере)

Один процесс поднимает сразу:

1. **Telegram-бот** — команды `/start`, `/check`, `/add`, `/remove`, `/list`, admin  
2. **Collector** — раз в `interval_sec` (по умолчанию 60 с) тянет OI/цену/funding с бирж по union watchlist  
3. **Signal pipeline** — метрики, S1–S7, фильтры F1–F8, рассылка  
4. **Post-factum worker** — цены через 5м / 15м / 1ч / 4ч / 1д после сигнала  
5. **Config watcher** — hot-reload `config.yaml` без рестарта  

### Что НЕ коммитить в GitHub

Уже в `.gitignore`:

- `.env` — токен бота  
- `config.yaml` — рабочий конфиг (в репо только `config.example.yaml`)  
- `data/` — SQLite БД  
- `.venv/`  
- кэши, `*.db`, большие CSV/JSON экспорта  

---

## 2. Подготовка Telegram

1. Откройте [@BotFather](https://t.me/BotFather) → `/newbot` → получите **BOT_TOKEN**.  
2. Узнайте свой Telegram ID (например [@userinfobot](https://t.me/userinfobot)) → это **ADMIN_IDS**.  
3. Позже на сервере эти значения попадут только в `.env` (не в git).

---

## 3. GitHub: создать репозиторий и залить код

На машине разработки (Windows / Linux / macOS), в папке проекта:

### 3.1. Первый раз (репозитория ещё нет)

1. На GitHub: **New repository** (например `oi-analictick`), **без** README/license (если папка уже с кодом).  
2. Локально:

```bash
cd oi_analictick

git init
git add .
git status
# Убедитесь, что НЕТ .env, config.yaml, data/, .venv/

git commit -m "Initial commit: OI Telegram monitoring bot"

git branch -M main
git remote add origin https://github.com/<USER>/<REPO>.git
git push -u origin main
```

С SSH:

```bash
git remote add origin git@github.com:<USER>/<REPO>.git
git push -u origin main
```

### 3.2. Дальнейшие обновления кода

```bash
git add -A
git status
git commit -m "Описание изменений"
git push
```

На сервере после этого: `git pull` (см. §5.4).

### 3.3. Что должно быть в репозитории

```
main.py
requirements.txt
config.example.yaml
.env.example
README.md
app/
deploy/oi-bot.service
tests/test_smoke.py
parse_oi_to_csv.py      # офлайн-утилита
enrich_prices.py        # офлайн-утилита
```

---

## 4. Локальный запуск (разработка / проверка)

### 4.1. Требования

- Python **3.12+**
- доступ в интернет (Telegram API + биржи)

### 4.2. Установка

**Windows (PowerShell):**

```powershell
cd D:\ресерч\soft\oi_analictick

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
# Отредактируйте .env: BOT_TOKEN и ADMIN_IDS
notepad .env

# каталог БД создастся сам при старте
python main.py
```

**Linux / macOS:**

```bash
cd oi_analictick
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.yaml config.yaml
nano .env   # BOT_TOKEN, ADMIN_IDS
python main.py
```

### 4.3. Переменные `.env`

| Переменная | Пример | Описание |
|---|---|---|
| `BOT_TOKEN` | `123456:AA...` | токен BotFather |
| `ADMIN_IDS` | `123456789` | ID админов через запятую |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/oi_bot.db` | путь к БД |
| `CONFIG_PATH` | `./config.yaml` | путь к YAML |
| `LOG_LEVEL` | `INFO` | уровень логов |

### 4.4. Проверка, что бот жив

1. В Telegram найдите бота → `/start`  
2. `/add BTC`  
3. `/list`  
4. Админ: `/admin_stats`, `/admin_status`  

Остановка: `Ctrl+C`.

### 4.5. Локальные тесты без токена

```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python tests\test_smoke.py
```

```bash
PYTHONPATH=. .venv/bin/python tests/test_smoke.py
```

### 4.6. Офлайн-утилиты (только локально / по необходимости)

Не входят в systemd-бот:

```bash
# экспорт канала Telegram → CSV
python parse_oi_to_csv.py -i result.json -o result.csv

# дописать цены 5m/15m/1h/4h/1d в CSV
python enrich_prices.py -i result.csv -o result.csv
```

---

## 5. Сервер (прод, 24/7)

Рекомендуемые ресурсы (из ТЗ): ~4 GB RAM, 2–4 CPU, 40 GB диск. Режим `monitoring_mode: watchlist`.

### 5.1. Подготовка ОС (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git

# пользователь без лишних прав
sudo useradd -r -m -d /opt/oi-bot -s /bin/bash oi || true
sudo mkdir -p /opt/oi-bot
sudo chown -R oi:oi /opt/oi-bot
```

### 5.2. Клон с GitHub и установка

```bash
sudo -u oi -H bash <<'EOF'
cd /opt/oi-bot
git clone https://github.com/<USER>/<REPO>.git .
# если репо уже склонировано в подпапку — поправьте путь

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
cp config.example.yaml config.yaml
mkdir -p data
EOF
```

Заполните секреты **только на сервере**:

```bash
sudo -u oi nano /opt/oi-bot/.env
```

```env
BOT_TOKEN=вставьте_токен
ADMIN_IDS=ваш_telegram_id
DATABASE_URL=sqlite+aiosqlite:///./data/oi_bot.db
CONFIG_PATH=./config.yaml
LOG_LEVEL=INFO
```

При необходимости подправьте пороги:

```bash
sudo -u oi nano /opt/oi-bot/config.yaml
```

> На сервере с 4 GB **не** ставьте `monitoring_mode: global`.

### 5.3. systemd

Файл в репозитории: [`deploy/oi-bot.service`](deploy/oi-bot.service).

```bash
sudo cp /opt/oi-bot/deploy/oi-bot.service /etc/systemd/system/oi-bot.service
sudo systemctl daemon-reload
sudo systemctl enable oi-bot
sudo systemctl start oi-bot
sudo systemctl status oi-bot
```

Логи:

```bash
journalctl -u oi-bot -f
journalctl -u oi-bot -n 200 --no-pager
```

Полезные команды:

```bash
sudo systemctl restart oi-bot   # после смены .env или кода
sudo systemctl stop oi-bot
```

Смена только `config.yaml` обычно **не** требует рестарта (hot-reload). После правки можно также вызвать в боте `/admin_reload`.

### 5.4. Обновление с GitHub на сервере

См. также отдельный блок **«Обновление репозитория и редеплой»** ниже (§10).

```bash
sudo systemctl stop oi-bot
sudo -u oi -H bash <<'EOF'
cd /opt/oi-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
EOF
sudo systemctl start oi-bot
sudo systemctl status oi-bot
```

Бэкап БД перед крупным обновлением:

```bash
sudo -u oi cp /opt/oi-bot/data/oi_bot.db /opt/oi-bot/data/oi_bot.db.bak-$(date +%F)
```

### 5.5. Сеть и безопасность

- Исходящий HTTPS: `api.telegram.org`, API бирж (Binance/Bybit/Bitget/Hyperliquid/Aster).  
- Входящие порты для бота **не нужны** (long polling).  
- Права: `.env` только у пользователя `oi` (`chmod 600 .env`).  
- Не храните `BOT_TOKEN` в Issues/коммитах.

### 5.6. Проверка на проде

1. `/start` в боте  
2. `/add` → ввести `BTC`  
3. `/list` — тикер и биржи  
4. `/check` → ввести тикер — таблица OI  
5. `/admin_stats` / `/admin_status`  
6. В логах нет бесконечных traceback  

---

## 6. Команды бота

| Команда | Кто | Действие |
|---|---|---|
| `/start` | все | регистрация и список команд |
| `/check` | все | «Введите тикер» → биржи и OI |
| `/add` | все | «Введите тикер» → добавить в мониторинг |
| `/remove` | все | кнопки тикеров → удаление из watchlist |
| `/list` | все | тикер + биржи, где доступен |
| `/admin_stats` | админ | пользователи, тикеры, RAM, config_hash |
| `/admin_symbols` | админ | активные base-символы |
| `/admin_reload` | админ | перечитать YAML |
| `/admin_status` | админ | health бирж |

---

## 7. Конфигурация `config.yaml`

Образец: [`config.example.yaml`](config.example.yaml).

Ключевые блоки:

- `collector.interval_sec` — частота сбора  
- `windows` — `1h`, `4h`, `24h`  
- `signals.S1`…`S7` — пороги сигналов  
- `filters.F1`…`F8` — ликвидность, cooldown, market_wide  
- `resources.max_memory_mb` — цель ≤ 700  
- `resources.max_concurrent_requests` — параллелизм API  
- `monitoring_mode` — `watchlist` (прод) / `global` (осторожно)  

---

## 8. Типовые проблемы

| Симптом | Что проверить |
|---|---|
| Бот не отвечает | `BOT_TOKEN`, `systemctl status oi-bot`, `journalctl -u oi-bot` |
| «Unauthorized» | неверный токен / второй экземпляр с тем же токеном |
| Нет данных биржи | `/admin_status`, исходящий HTTPS, rate-limit |
| Нет сигналов | мало истории (&lt; 7 дней), F1 (OI/volume), пустой watchlist |
| Высокий RAM | только watchlist, снизить concurrency, не включать global |
| Конфиг не применился | синтаксис YAML, `/admin_reload`, в логах «invalid config» |

Два процесса с одним `BOT_TOKEN` (локально + сервер) конфликтуют — для прода остановите локальный `main.py`.

---

## 9. Краткая схема жизненного цикла

```text
[ПК] код + git push → [GitHub]
                         ↓ git clone / git pull
                      [VPS] .env + config.yaml + systemd
                         ↓
                   main.py 24/7 → Telegram пользователям
```

```text
Пользователь /add BTC
    → watchlist в SQLite
    → collector раз в минуту по union тикеров
    → metrics → S1–S7 → F1–F8
    → одно сообщение в Telegram
    → post-factum цены 5m…1d
```

---

## 10. Обновление репозитория и редеплой

Подставьте свой путь на сервере: в инструкции ниже `/opt/oi-bot`  
(если ставили в `/root/OI_monitoring` — замените путь везде).

### A. На своём ПК (залить код в GitHub)

```bash
cd oi_analictick          # локальная папка проекта

git status
git add -A
git status                # проверьте: нет .env, data/, .venv/, config.yaml

git commit -m "Описание изменений"
git push origin main
```

Если репозиторий ещё не подключён:

```bash
git init
git add -A
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<USER>/<REPO>.git
git push -u origin main
```

### B. На сервере (подтянуть и перезапустить)

**1. SSH**

```bash
ssh user@ваш_сервер
```

**2. (Рекомендуется) бэкап БД**

```bash
cp /opt/oi-bot/data/oi_bot.db /opt/oi-bot/data/oi_bot.db.bak-$(date +%F-%H%M)
```

**3. Остановить бота → pull → зависимости → старт**

Если сервис systemd `oi-bot`:

```bash
sudo systemctl stop oi-bot

cd /opt/oi-bot
sudo -u oi git pull origin main
# если репо от root:
# cd /root/OI_monitoring && git pull origin main

source .venv/bin/activate    # или: sudo -u oi -H bash -c 'cd /opt/oi-bot && source .venv/bin/activate && pip install -r requirements.txt'
pip install -r requirements.txt

sudo systemctl start oi-bot
sudo systemctl status oi-bot
```

Если бот запущен **не** через systemd (вручную / screen / другой unit) — остановите тот процесс и снова:

```bash
cd /путь/к/проекту
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
python main.py
# или ваш systemctl restart <имя>
```

**4. Проверка**

```bash
journalctl -u oi-bot -f
```

В Telegram:

- `/start`
- `/check` → тикер
- `/list`
- нет ошибок `offset-naive` / traceback каждую минуту

### C. Что не затирается при `git pull`

| Файл / папка | На сервере |
|---|---|
| `.env` | свой, в git не входит |
| `config.yaml` | свой (в git только `config.example.yaml`) |
| `data/oi_bot.db` | своя БД, не в git |
| `.venv/` | не в git |

После обновления **не нужно** заново создавать `.env`, если путь к репо тот же.

### D. Только смена порогов в `config.yaml`

Рестарт не обязателен:

```bash
nano /opt/oi-bot/config.yaml
# в боте: /admin_reload
```

Смена `.env` (токен и т.п.) — нужен `systemctl restart oi-bot`.
