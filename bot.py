import os
import json
import csv
import math
from datetime import datetime, timezone

import requests

STATE_FILE = "secretary_state.json"
TRADES_FILE = "trades.csv"

BASE_CAPITAL = 10_000.0  # базовый виртуальный депозит

CMC_API_KEY = os.environ.get("CMC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ---- ПАРАМЕТРЫ СТРАТЕГИИ ----

# Целевые суммы покупок по уровням F&G (кратно 50$, суммарно 10 000 $)
BUY_TARGETS = {
    40: 1100.0,
    35: 1500.0,
    30: 1850.0,
    25: 1850.0,
    20: 1850.0,
    15: 1850.0,
}

# Порядок уровней для покупок и продаж
BUY_LEVELS = [40, 35, 30, 25, 20, 15]
SELL_LEVELS = [60, 65, 70, 75]

# Доли продажи от ЦЕЛЕВОГО пакета на каждый уровень (в сумме ≈ 100%)
SELL_FRACS = {
    60: 0.25,
    65: 0.25,
    70: 0.25,
    75: 0.25,
}


# ---- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----

def fmt_usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def round_down_50(x: float) -> float:
    """Округление вниз до ближайших 50$."""
    if x <= 0:
        return 0.0
    return math.floor(x / 50.0) * 50.0


def load_state():
    if not os.path.exists(STATE_FILE):
        # начальное состояние
        state = {
            "base_capital": BASE_CAPITAL,
            "cash_usd": BASE_CAPITAL,
            "btc_amount": 0.0,
            "eth_amount": 0.0,
            "avg_entry_btc": None,
            "avg_entry_eth": None,
            # Бакеты по уровням покупок: сколько сейчас "вложено" и сколько BTC/ETH за этим стоит
            "buckets": {
                str(lvl): {
                    "invested_usd": 0.0,
                    "btc_amount": 0.0,
                    "eth_amount": 0.0,
                }
                for lvl in BUY_LEVELS
            },
            # флажки, чтобы каждый уровень ПРОДАЖИ сработал максимум 1 раз за цикл
            "sell_used": {str(lvl): False for lvl in SELL_LEVELS},
        }
        save_state(state)
        return state

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    # если старый стейт без бакетов — инициализируем
    if "buckets" not in state:
        state["buckets"] = {
            str(lvl): {
                "invested_usd": 0.0,
                "btc_amount": 0.0,
                "eth_amount": 0.0,
            }
            for lvl in BUY_LEVELS
        }
    # если нет sell_used — добавляем
    if "sell_used" not in state:
        state["sell_used"] = {str(lvl): False for lvl in SELL_LEVELS}

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_trade(
    asset: str,
    action: str,
    fng: int,
    price: float,
    usd_amount: float,
    asset_delta: float,
    cash_after: float,
    asset_after: float,
    avg_entry_price: float | None,
):
    """Логируем сделку в trades.csv"""
    is_new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new:
            writer.writerow(
                [
                    "timestamp_utc",
                    "asset",
                    "action",
                    "fng",
                    "price",
                    "usd_amount",
                    "asset_delta",
                    "cash_after",
                    "asset_after",
                    "avg_entry_price",
                ]
            )
        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(),
                asset,
                action,
                fng,
                price,
                usd_amount,
                asset_delta,
                cash_after,
                asset_after,
                avg_entry_price if avg_entry_price is not None else "",
            ]
        )


def get_fng_cmc():
    """
    Получаем последний индекс страха и жадности от CoinMarketCap.
    Поддерживаем Unix timestamp и ISO-формат.
    """
    url = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {"limit": 1}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    item = data["data"][0]
    value = int(item["value"])

    ts_raw = str(item.get("timestamp", ""))
    if ts_raw.isdigit():
        ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
    else:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))

    return value, ts


def get_price(symbol: str) -> float:
    """
    Цена через CoinGecko (без ключа):
    symbol: "BTCUSDT" или "ETHUSDT"
    """
    if symbol == "BTCUSDT":
        coin_id = "bitcoin"
    elif symbol == "ETHUSDT":
        coin_id = "ethereum"
    else:
        raise ValueError(f"Неизвестный тикер для CoinGecko: {symbol}")

    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data[coin_id]["usd"])


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


def reset_cycle(state):
    """Полный сброс цикла: когда мы полностью вышли из позиции."""
    state["buckets"] = {
        str(lvl): {
            "invested_usd": 0.0,
            "btc_amount": 0.0,
            "eth_amount": 0.0,
        }
        for lvl in BUY_LEVELS
    }
    state["sell_used"] = {str(lvl): False for lvl in SELL_LEVELS}
    state["avg_entry_btc"] = None
    state["avg_entry_eth"] = None


# ---- ОСНОВНАЯ ЛОГИКА ----

