import os
import json
import math
import csv
from datetime import datetime, timezone

import requests

STATE_FILE = "secretary_state.json"
TRADES_FILE = "trades.csv"

BASE_CAPITAL = 10_000.0  # общий виртуальный депозит для BTC+ETH

CMC_API_KEY = os.environ.get("CMC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def fmt_usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def load_state():
    if not os.path.exists(STATE_FILE):
        state = {
            "base_capital": BASE_CAPITAL,
            "cash_usd": BASE_CAPITAL,
            "btc_amount": 0.0,
            "eth_amount": 0.0,
            "avg_entry_btc": None,
            "avg_entry_eth": None,
            # флаги сработавших уровней внутри текущего цикла
            "buy_used": {
                "40": False,
                "35": False,
                "30": False,
                "25": False,
                "20": False,
                "15": False,
            },
            "sell_used": {
                "60": False,
                "65": False,
                "70": False,
            },
        }
        save_state(state)
        return state
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_trade(asset, action, fng, price, usd_amount, asset_delta, cash_after, asset_after, avg_entry_price):
    """
    asset: 'BTC' или 'ETH'
    action: 'BUY' или 'SELL'
    """
    is_new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new:
            writer.writerow([
                "timestamp_utc",
                "asset",
                "action",
                "fng",
                "price",
                "usd_amount",
                "asset_delta",
                "cash_after",
                "asset_after",
                "avg_entry_price"
            ])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            asset,
            action,
            fng,
            price,
            usd_amount,
            asset_delta,
            cash_after,
            asset_after,
            avg_entry_price if avg_entry_price is not None else ""
        ])


def get_fng_cmc():
    """
    Получаем последний индекс страха и жадности от CoinMarketCap.
    """
    url = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {"limit": 1}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    item = data["data"][0]
    value = int(item["value"])
    ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
    return value, ts


def get_price(symbol: str) -> float:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": symbol}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------- ПАРАМЕТРЫ СТРАТЕГИИ ----------

# лестница покупок: доли от текущего кэша
BUY_LADDER = {
    40: 0.10,
    35: 0.10,
    30: 0.10,
    25: 0.20,
    20: 0.25,
    15: 0.25,
}

# лестница продаж: доли от текущей позиции BTC+ETH
SELL_L60 = 0.30
SELL_L65 = 0.20
SELL_L70 = 0.10
# на 75 продаём всё, что осталось


def reset_cycle_flags(state):
    state["buy_used"] = {k: False for k in state["buy_used"].keys()}
    state["sell_used"] = {k: False for k in state["sell_used"].keys()}


