import os
import json
from datetime import datetime, date, timedelta, timezone

import requests

CMC_API_KEY = os.environ.get("CMC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MONTHLY_META_FILE = "monthly_meta.json"
YEARLY_META_FILE = "yearly_meta.json"
STATE_FILE = "secretary_state.json"
BASE_CAPITAL = 10_000.0


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


def get_year_bounds():
    """
    Берём прошедший год.
    Если сейчас 2026-й — отчёт будет за 2025-й.
    """
    today = datetime.now(timezone.utc).date()
    year = today.year - 1
    return year, date(year, 1, 1), date(year, 12, 31)


def get_yearly_fng_stats(year: int):
    if not CMC_API_KEY:
        return None

    start = date(year, 1, 1)
    end = date(year, 12, 31)
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
    timestamps = [x["timestamp"] for x in data_sorted]

    vmin = min(values)
    vmax = max(values)
    avg = sum(values) / len(values)

    idx_min = values.index(vmin)
    idx_max = values.index(vmax)

    ts_min = datetime.fromisoformat(
        timestamps[idx_min].replace("Z", "+00:00")
    ).date()
    ts_max = datetime.fromisoformat(
        timestamps[idx_max].replace("Z", "+00:00")
    ).date()

    return {
        "min": vmin,
        "min_date": ts_min,
        "max": vmax,
        "max_date": ts_max,
        "avg": avg,
    }


def fmt_usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def main():
    if not (CMC_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Не заданы переменные окружения для годового отчёта")
        return

    year, start, end = get_year_bounds()
    monthly_meta = load_json(MONTHLY_META_FILE, {})
    yearly_meta = load_json(YEARLY_META_FILE, {})

    pnl_year_usd = 0.0
    for m in range(1, 13):
        key = f"{year}-{m:02d}"
        if key in monthly_meta:
            pnl_year_usd += monthly_meta[key].get("pnl_usd", 0.0)

    pnl_year_pct = pnl_year_usd / BASE_CAPITAL * 100 if BASE_CAPITAL > 0 else 0.0

    fng_stats = get_yearly_fng_stats(year)

    header = f"📆 <b>Итоги за {year} год</b>\n"

    if fng_stats:
        f_min = fng_stats["min"]
        d_min = fng_stats["min_date"]
        f_max = fng_stats["max"]
        d_max = fng_stats["max_date"]
        f_avg = fng_stats["avg"]

        fng_block = (
            "\n📊 <b>Индекс страха и жадности</b>\n"
            f"За {year} год минимальное значение индекса зафиксировано <b>{d_min.strftime('%d.%m.%Y')}</b> "
            f"на уровне <b>{f_min}</b>, а максимальное — <b>{d_max.strftime('%d.%m.%Y')}</b> "
            f"на уровне <b>{f_max}</b>.\n"
            f"Среднее значение индекса за год: <b>{f_avg:.1f}</b>.\n"
        )
    else:
        fng_block = "\n📊 Не удалось получить статистику индекса за год.\n"

    result_block = (
        f"\n💰 <b>Финансовый результат</b>\n"
        f"По итогам {year} года стратегия зафиксировала совокупный результат:\n"
        f"• PnL: <b>{fmt_usd(pnl_year_usd)} $</b> "
        f"({pnl_year_pct:+.2f}% к базовому депо 10 000 $)\n"
        f"• Сумма ориентировочная, без учёта проскальзывания и комиссий биржи.\n"
    )

    # текущее состояние виртуального портфеля
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        cash = float(state.get("cash_usd", BASE_CAPITAL))
        btc = float(state.get("btc_amount", 0.0))
        eth = float(state.get("eth_amount", 0.0))

        try:
            btc_price = float(
                requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin", "vs_currencies": "usd"},
                    timeout=10,
                ).json()["bitcoin"]["usd"]
            )
            eth_price = float(
                requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "ethereum", "vs_currencies": "usd"},
                    timeout=10,
                ).json()["ethereum"]["usd"]
            )
        except Exception:
            btc_price = 0.0
            eth_price = 0.0

        btc_value = btc * btc_price
        eth_value = eth * eth_price
        total_value = cash + btc_value + eth_value
        total_pct = (total_value / BASE_CAPITAL - 1) * 100 if BASE_CAPITAL > 0 else 0.0

        state_block = (
            "\n💼 <b>Текущее состояние виртуального портфеля</b>\n"
            f"Кэш: <b>{fmt_usd(cash)} $</b>\n"
            f"BTC: <b>{btc:.6f}</b> (~<b>{fmt_usd(btc_value)} $</b>)\n"
            f"ETH: <b>{eth:.6f}</b> (~<b>{fmt_usd(eth_value)} $</b>)\n"
            f"Совокупная стоимость: <b>{fmt_usd(total_value)} $</b> "
            f"({total_pct:+.2f}% к базовому депо)\n"
        )
    else:
        state_block = "\n💼 Данные о текущем состоянии портфеля недоступны.\n"

    comment = (
        "\n🔎 <b>Комментарий</b>\n"
        "Модель одинаково относится к BTC и ETH: наращивает позицию при страхе и "
        "поэтапно фиксирует прибыль в фазах жадности. Лестница покупок и продаж "
        "осталась неизменной: целевые объёмы завязаны на значения F&G, что позволяет "
        "избегать попыток угадать точное дно или вершину и вместо этого работать "
        "через набор и разгрузку в заранее определённых зонах."
    )

    res = send_telegram(header + fng_block + result_block + state_block + comment)
    message_id = res["result"]["message_id"]

    yearly_meta[str(year)] = {
        "message_id": message_id,
        "pnl_usd": pnl_year_usd,
        "pnl_pct": pnl_year_pct,
    }
    save_json(YEARLY_META_FILE, yearly_meta)

    # ссылки на предыдущие годы
    links = []
    for y, meta in sorted(yearly_meta.items()):
        mid = meta.get("message_id")
        if mid:
            links.append(
                f"• <a href=\"https://t.me/{TELEGRAM_CHAT_ID.lstrip('@')}/{mid}\">{y}</a>"
            )

    if links:
        links_block = "\n\n🔗 <b>Отчёты за предыдущие годы</b>\n" + "\n".join(links)
        send_telegram(links_block)

    print("Годовой отчёт отправлен.")


if __name__ == "__main__":
    main()
