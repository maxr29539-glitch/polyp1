import re
import time
import queue
import threading
import requests
from datetime import datetime, timezone, timedelta
from requests.exceptions import RequestException, ReadTimeout, ConnectTimeout, SSLError

# =========================
# НАСТРОЙКИ
# =========================

TELEGRAM_BOT_TOKEN = "8734693345:AAHipRAqQ0I57vT07ZYxJ18tlmmey9YJyqc"
TELEGRAM_CHAT_ID = "531608512"

CHAIN_CHECK_EVERY_SECONDS = 3  # Проверка каждый 3 секунды (для блоков)
DATA_API_CHECK_EVERY_SECONDS = 0.35  # Быстрый polling для минимальной задержки
MAX_ALERT_DELAY_SECONDS = None  # None = не отбрасывать новые сделки из-за задержки API

FETCH_LIMIT = 30  # Меньше payload на VPN, быстрее ответ API

ERROR_ALERT_COOLDOWN_SECONDS = 300
TG_QUEUE_MAXSIZE = 1000  # Ограничение на размер очереди сообщений Telegram

# Официальные exchange-контракты Polymarket на Polygon
EXCHANGE_ADDRESSES = [
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
]

POLYGON_HTTP_URLS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
]

# =========================
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# =========================

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
MSK_TZ = timezone(timedelta(hours=3))

telegram_queue = queue.Queue(maxsize=TG_QUEUE_MAXSIZE)
last_update_id = None
last_error_alert_times = {}

watch_state = {
    "wallet": None,
    "min_usd": 10.0,
    "enabled": False,
    "seen_ids": set(),  # для /trades
    "seen_logs": set(),  # для on-chain log id
    "last_seen_ts": 0,  # Unix ts для /trades
    "last_block": None,  # последний обработанный block
    "api_synced": False,  # первая успешная синхронизация API без спама в Telegram
}

state_lock = threading.Lock()
current_rpc_index = 0

# =========================
# УТИЛИТЫ
# =========================

def should_send_error_alert(error_key: str) -> bool:
    now = time.time()
    last_time = last_error_alert_times.get(error_key, 0)
    if now - last_time >= ERROR_ALERT_COOLDOWN_SECONDS:
        last_error_alert_times[error_key] = now
        return True
    return False


def is_valid_wallet(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))


def parse_timestamp_raw(ts_value):
    try:
        ts_int = int(ts_value)
        if ts_int > 10_000_000_000:
            ts_int = ts_int / 1000
        return datetime.fromtimestamp(ts_int, tz=timezone.utc)
    except Exception:
        return None


def parse_timestamp_unix(ts_value):
    dt = parse_timestamp_raw(ts_value)
    if dt is None:
        return 0
    return int(dt.timestamp())


def parse_timestamp_text(ts_value):
    dt = parse_timestamp_raw(ts_value)
    if dt is None:
        return str(ts_value)
    return dt.astimezone(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")


def seconds_delay_from_ts(ts_value):
    dt = parse_timestamp_raw(ts_value)
    if dt is None:
        return "n/a"
    delay = (datetime.now(timezone.utc) - dt).total_seconds()
    if delay < 0:
        delay = 0
    return f"{delay:.1f} сек"


def format_unix_utc(ts: int | None):
    if ts is None:
        return "n/a"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def delay_from_unix(ts: int | None):
    if ts is None:
        return "n/a"
    delay = time.time() - ts
    if delay < 0:
        delay = 0
    return f"{delay:.1f} сек"


def send_telegram_async(text: str):
    try:
        telegram_queue.put_nowait(text)
    except queue.Full:
        print("Очередь Telegram переполнена, сообщение пропущено")


def telegram_worker():
    while True:
        text = telegram_queue.get()
        if text is None:
            break

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        ok = False

        for attempt in range(2):
            try:
                response = session.post(
                    url,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=(4, 8),
                )
                response.raise_for_status()
                ok = True
                break
            except (ReadTimeout, ConnectTimeout):
                print(f"Telegram timeout, попытка {attempt + 1}/2")
                time.sleep(0.5)
            except RequestException as e:
                print(f"Ошибка Telegram: {e}")
                break
            except Exception as e:
                print(f"Неизвестная ошибка Telegram: {e}")
                break

        if not ok:
            print("Не удалось отправить сообщение в Telegram")

        telegram_queue.task_done()


# =========================
# TELEGRAM КОМАНДЫ
# =========================

def get_telegram_updates():
    global last_update_id

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 25}

    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=(4, 30))
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                raise RuntimeError(f"Telegram getUpdates не ok: {data}")

            return data.get("result", [])
        except (ReadTimeout, ConnectTimeout, SSLError, RequestException) as e:
            last_error = e
            print(f"Telegram getUpdates error, попытка {attempt + 1}/3: {e}")
            time.sleep(0.7)

    if last_error:
        raise last_error
    return []


