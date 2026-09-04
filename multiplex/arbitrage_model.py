"""Модель батарейного енерго-арбітражу на реальному профілі Мультіплекса (Сіті Центр, Миколаїв).

Ідея: заряд у дешеве нічне вікно (23:00–07:00), розряд у дороге вечірнє (17:00–23:00) для
самоспоживання — зсув споживання в часі, без експорту. Рухається лише ЕНЕРГО-складова ціни;
передача+розподіл плоскі за кВт·год, тож круговий ККД навіть додає трохи мережевих витрат.

Ціни РЕПРЕЗЕНТАТИВНІ (з дослідження ринку UA 2025–2026, ex-VAT) — замінюються реальним тарифом
Мультіплекса, щойно надійде. Дає порядок величини й рекомендований розмір, не остаточний рахунок.

Запуск:  python3 multiplex/arbitrage_model.py
"""

import collections
import csv
import os
from datetime import datetime

DATA = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "load_30min.csv"
)

# --- Цінові припущення (ex-VAT, грн/кВт·год) — РЕПРЕЗЕНТАТИВНІ, звірити з рахунком ---
NIGHT_ENERGY = 3.30  # нічна енерго-ціна (дешеве вікно)
PEAK_ENERGY = 7.70  # вечірня пікова енерго-ціна
FLAT_NETWORK = 2.65  # передача+розподіл 2-й клас (плоска, Миколаївобленерго) — платиться на всі кВт·год
ETA = 0.87  # круговий ККД
CYCLES = 1  # циклів/добу (база; 2 цикли ~вдвічі — окремо)
PEAK_H = range(17, 23)  # вечірнє пікове вікно розряду
# capex сценарії (грн/кВт·год встановленої корисної ємності), із пільгою ПДВ/мито
CAPEX = {"низький": 11000, "середній": 14000, "високий": 18000}
SWEEP = [50, 100, 150, 200, 250, 300]  # кВт·год корисної ємності


def load_series():
    rows = list(csv.DictReader(open(DATA)))
    out = []
    for r in rows:
        v = r["kwh_30min"]
        out.append((datetime.fromisoformat(r["timestamp"]), float(v) if v else None))
    return out


def daily_peak_energy(series):
    """Середня добова енергія у вечірньому піковому вікні (кВт·год) — стеля корисного розряду."""
    byday = collections.defaultdict(float)
    have = collections.defaultdict(bool)
    for ts, v in series:
        if v is not None and ts.hour in PEAK_H:
            byday[ts.date()] += v
            have[ts.date()] = True
    days = [byday[d] for d in byday if have[d]]
    return sum(days) / len(days), len(days)


def annual_saving(e_usable, peak_avail, cycles=CYCLES):
    """Річна економія (грн) для батареї корисною ємністю e_usable кВт·год."""
    e_deliver = min(e_usable, peak_avail) * cycles  # обмежено піковим споживанням
    charge = e_deliver / ETA
    daily = (
        e_deliver * PEAK_ENERGY
        - charge * NIGHT_ENERGY
        - (charge - e_deliver) * FLAT_NETWORK
    )
    return daily * 365, e_deliver


def main():
    series = load_series()
    peak_avail, ndays = daily_peak_energy(series)
    spread_net = PEAK_ENERGY - NIGHT_ENERGY / ETA - (1 / ETA - 1) * FLAT_NETWORK
    print(f"Днів у даних: {ndays}")
    print(
        f"Сер. добова енергія у вікні 17:00–23:00: {peak_avail:.0f} кВт·год (стеля розряду/цикл)"
    )
    print(
        f"Чистий спред за кВт·год (пік − ніч/η − мережа·(1/η−1)): {spread_net:.2f} грн\n"
    )

    print(
        f"{'кВт·год':>8} {'достав/добу':>12} {'економія/рік':>16} "
        + "  ".join(f"окуп.{k}" for k in CAPEX)
    )
    for e in SWEEP:
        yr, ed = annual_saving(e, peak_avail)
        paybacks = []
        for cap in CAPEX.values():
            capex = e * cap
            paybacks.append(f"{capex / yr:>5.1f}р" if yr > 0 else "  n/a")
        print(f"{e:>8} {ed:>11.0f}к {yr:>13,.0f} грн  " + "  ".join(paybacks))

    # рекомендація: найменша батарея, що майже вичерпує пікове вікно (коліно ефективності)
    knee = min((e for e in SWEEP if e >= 0.9 * peak_avail), default=SWEEP[-1])
    yr, ed = annual_saving(knee, peak_avail)
    print(
        f"\nРЕКОМЕНДАЦІЯ (1 цикл): ~{knee} кВт·год — покриває ~весь вечірній пік ({peak_avail:.0f} кВт·год)."
    )
    print(
        f"  Економія ~{yr:,.0f} грн/рік; окупність {knee*CAPEX['середній']/yr:.1f} р (середній capex)."
    )
    yr2, _ = annual_saving(knee, peak_avail, cycles=2)
    print(
        f"  2 цикли/добу (нічний + денний-сонячний заряд влітку): ~{yr2:,.0f} грн/рік → окупність вдвічі краща."
    )
    # чутливість до спреду
    print("\nЧутливість річної економії до захопленого спреду (для рекоменд. розміру):")
    for sp in (3, 4, 5, 6):
        ed = min(knee, peak_avail)
        print(f"  спред {sp} грн/кВт·год → ~{ed*sp*365:,.0f} грн/рік")


if __name__ == "__main__":
    main()
