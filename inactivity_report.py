import os
import csv
import json
from datetime import datetime, date, timedelta, timezone

import requests

CMC_API_KEY = os.environ.get("CMC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRADES_FILE = "trades.csv"
INACTIVITY_META_FILE = "inactivity_meta.json"


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def get_last_trade_date():
    if not os.path.exists(TRADES_FILE):
        return None
    last_ts = None
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp_utc"])
            last_ts = ts
    return last_ts.date() if last_ts else None


def get_fng_range_last_days(days=7):
    if not CMC_API_KEY:
        return None

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    url = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "start": start.isoformat(),
        "end": (today + timedelta(days=1)).isoformat(),
        "interval": "daily",
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    if not data:
        return None

    values = [int(item["value"]) for item in data]
    return min(values), max(values)


def main():
    if not (CMC_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Не заданы переменные окружения для inactivity-отчёта")
        return

    inactivity_meta = load_json(INACTIVITY_META_FILE, {})
    last_inact_ts = inactivity_meta.get("last_inactivity_report_ts")
    last_inact_date = (
        datetime.fromisoformat(last_inact_ts).date() if last_inact_ts else None
    )

    today = datetime.now(timezone.utc).date()
    last_trade = get_last_trade_date()

    if last_trade is None:
        print("Нет сделок, отчёт о тишине не нужен.")
        return

    if (today - last_trade).days < 7:
        print("Сделки были менее 7 дней назад, тишина не критична.")
        return

    if last_inact_date and (today - last_inact_date).days < 7:
        print("Уже был отчёт о тишине менее 7 дней назад.")
        return

    fng_range = get_fng_range_last_days(7)
    if fng_range:
        f_min, f_max = fng_range
        fng_block = (
            f"Индекс страха и жадности за последние 7 дней колеблется "
            f"между <b>{f_min}</b> и <b>{f_max}</b>.\n"
        )
    else:
        fng_block = (
            "За последние 7 дней не удалось получить полные данные по индексу.\n"
        )

    text = (
        "😴 <b>Стратегия молчит уже неделю — и это нормально</b>\n\n"
        f"{fng_block}"
        "Рынок не даёт ни глубокого страха, ни ярко выраженной жадности, поэтому модель "
        "не открывает новых виртуальных сделок по BTC и ETH.\n\n"
        "План остаётся прежним:\n"
        "• будем агрессивно докупаться при F&G ≤ 25;\n"
        "• начнём разгружаться при F&G ≥ 60;\n"
        "• при F&G ≥ 70 усилим фиксацию и при F&G ≥ 75 полностью выходим из виртуальной позиции.\n\n"
        "Если появятся сигналы — они сразу появятся в канале. Пока рынок думает — мы не торопим события."
    )

    send_telegram(text)
    inactivity_meta["last_inactivity_report_ts"] = datetime.now(
        timezone.utc
    ).isoformat()
    save_json(INACTIVITY_META_FILE, inactivity_meta)
    print("Отчёт о тишине отправлен.")


if __name__ == "__main__":
    main()