def main():
    if not (CMC_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print(
            "Не заданы переменные окружения: CMC_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
        )
        return

    state = load_state()

    cash = float(state.get("cash_usd", BASE_CAPITAL))
    btc = float(state.get("btc_amount", 0.0))
    eth = float(state.get("eth_amount", 0.0))
    avg_btc = state.get("avg_entry_btc")
    avg_eth = state.get("avg_entry_eth")
    buckets = state["buckets"]
    sell_used = state["sell_used"]
    base = float(state.get("base_capital", BASE_CAPITAL))

    try:
        fng, fng_ts = get_fng_cmc()
        btc_price = get_price("BTCUSDT")
        eth_price = get_price("ETHUSDT")
    except Exception as e:
        print("Ошибка при запросе данных:", e)
        return

    actions_text_parts: list[str] = []

    # --- Проверка: если полностью вышли из позиции, сбрасываем цикл ---
    total_invested = sum(bucket["invested_usd"] for bucket in buckets.values())
    if total_invested <= 0.0 and btc <= 0.0 and eth <= 0.0:
        reset_cycle(state)
        buckets = state["buckets"]
        sell_used = state["sell_used"]

    # ---------- БЛОК ПРОДАЖ ----------

    # обрабатываем уровни продаж последовательно: 60 → 65 → 70 → 75
    for lvl in SELL_LEVELS:
        lvl_str = str(lvl)
        if sell_used[lvl_str]:
            continue  # этот уровень продаж уже отработал в текущем цикле

        if fng >= lvl:
            # считаем суммарную продажу по всем бакетам
            frac = SELL_FRACS[lvl]

            total_sell_btc = 0.0
            total_sell_eth = 0.0
            total_sell_usd_btc = 0.0
            total_sell_usd_eth = 0.0

            for bl in BUY_LEVELS:
                bl_str = str(bl)
                bucket = buckets[bl_str]
                invested = float(bucket["invested_usd"])
                if invested <= 0:
                    continue

                # планируемый объём продажи по целевому пакету
                target = BUY_TARGETS[bl]
                planned_usd = target * frac

                # округляем вниз до 50$ и ограничиваем текущим остатком
                sell_usd = min(invested, round_down_50(planned_usd))
                if sell_usd <= 0:
                    continue

                # считаем, сколько BTC и ETH "сидит" в бакете по текущей цене
                bucket_btc = float(bucket["btc_amount"])
                bucket_eth = float(bucket["eth_amount"])

                btc_value = bucket_btc * btc_price
                eth_value = bucket_eth * eth_price
                bucket_value = btc_value + eth_value

                if bucket_value <= 0:
                    continue

                # делим продаваемую сумму между BTC и ETH пропорционально их текущей стоимости
                sell_btc_usd = sell_usd * (btc_value / bucket_value)
                sell_eth_usd = sell_usd * (eth_value / bucket_value)

                sell_btc_amt = sell_btc_usd / btc_price
                sell_eth_amt = sell_eth_usd / eth_price

                # защита от "сверхпродажи"
                sell_btc_amt = min(sell_btc_amt, bucket_btc, btc)
                sell_eth_amt = min(sell_eth_amt, bucket_eth, eth)

                # обновляем глобальные и локальные значения
                btc -= sell_btc_amt
                eth -= sell_eth_amt
                bucket["btc_amount"] -= sell_btc_amt
                bucket["eth_amount"] -= sell_eth_amt
                bucket["invested_usd"] -= sell_usd
                cash += sell_usd

                total_sell_btc += sell_btc_amt
                total_sell_eth += sell_eth_amt
                total_sell_usd_btc += sell_btc_usd
                total_sell_usd_eth += sell_eth_usd

            if total_sell_btc > 0 or total_sell_eth > 0:
                sell_used[lvl_str] = True

                total_sell_usd = total_sell_usd_btc + total_sell_usd_eth
                pct_initial = total_sell_usd / base * 100 if base > 0 else 0.0

                actions_text_parts.append(
                    "📈 <b>Сигнал: ПРОДАЖА BTC и ETH</b>\n"
                    f"Уровень жадности: <b>{lvl}</b>, текущий F&G: <b>{fng}</b>\n\n"
                    f"Общий объём продажи: <b>{fmt_usd(total_sell_usd)} $</b> "
                    f"(≈ {pct_initial:.2f}% от базового портфеля)\n"
                    f"BTC: продано на ~<b>{fmt_usd(total_sell_usd_btc)} $</b>\n"
                    f"ETH: продано на ~<b>{fmt_usd(total_sell_usd_eth)} $</b>"
                )

                # логируем сделку агрегированно по активам
                if total_sell_btc > 0:
                    log_trade(
                        asset="BTC",
                        action="SELL",
                        fng=fng,
                        price=btc_price,
                        usd_amount=total_sell_usd_btc,
                        asset_delta=-total_sell_btc,
                        cash_after=cash,
                        asset_after=btc,
                        avg_entry_price=avg_btc,
                    )
                if total_sell_eth > 0:
                    log_trade(
                        asset="ETH",
                        action="SELL",
                        fng=fng,
                        price=eth_price,
                        usd_amount=total_sell_usd_eth,
                        asset_delta=-total_sell_eth,
                        cash_after=cash,
                        asset_after=eth,
                        avg_entry_price=avg_eth,
                    )

    # после продаж проверяем, не вышли ли полностью
    total_invested = sum(bucket["invested_usd"] for bucket in buckets.values())
    if total_invested <= 0 and btc <= 0 and eth <= 0:
        cash = base  # виртуально считаем, что полностью вернулись в кэш
        reset_cycle(state)
        buckets = state["buckets"]
        sell_used = state["sell_used"]
        btc = 0.0
        eth = 0.0
        avg_btc = None
        avg_eth = None

    # ---------- БЛОК ПОКУПОК ----------

    # если F&G достаточно низкий — "подтягиваем" каждый пакет к целевому уровню
    for lvl in BUY_LEVELS:
        if fng <= lvl:
            lvl_str = str(lvl)
            bucket = buckets[lvl_str]
            target = BUY_TARGETS[lvl]
            invested = float(bucket["invested_usd"])

            need_usd = target - invested
            if need_usd <= 0:
                continue

            # не тратим больше, чем есть кэша
            need_usd = min(need_usd, cash)
            buy_usd = round_down_50(need_usd)

            if buy_usd <= 0:
                continue

            # половина в BTC, половина в ETH
            usd_btc = buy_usd / 2.0
            usd_eth = buy_usd / 2.0

            buy_btc_amount = usd_btc / btc_price
            buy_eth_amount = usd_eth / eth_price

            # обновляем среднюю цену входа
            if buy_btc_amount > 0:
                if btc <= 0:
                    avg_btc = btc_price
                else:
                    total_cost_btc = avg_btc * btc + usd_btc
                    btc_new = btc + buy_btc_amount
                    avg_btc = total_cost_btc / btc_new
            if buy_eth_amount > 0:
                if eth <= 0:
                    avg_eth = eth_price
                else:
                    total_cost_eth = avg_eth * eth + usd_eth
                    eth_new = eth + buy_eth_amount
                    avg_eth = total_cost_eth / eth_new

            # обновляем портфель и бакет
            btc += buy_btc_amount
            eth += buy_eth_amount
            bucket["btc_amount"] += buy_btc_amount
            bucket["eth_amount"] += buy_eth_amount
            bucket["invested_usd"] += buy_usd
            cash -= buy_usd

            pct_initial = buy_usd / base * 100 if base > 0 else 0.0

            actions_text_parts.append(
                "📉 <b>Сигнал: ПОКУПКА BTC и ETH</b>\n"
                f"Уровень индекса: <b>{lvl}</b>, текущий F&G: <b>{fng}</b>\n\n"
                f"Общий объём покупки: <b>{fmt_usd(buy_usd)} $</b> "
                f"(≈ {pct_initial:.2f}% от базового портфеля)\n"
                f"BTC: покупка на ~<b>{fmt_usd(usd_btc)} $</b>\n"
                f"ETH: покупка на ~<b>{fmt_usd(usd_eth)} $</b>"
            )

            # логируем покупку агрегированно по активам
            if buy_btc_amount > 0:
                log_trade(
                    asset="BTC",
                    action="BUY",
                    fng=fng,
                    price=btc_price,
                    usd_amount=usd_btc,
                    asset_delta=buy_btc_amount,
                    cash_after=cash,
                    asset_after=btc,
                    avg_entry_price=avg_btc,
                )
            if buy_eth_amount > 0:
                log_trade(
                    asset="ETH",
                    action="BUY",
                    fng=fng,
                    price=eth_price,
                    usd_amount=usd_eth,
                    asset_delta=buy_eth_amount,
                    cash_after=cash,
                    asset_after=eth,
                    avg_entry_price=avg_eth,
                )

    # ---------- ИТОГ И ОТПРАВКА В TELEGRAM ----------

    # если в этом прогоне не было ни покупок, ни продаж — молчим
    if not actions_text_parts:
        print(f"Сигналов нет. F&G={fng}, BTC={btc_price}, ETH={eth_price}")
        state["cash_usd"] = cash
        state["btc_amount"] = btc
        state["eth_amount"] = eth
        state["avg_entry_btc"] = avg_btc
        state["avg_entry_eth"] = avg_eth
        state["buckets"] = buckets
        state["sell_used"] = sell_used
        save_state(state)
        return

    # обновляем состояние
    state["cash_usd"] = cash
    state["btc_amount"] = btc
    state["eth_amount"] = eth
    state["avg_entry_btc"] = avg_btc
    state["avg_entry_eth"] = avg_eth
    state["buckets"] = buckets
    state["sell_used"] = sell_used
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
        f"Средняя цена входа BTC: <b>{fmt_usd(avg_btc)} USDT</b>" if avg_btc else "\nСредняя цена входа BTC: —"
    )

    if avg_eth:
        summary += f"\nСредняя цена входа ETH: <b>{fmt_usd(avg_eth)} USDT</b>"
    else:
        summary += "\nСредняя цена входа ETH: —"

    text = "\n\n".join(actions_text_parts) + summary

    try:
        send_telegram(text)
        print("Сигнал(ы) отправлен(ы) в Telegram.")
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


if __name__ == "__main__":
    main()