def help_text():
    return (
        "Команды:\n\n"
        "/watch <wallet> <min_usd>\n"
        "Пример:\n"
        "/watch 0x63ce342161250d705dc0b16df89036c8e5f9ba9a 10\n\n"
        "/status — показать настройки\n"
        "/stop — остановить слежение\n"
        "/help — помощь\n\n"
        "Можно и без команды:\n"
        "0x63ce342161250d705dc0b16df89036c8e5f9ba9a 10"
    )


def reset_watch_state(wallet: str, min_usd: float):
    with state_lock:
        watch_state["wallet"] = wallet
        watch_state["min_usd"] = min_usd
        watch_state["enabled"] = True
        watch_state["seen_ids"] = set()
        watch_state["seen_logs"] = set()
        watch_state["last_seen_ts"] = 0
        watch_state["last_block"] = None
        watch_state["api_synced"] = False


def stop_watch_state():
    with state_lock:
        watch_state["wallet"] = None
        watch_state["min_usd"] = 10.0
        watch_state["enabled"] = False
        watch_state["seen_ids"] = set()
        watch_state["seen_logs"] = set()
        watch_state["last_seen_ts"] = 0
        watch_state["last_block"] = None
        watch_state["api_synced"] = False


def handle_command(text: str):
    text = text.strip()

    if text.startswith("/help"):
        send_telegram_async(help_text())
        return

    if text.startswith("/status"):
        with state_lock:
            enabled = watch_state["enabled"]
            wallet = watch_state["wallet"]
            min_usd = watch_state["min_usd"]
            last_seen_ts = watch_state["last_seen_ts"]

        if enabled and wallet:
            send_telegram_async(
                "✅ Слежение активно\n\n"
                f"Кошелёк: {wallet}\n"
                f"Порог: ${min_usd:.2f}\n"
                f"Последний timestamp: {format_unix_utc(last_seen_ts) if last_seen_ts else 'нет'}"
            )
        else:
            send_telegram_async("Сейчас слежение выключено.")
        return

    if text.startswith("/stop"):
        stop_watch_state()
        send_telegram_async("⏹ Слежение остановлено.")
        return

    if text.startswith("/watch"):
        parts = text.split()
        if len(parts) != 3:
            send_telegram_async(
                "Неверный формат.\n\n"
                "Используй:\n"
                "/watch 0x63ce342161250d705dc0b16df89036c8e5f9ba9a 10"
            )
            return

        wallet = parts[1].strip().lower()
        min_usd_raw = parts[2].strip().replace(",", ".")

        if not is_valid_wallet(wallet):
            send_telegram_async("Неверный кошелёк. Нужен адрес 0x + 40 hex-символов.")
            return

        try:
            min_usd = float(min_usd_raw)
        except ValueError:
            send_telegram_async("Порог должен быть числом. Пример: 10")
            return

        if min_usd <= 0:
            send_telegram_async("Порог должен быть больше 0.")
            return

        reset_watch_state(wallet, min_usd)
        warmup_sources()

        send_telegram_async(
            "✅ Слежение запущено\n\n"
            f"Кошелёк: {wallet}\n"
            f"Порог: ${min_usd:.2f}\n"
            f"Data API polling: каждые {DATA_API_CHECK_EVERY_SECONDS} сек"
        )
        return

    parts = text.split()
    if len(parts) == 2 and is_valid_wallet(parts[0]):
        try:
            float(parts[1].replace(",", "."))
            handle_command(f"/watch {parts[0]} {parts[1]}")
            return
        except ValueError:
            pass

    send_telegram_async("Не понял команду.\n\nНапиши /help")


# =========================
# POLYGON JSON-RPC
# =========================

