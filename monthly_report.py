import os
import csv
import json
from datetime import datetime, date, timedelta, timezone

import requests

CMC_API_KEY = os.environ.get("CMC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRADES_FILE = "trades.csv"
MONTHLY_META_FILE = "monthly_meta.json"
BASE_CAPITAL = 10_000.0


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def fmt_usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def get_month_bounds():
    """
    Берём ПРОШЛЫЙ календарный месяц.
    Если сегодня 5 апреля — отчёт будет за март.
    """
    today = datetime.now(timezone.utc).date()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    year = last_prev.year
    month = last_prev.month
    start = date(year, month, 1)
    end = last_prev
    return year, month, start, end


def load_trades_for_month(start: date, end: date):
    if not os.path.exists(TRADES_FILE):
        return []

    trades = []
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp_utc"])
            d = ts.date()
            if start <= d <= end:
                trades.append(
                    {
                        "date": d,
                        "asset": row["asset"],
                        "action": row["action"],
                        "fng": int(row["fng"]),
                        "price": float(row["price"]),
                        "usd_amount": float(row["usd_amount"]),
                        "asset_delta": float(row["asset_delta"]),
                        "avg_entry_price": float(row["avg_entry_price"])
                        if row["avg_entry_price"]
                        else None,
                    }
                )
    return trades


def get_monthly_fng_stats(start: date, end: date):
    if not CMC_API_KEY:
        return None

    url = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "start": start.isoformat(),
        "end": (end + timedelta(days=1)).isoformat(),
        "interval": "daily",
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    if not data:
        return None

    data_sorted = sorted(data, key=lambda x: x["timestamp"])
    values = [int(item["value"]) for item in data_sorted]
    first = values[0]
    last = values[-1]
    vmin = min(values)
    vmax = max(values)
    avg = sum(values) / len(values)
    return {"first": first, "last": last, "min": vmin, "max": vmax, "avg": avg}


def month_name_ru_nom(m: int) -> str:
    names = {
        1: "январь",
        2: "февраль",
        3: "март",
        4: "апрель",
        5: "май",
        6: "июнь",
        7: "июль",
        8: "август",
        9: "сентябрь",
        10: "октябрь",
        11: "ноябрь",
        12: "декабрь",
    }
    return names.get(m, str(m))


def month_name_ru_gen(m: int) -> str:
    names = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    return names.get(m, str(m))


def load_monthly_meta():
    if not os.path.exists(MONTHLY_META_FILE):
        return {}
    with open(MONTHLY_META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_monthly_meta(meta):
    with open(MONTHLY_META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def main():
    if not (CMC_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Не заданы переменные окружения для месячного отчёта")
        return

    year, month, start, end = get_month_bounds()
    trades = load_trades_for_month(start, end)
    fng_stats = get_monthly_fng_stats(start, end)

    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]

    total_buy_usd = sum(t["usd_amount"] for t in buys)
    total_sell_usd = sum(t["usd_amount"] for t in sells)

    pnl_usd = 0.0
    for t in sells:
        if t["avg_entry_price"] is None:
            continue
        cost = abs(t["asset_delta"]) * t["avg_entry_price"]
        profit = t["usd_amount"] - cost
        pnl_usd += profit

    pnl_pct = pnl_usd / BASE_CAPITAL * 100 if BASE_CAPITAL > 0 else 0.0

    month_nom = month_name_ru_nom(month).capitalize()
    month_gen = month_name_ru_gen(month)

    header = f"📆 <b>Итоги за {month_nom} {year} года</b>\n"

    if fng_stats:
        f_first = fng_stats["first"]
        f_last = fng_stats["last"]
        f_min = fng_stats["min"]
        f_max = fng_stats["max"]
        f_avg = fng_stats["avg"]

        if f_last < f_first:
            trend = "снизился"
        elif f_last > f_first:
            trend = "вырос"
        else:
            trend = "практически не изменился"

        fng_block = (
            "\n📊 <b>Индекс страха и жадности</b>\n"
            f"С начала месяца индекс {trend} с <b>{f_first}</b> до <b>{f_last}</b>.\n"
            f"Минимум за месяц: <b>{f_min}</b>, максимум: <b>{f_max}</b>, "
            f"среднее значение: <b>{f_avg:.1f}</b>.\n"
        )
    else:
        fng_block = "\n📊 Не удалось получить статистику индекса за месяц.\n"

    actions_block = (
        "\n💼 <b>Действия стратегии</b>\n"
        f"Всего сделок: <b>{len(trades)}</b>\n"
        f"Покупок (BTC и ETH): <b>{len(buys)}</b>, на сумму ~<b>{fmt_usd(total_buy_usd)} $</b>\n"
        f"Продаж (BTC и ETH): <b>{len(sells)}</b>, на сумму ~<b>{fmt_usd(total_sell_usd)} $</b>\n"
    )

    result_block = (
        f"\n💰 <b>Результат {month_gen}:</b>\n"
        f"• PnL: <b>{fmt_usd(pnl_usd)} $</b> "
        f"({pnl_pct:+.2f}% к базовому депо 10 000 $)\n"
        f"• Сумма ориентировочная, без учёта проскальзывания и комиссий биржи.\n"
    )

    comment = (
        "\n🔎 <b>Комментарий</b>\n"
        "Стратегия симметрично работает по BTC и ETH: накапливает позицию при страхе "
        "(F&G в низких значениях) и частями фиксирует прибыль в фазах жадности по заранее "
        "заданной лестнице уровней. Мы продолжаем следить за тем, когда индекс вернётся "
        "в зону сильного страха для новых покупок или в зону экстремальной жадности для "
        "усиленной фиксации."
    )

    meta = load_monthly_meta()
    key = f"{year}-{month:02d}"

    text = header + fng_block + actions_block + result_block + comment

    res = send_telegram(text)
    message_id = res["result"]["message_id"]

    meta[key] = {"message_id": message_id, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct}
    save_monthly_meta(meta)

    # ссылки на отчёты этого же года
    links = []
    for m in range(1, 12 + 1):
        k = f"{year}-{m:02d}"
        if k in meta:
            mid = meta[k]["message_id"]
            m_name = month_name_ru_nom(m).capitalize()
            links.append(
                f"• <a href=\"https://t.me/{TELEGRAM_CHAT_ID.lstrip('@')}/{mid}\">{m_name} {year}</a>"
            )

    if links:
        links_block = "\n\n🔗 <b>Отчёты за этот год</b>\n" + "\n".join(links)
        send_telegram(links_block)

    print("Месячный отчёт отправлен.")


if __name__ == "__main__":
    main()