def main():
    if not (CMC_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Не заданы переменные окружения: CMC_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    state = load_state()
    cash = float(state["cash_usd"])
    btc = float(state["btc_amount"])
    eth = float(state["eth_amount"])
    avg_btc = state["avg_entry_btc"]
    avg_eth = state["avg_entry_eth"]

    try:
        fng, fng_ts = get_fng_cmc()
        btc_price = get_price("BTCUSDT")
        eth_price = get_price("ETHUSDT")
    except Exception as e:
        print("Ошибка при запросе данных:", e)
        return

    base = float(state["base_capital"])

    actions_text_parts = []

    # ---------- ПРОДАЖА СНАЧАЛА ----------

    # полная ликвидация при F&G >= 75
    if fng >= 75 and (btc > 0 or eth > 0):
        usd_from_btc = btc * btc_price
        usd_from_eth = eth * eth_price
        total_usd = usd_from_btc + usd_from_eth

        cash += total_usd
        btc = 0.0
        eth = 0.0
        avg_btc = None
        avg_eth = None

        reset_cycle_flags(state)

        pct_initial = total_usd / base * 100 if base > 0 else 0.0

        actions_text_parts.append(
            "📈 <b>Сигнал: ПОЛНАЯ ПРОДАЖА BTC и ETH (уровень 75)</b>\n"
            f"Индекс страха и жадности: <b>{fng}</b>\n\n"
            f"Объём сделки: <b>{fmt_usd(total_usd)} $</b> "
            f"(≈ {pct_initial:.2f}% от базового портфеля)\n"
            f"BTC: продано на ~<b>{fmt_usd(usd_from_btc)} $</b>\n"
            f"ETH: продано на ~<b>{fmt_usd(usd_from_eth)} $</b>"
        )

        # логируем сделки отдельно по BTC и ETH
        log_trade(
            asset="BTC",
            action="SELL",
            fng=fng,
            price=btc_price,
            usd_amount=usd_from_btc,
            asset_delta=-btc,   # но btc уже 0, поэтому логируем через исходное значение:
            cash_after=cash,
            asset_after=0.0,
            avg_entry_price=avg_btc,
        )
        log_trade(
            asset="ETH",
            action="SELL",
            fng=fng,
            price=eth_price,
            usd_amount=usd_from_eth,
            asset_delta=-eth,
            cash_after=cash,
            asset_after=0.0,
            avg_entry_price=avg_eth,
        )

    else:
        # частичные продажи на 70, 65, 60 (в таком порядке, сверху вниз)
        total_portfolio_usd_before = btc * btc_price + eth * eth_price + cash

        # 70: 10% позиции
        if 70 <= fng < 75 and (btc > 0 or eth > 0) and not state["sell_used"]["70"]:
            frac = SELL_L70
            sell_btc = btc * frac
            sell_eth = eth * frac
            usd_from_btc = sell_btc * btc_price
            usd_from_eth = sell_eth * eth_price
            total_usd = usd_from_btc + usd_from_eth

            btc -= sell_btc
            eth -= sell_eth
            cash += total_usd

            state["sell_used"]["70"] = True

            pct_initial = total_usd / base * 100 if base > 0 else 0.0
            pct_pos = frac * 100

            actions_text_parts.append(
                "📈 <b>Сигнал: ПРОДАЖА BTC и ETH (уровень 70)</b>\n"
                f"Индекс страха и жадности: <b>{fng}</b>\n\n"
                f"Объём сделки: <b>{fmt_usd(total_usd)} $</b> "
                f"(≈ {pct_initial:.2f}% от базового портфеля, {pct_pos:.2f}% от текущей позиции)\n"
                f"BTC: продано на ~<b>{fmt_usd(usd_from_btc)} $</b>\n"
                f"ETH: продано на ~<b>{fmt_usd(usd_from_eth)} $</b>"
            )

            log_trade(
                asset="BTC",
                action="SELL",
                fng=fng,
                price=btc_price,
                usd_amount=usd_from_btc,
                asset_delta=-sell_btc,
                cash_after=cash,
                asset_after=btc,
                avg_entry_price=avg_btc,
            )
            log_trade(
                asset="ETH",
                action="SELL",
                fng=fng,
                price=eth_price,
                usd_amount=usd_from_eth,
                asset_delta=-sell_eth,
                cash_after=cash,
                asset_after=eth,
                avg_entry_price=avg_eth,
            )

        # 65: 20% позиции
        if 65 <= fng < 70 and (btc > 0 or eth > 0) and not state["sell_used"]["65"]:
            frac = SELL_L65
            sell_btc = btc * frac
            sell_eth = eth * frac
            usd_from_btc = sell_btc * btc_price
            usd_from_eth = sell_eth * eth_price
            total_usd = usd_from_btc + usd_from_eth

            btc -= sell_btc
            eth -= sell_eth
            cash += total_usd

            state["sell_used"]["65"] = True

            pct_initial = total_usd / base * 100 if base > 0 else 0.0
            pct_pos = frac * 100

            actions_text_parts.append(
                "📈 <b>Сигнал: ПРОДАЖА BTC и ETH (уровень 65)</b>\n"
                f"Индекс страха и жадности: <b>{fng}</b>\n\n"
                f"Объём сделки: <b>{fmt_usd(total_usd)} $</b> "
                f"(≈ {pct_initial:.2f}% от базового портфеля, {pct_pos:.2f}% от текущей позиции)\n"
                f"BTC: продано на ~<b>{fmt_usd(usd_from_btc)} $</b>\n"
                f"ETH: продано на ~<b>{fmt_usd(usd_from_eth)} $</b>"
            )

            log_trade(
                asset="BTC",
                action="SELL",
                fng=fng,
                price=btc_price,
                usd_amount=usd_from_btc,
                asset_delta=-sell_btc,
                cash_after=cash,
                asset_after=btc,
                avg_entry_price=avg_btc,
            )
            log_trade(
                asset="ETH",
                action="SELL",
                fng=fng,
                price=eth_price,
                usd_amount=usd_from_eth,
                asset_delta=-sell_eth,
                cash_after=cash,
                asset_after=eth,
                avg_entry_price=avg_eth,
            )

        # 60: 30% позиции
        if 60 <= fng < 65 and (btc > 0 or eth > 0) and not state["sell_used"]["60"]:
            frac = SELL_L60
            sell_btc = btc * frac
            sell_eth = eth * frac
            usd_from_btc = sell_btc * btc_price
            usd_from_eth = sell_eth * eth_price
            total_usd = usd_from_btc + usd_from_eth

            btc -= sell_btc
            eth -= sell_eth
            cash += total_usd

            state["sell_used"]["60"] = True

            pct_initial = total_usd / base * 100 if base > 0 else 0.0
            pct_pos = frac * 100

            actions_text_parts.append(
                "📈 <b>Сигнал: ПРОДАЖА BTC и ETH (уровень 60)</b>\n"
                f"Индекс страха и жадности: <b>{fng}</b>\n\n"
                f"Объём сделки: <b>{fmt_usd(total_usd)} $</b> "
                f"(≈ {pct_initial:.2f}% от базового портфеля, {pct_pos:.2f}% от текущей позиции)\n"
                f"BTC: продано на ~<b>{fmt_usd(usd_from_btc)} $</b>\n"
                f"ETH: продано на ~<b>{fmt_usd(usd_from_eth)} $</b>"
            )

            log_trade(
                asset="BTC",
                action="SELL",
                fng=fng,
                price=btc_price,
                usd_amount=usd_from_btc,
                asset_delta=-sell_btc,
                cash_after=cash,
                asset_after=btc,
                avg_entry_price=avg_btc,
            )
            log_trade(
                asset="ETH",
                action="SELL",
                fng=fng,
                price=eth_price,
                usd_amount=usd_from_eth,
                asset_delta=-sell_eth,
                cash_after=cash,
                asset_after=eth,
                avg_entry_price=avg_eth,
            )

    # если вышли полностью → сбросить цикл
    if btc <= 0 and eth <= 0:
        btc = 0.0
        eth = 0.0
        avg_btc = None
        avg_eth = None
        reset_cycle_flags(state)

    # ---------- ПОКУПКИ ПО ЛЕСТНИЦЕ ----------

    # Проверяем уровни 40,35,30,25,20,15 по порядку (от меньшей агрессии к большей)
    # В один запуск могут сработать сразу несколько уровней, если F&G низкий.
    buy_order = [40, 35, 30, 25, 20, 15]

    for level in buy_order:
        frac = BUY_LADDER[level]
        key = str(level)
        if not state["buy_used"][key] and fng <= level and cash > 0 and frac > 0:
            spend = cash * frac
            if spend <= 0:
                continue

            # половина в BTC, половина в ETH
            usd_btc = spend / 2.0
            usd_eth = spend / 2.0

            buy_btc = usd_btc / btc_price
            buy_eth = usd_eth / eth_price

            # обновляем среднюю цену
            if buy_btc > 0:
                if btc <= 0:
                    avg_btc = btc_price
                else:
                    total_cost_btc = avg_btc * btc + usd_btc
                    btc_new = btc + buy_btc
                    avg_btc = total_cost_btc / btc_new
            if buy_eth > 0:
                if eth <= 0:
                    avg_eth = eth_price
                else:
                    total_cost_eth = avg_eth * eth + usd_eth
                    eth_new = eth + buy_eth
                    avg_eth = total_cost_eth / eth_new

            btc += buy_btc
            eth += buy_eth
            cash -= spend

            state["buy_used"][key] = True

            pct_initial = spend / base * 100 if base > 0 else 0.0
            pct_cash = frac * 100

            actions_text_parts.append(
                "📉 <b>Сигнал: ПОКУПКА BTC и ETH</b>\n"
                f"Уровень индекса: <b>{level}</b>, текущий F&G: <b>{fng}</b>\n\n"
                f"Общий объём покупки: <b>{fmt_usd(spend)} $</b> "
                f"(≈ {pct_initial:.2f}% от базового портфеля, {pct_cash:.2f}% от текущего кэша)\n"
                f"BTC: покупка на ~<b>{fmt_usd(usd_btc)} $</b>\n"
                f"ETH: покупка на ~<b>{fmt_usd(usd_eth)} $</b>"
            )

            log_trade(
                asset="BTC",
                action="BUY",
                fng=fng,
                price=btc_price,
                usd_amount=usd_btc,
                asset_delta=buy_btc,
                cash_after=cash,
                asset_after=btc,
                avg_entry_price=avg_btc,
            )
            log_trade(
                asset="ETH",
                action="BUY",
                fng=fng,
                price=eth_price,
                usd_amount=usd_eth,
                asset_delta=buy_eth,
                cash_after=cash,
                asset_after=eth,
                avg_entry_price=avg_eth,
            )

    # если нет ни одной покупки/продажи — ничего не шлём
    if not actions_text_parts:
        print(f"Сигналов нет. F&G={fng}, BTC={btc_price}, ETH={eth_price}")
        # просто сохраняем состояние
        state["cash_usd"] = cash
        state["btc_amount"] = btc
        state["eth_amount"] = eth
        state["avg_entry_btc"] = avg_btc
        state["avg_entry_eth"] = avg_eth
        save_state(state)
        return

    # обновляем состояние
    state["cash_usd"] = cash
    state["btc_amount"] = btc
    state["eth_amount"] = eth
    state["avg_entry_btc"] = avg_btc
    state["avg_entry_eth"] = avg_eth
    save_state(state)

    total_value = cash + btc * btc_price + eth * eth_price
    port_change_pct = (total_value / base - 1.0) * 100 if base > 0 else 0.0

    summary = (
        "\n\n💼 <b>Состояние виртуального портфеля</b>\n"
        f"Кэш: <b>{fmt_usd(cash)} $</b>\n"
        f"BTC: <b>{btc:.6f}</b> (~<b>{fmt_usd(btc * btc_price)} $</b>)\n"
        f"ETH: <b>{eth:.6f}</b> (~<b>{fmt_usd(eth * eth_price)} $</b>)\n"
        f"Итого: <b>{fmt_usd(total_value)} $</b> "
        f"({port_change_pct:+.2f}% к базовому 10 000 $)\n"
        f"Средняя цена входа BTC: <b>{fmt_usd(avg_btc) + ' USDT' if avg_btc else '—'}</b>\n"
        f"Средняя цена входа ETH: <b>{fmt_usd(avg_eth) + ' USDT' if avg_eth else '—'}</b>"
    )

    text = "\n\n".join(actions_text_parts) + summary

    try:
        send_telegram(text)
        print("Сигнал(ы) отправлен(ы) в Telegram.")
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


if __name__ == "__main__":
    main()