def rpc_post(method: str, params: list, timeout=(3, 6)):
    global current_rpc_index
    last_error = None

    for i in range(len(POLYGON_HTTP_URLS)):
        idx = (current_rpc_index + i) % len(POLYGON_HTTP_URLS)
        url = POLYGON_HTTP_URLS[idx]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        try:
            r = session.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            current_rpc_index = idx
            return data.get("result")
        except Exception as e:
            last_error = e
            continue

    raise last_error


def hex_to_int(value: str) -> int:
    return int(value, 16)


def get_latest_block_number():
    result = rpc_post("eth_blockNumber", [])
    return hex_to_int(result)


def get_block_timestamp(block_number: int):
    result = rpc_post("eth_getBlockByNumber", [hex(block_number), False])
    if not result:
        return None
    ts_hex = result.get("timestamp")
    if not ts_hex:
        return None
    return int(ts_hex, 16)


def get_logs_for_block(block_number: int):
    params = [{
        "fromBlock": hex(block_number),
        "toBlock": hex(block_number),
        "address": EXCHANGE_ADDRESSES,
    }]
    result = rpc_post("eth_getLogs", params, timeout=(3, 8))
    return result or []


def topic_to_address(topic_hex: str) -> str:
    clean = topic_hex[2:].lower()
    return "0x" + clean[-40:]


def decode_uint256_words(data_hex: str):
    clean = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(clean) % 64 != 0:
        raise ValueError("Некорректная длина data")
    return [int(clean[i:i + 64], 16) for i in range(0, len(clean), 64)]


def make_log_id(log: dict) -> str:
    return f"{log.get('transactionHash','')}:{log.get('logIndex','')}"


# =========================
# ON-CHAIN PARSER
# =========================

def parse_candidate_trade_log(log: dict, target_wallet: str, min_usd: float, block_ts: int | None):
    try:
        topics = log.get("topics", [])
        data_hex = log.get("data", "")

        if len(topics) < 4:
            return None

        maker = topic_to_address(topics[2])
        if maker.lower() != target_wallet.lower():
            return None

        values = decode_uint256_words(data_hex)
        if len(values) < 5:
            return None

        maker_asset_id = values[0]
        taker_asset_id = values[1]
        maker_amount_filled = values[2]
        taker_amount_filled = values[3]
        fee = values[4]

        if maker_asset_id == 0:
            side = "BUY"
            usdc_raw = maker_amount_filled
            token_id = taker_asset_id
            shares_raw = taker_amount_filled
        elif taker_asset_id == 0:
            side = "SELL"
            usdc_raw = taker_amount_filled
            token_id = maker_asset_id
            shares_raw = maker_amount_filled
        else:
            return None

        notional_usd = usdc_raw / 1_000_000
        if notional_usd < min_usd:
            return None

        shares = shares_raw / 1_000_000 if shares_raw else 0.0
        price = (notional_usd / shares) if shares > 0 else None
        price_text = f"{price:.4f}" if price is not None else "n/a"
        fee_usd = fee / 1_000_000

        tx_hash = log.get("transactionHash", "0x?")
        block_number = hex_to_int(log["blockNumber"]) if log.get("blockNumber") else None
        exchange_address = log.get("address", "").lower()
        exchange_name = "Neg Risk CTF Exchange" if exchange_address == EXCHANGE_ADDRESSES[1] else "CTF Exchange"

        text = (
            f"🚨 Polymarket chain alert\n\n"
            f"Кошелёк: {maker}\n"
            f"Действие: {side}\n"
            f"Сумма: ${notional_usd:,.2f}\n"
            f"Цена: {price_text}\n"
            f"Shares: {shares:,.2f}\n"
            f"Token ID: {token_id}\n"
            f"Fee: ${fee_usd:,.4f}\n"
            f"Контракт: {exchange_name}\n"
            f"Block: {block_number}\n"
            f"Время блока: {format_unix_utc(block_ts)}\n"
            f"Задержка сигнала: {delay_from_unix(block_ts)}\n"
            f"TX: {tx_hash}"
        )

        return text

    except Exception:
        return None


# =========================
# POLYMARKET DATA API
# =========================

def get_market_by_token_id(token_id: str):
    url = f"https://data-api.polymarket.com/markets/{token_id}"
    response = session.get(url)
    data = response.json()
    return data.get("name", "Неизвестный рынок")


def get_user_trades(wallet: str):
    url = "https://data-api.polymarket.com/trades"
    params = {
        "user": wallet,
        "limit": FETCH_LIMIT,
        "offset": 0,
    }

    last_error = None

    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=(1.5, 3.5))
            r.raise_for_status()
            return r.json()
        except (ReadTimeout, ConnectTimeout) as e:
            last_error = e
            print(f"Polymarket timeout, попытка {attempt + 1}/3")
            time.sleep(0.15 * (attempt + 1))
        except RequestException as e:
            last_error = e
            print(f"Ошибка Polymarket: {e}")
            break

    if last_error:
        raise last_error

    return []


def _is_5m_or_15m_market(title: str) -> bool:
    low = title.lower()
    keyword_markers = [
        "5m",
        "15m",
        "5 min",
        "15 min",
        "5-minute",
        "15-minute",
        "5 minute",
        "15 minute",
        "5 minutes",
        "15 minutes",
    ]
    if any(marker in low for marker in keyword_markers):
        return True

    # Example: "10:55AM-11:00AM ET" -> 5 minutes
    m = re.search(r"(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)\s*ET", title, re.IGNORECASE)
    if not m:
        return False

    h1, m1, ap1, h2, m2, ap2 = m.groups()

    def to_minutes(hh: str, mm: str, ap: str) -> int:
        hour = int(hh) % 12
        if ap.upper() == "PM":
            hour += 12
        return hour * 60 + int(mm)

    start = to_minutes(h1, m1, ap1)
    end = to_minutes(h2, m2, ap2)
    if end < start:
        end += 24 * 60

    duration = end - start
    return duration in (5, 15)


def trade_to_alert(item: dict, wallet: str, min_usd: float):
    side = str(item.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        return None

    size = item.get("size")
    price = item.get("price")
    usdc_size = item.get("usdcSize")

    notional = None
    try:
        if usdc_size is not None:
            notional = float(usdc_size)
        elif size is not None and price is not None:
            notional = float(size) * float(price)
    except Exception:
        return None

    if notional is None or notional < min_usd:
        return None

    tx_hash = item.get("transactionHash", "no_tx_hash")
    title = item.get("title") or item.get("market") or "Без названия"
    if _is_5m_or_15m_market(title):
        return None

    outcome = item.get("outcome") or "?"
    ts_value = item.get("timestamp")
    ts = parse_timestamp_text(ts_value)
    delay_text = seconds_delay_from_ts(ts_value)
    price_text = f"{float(price):.4f}" if price is not None else "n/a"

    unique_id = f"{tx_hash}:{side}:{outcome}:{price_text}"

    text = (
        f"🚨 Polymarket API alert\n\n"
        f"Кошелёк: {wallet}\n"
        f"Действие: {side}\n"
        f"Сумма: ${notional:,.2f}\n"
        f"Исход: {outcome}\n"
        f"Цена: {price_text}\n"
        f"Рынок: {title}\n"
        f"Время сделки: {ts}\n"
        f"Задержка сигнала: {delay_text}\n"
        f"TX: {tx_hash}"
    )

    return unique_id, text, parse_timestamp_unix(ts_value)


# =========================
# ПРОГРЕВ
# =========================

def warmup_sources():
    with state_lock:
        wallet = watch_state["wallet"]
        min_usd = watch_state["min_usd"]

    if not wallet:
        return

    warmup_ok = True
    try:
        items = get_user_trades(wallet)
    except Exception as e:
        print(f"Не удалось прогреть /trades: {e}")
        items = []
        warmup_ok = False

    new_seen = set()
    max_ts = 0

    for item in items:
        parsed = trade_to_alert(item, wallet, min_usd)
        if parsed:
            unique_id, _, trade_ts = parsed
            new_seen.add(unique_id)
            if trade_ts > max_ts:
                max_ts = trade_ts

    with state_lock:
        watch_state["seen_ids"] = new_seen
        watch_state["last_seen_ts"] = max_ts
        watch_state["api_synced"] = warmup_ok

    print(f"Прогрев завершён. last_seen_ts={max_ts}")

def telegram_command_loop():
    global last_update_id

    while True:
        try:
            updates = get_telegram_updates()
            for upd in updates:
                last_update_id = upd.get("update_id", last_update_id)
                msg = upd.get("message") or upd.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                if text:
                    handle_command(text)
        except Exception as e:
            print(f"telegram_command_loop error: {e}")
            time.sleep(2)


def chain_loop():
    while True:
        try:
            with state_lock:
                enabled = watch_state["enabled"]
                wallet = watch_state["wallet"]
                min_usd = watch_state["min_usd"]
                last_block = watch_state["last_block"]
                seen_logs = set(watch_state["seen_logs"])

            if not enabled or not wallet:
                time.sleep(1)
                continue

            latest_block = get_latest_block_number()
            start_block = latest_block if last_block is None else last_block + 1

            if start_block > latest_block:
                time.sleep(CHAIN_CHECK_EVERY_SECONDS)
                continue

            end_block = min(latest_block, start_block + 19)

            for block_number in range(start_block, end_block + 1):
                block_ts = get_block_timestamp(block_number)
                logs = get_logs_for_block(block_number)

                for log in logs:
                    log_id = make_log_id(log)
                    if log_id in seen_logs:
                        continue

                    seen_logs.add(log_id)
                    text = parse_candidate_trade_log(log, wallet, min_usd, block_ts)
                    if text:
                        send_telegram_async(text)

                with state_lock:
                    watch_state["last_block"] = block_number
                    watch_state["seen_logs"] = seen_logs

            time.sleep(CHAIN_CHECK_EVERY_SECONDS)
        except Exception as e:
            print(f"chain_loop error: {e}")
            if should_send_error_alert("chain_loop"):
                send_telegram_async(f"Chain loop error: {e}")
            time.sleep(2)


def data_api_loop():
    fail_streak = 0

    while True:
        try:
            with state_lock:
                enabled = watch_state["enabled"]
                wallet = watch_state["wallet"]
                min_usd = watch_state["min_usd"]
                seen_ids = set(watch_state["seen_ids"])
                last_seen_ts = watch_state["last_seen_ts"]
                api_synced = watch_state["api_synced"]

            if not enabled or not wallet:
                time.sleep(1)
                continue

            items = get_user_trades(wallet)
            fail_streak = 0
            now_ts = int(time.time())
            max_seen_ts = last_seen_ts
            new_seen = set()
            alerts_to_send = []

            for item in items:
                parsed = trade_to_alert(item, wallet, min_usd)
                if not parsed:
                    continue

                unique_id, text, ts_unix = parsed
                new_seen.add(unique_id)
                if ts_unix > max_seen_ts:
                    max_seen_ts = ts_unix

                if unique_id in seen_ids:
                    continue

                if ts_unix <= 0:
                    continue

                # Optional stale-trade filter (disabled by default).
                if (
                    MAX_ALERT_DELAY_SECONDS is not None
                    and now_ts - ts_unix > MAX_ALERT_DELAY_SECONDS
                ):
                    continue

                if api_synced:
                    alerts_to_send.append((ts_unix, text))

            alerts_to_send.sort(key=lambda x: x[0])
            for _, text in alerts_to_send:
                send_telegram_async(text)

            with state_lock:
                merged = seen_ids | new_seen
                watch_state["seen_ids"] = merged
                watch_state["last_seen_ts"] = max_seen_ts
                watch_state["api_synced"] = True

            time.sleep(DATA_API_CHECK_EVERY_SECONDS)
        except Exception as e:
            fail_streak += 1
            print(f"data_api_loop error: {e}")
            if should_send_error_alert("data_api_loop"):
                send_telegram_async(f"Data API loop error: {e}")
            backoff = min(4.0, 0.6 * fail_streak)
            time.sleep(backoff)


# =========================
# MAIN
# =========================

def main():
    print("Бот запускается...")

    tg_sender_thread = threading.Thread(target=telegram_worker, daemon=True)
    tg_sender_thread.start()

    tg_commands_thread = threading.Thread(target=telegram_command_loop, daemon=True)
    tg_commands_thread.start()

    api_thread = threading.Thread(target=data_api_loop, daemon=True)
    api_thread.start()

    send_telegram_async(
        "✅ Быстрый бот запущен\n\n"
        "Команда:\n"
        "/watch 0x63ce342161250d705dc0b16df89036c8e5f9ba9a 10"
    )

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()
