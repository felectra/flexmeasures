"""Модель батарейного енерго-арбітражу на реальному профілі Мультіплекса (Сіті Центр, Миколаїв).

Ідея: заряд у дешеве нічне вікно (23:00–07:00), розряд у дороге вечірнє (17:00–23:00) для
самоспоживання — зсув споживання в часі, без експорту. Рухається лише ЕНЕРГО-складова ціни;
передача+розподіл плоскі за кВт·год, тож круговий ККД навіть додає трохи мережевих витрат.

Ціни РЕПРЕЗЕНТАТИВНІ (з дослідження ринку UA 2025–2026, ex-VAT) — замінюються реальним тарифом
Мультіплекса, щойно надійде. Дає порядок величини й ілюстративний розмір, не остаточний рахунок.

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
CYCLES = 1  # циклів/добу (один нічний заряд → вечірній розряд; більше циклів вимагає реальних вікон)
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


def daily_peak_energies(series):
    """Добова енергія у вечірньому піковому вікні (кВт·год) по КОЖНІЙ добі — стеля корисного розряду."""
    byday = collections.defaultdict(float)
    have = collections.defaultdict(bool)
    for ts, v in series:
        if v is not None and ts.hour in PEAK_H:
            byday[ts.date()] += v
            have[ts.date()] = True
    return [byday[d] for d in byday if have[d]]


def deliverable_per_day(e_usable, daily_peaks, cycles=CYCLES):
    """Сер. доставлена енергія (кВт·год/добу): усереднюємо min(ємність, добовий пік) ПО ДОБАХ."""
    return sum(min(e_usable, p) for p in daily_peaks) / len(daily_peaks) * cycles


def annual_saving(e_usable, daily_peaks, cycles=CYCLES):
    """Річна економія (грн): рахуємо подобово (mean of min), потім ×365 — не min(середнього),
    бо в дні з навантаженням нижчим за ємність батарея не «дозаряджається» до середнього.
    """
    per_day = []
    for peak in daily_peaks:
        e_deliver = (
            min(e_usable, peak) * cycles
        )  # обмежено піковим споживанням цієї доби
        charge = e_deliver / ETA
        per_day.append(
            e_deliver * PEAK_ENERGY
            - charge * NIGHT_ENERGY
            - (charge - e_deliver) * FLAT_NETWORK
        )
    saving = sum(per_day) / len(per_day) * 365
    return saving, deliverable_per_day(e_usable, daily_peaks, cycles)


def main():
    series = load_series()
    daily_peaks = daily_peak_energies(series)
    ndays = len(daily_peaks)
    peak_avail = sum(daily_peaks) / ndays
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
        yr, ed = annual_saving(e, daily_peaks)
        paybacks = []
        for cap in CAPEX.values():
            capex = e * cap
            paybacks.append(f"{capex / yr:>5.1f}р" if yr > 0 else "  n/a")
        print(f"{e:>8} {ed:>11.0f}к {yr:>13,.0f} грн  " + "  ".join(paybacks))

    # Ілюстративний розмір — не висновок сайзингу: «коліна» ефективності в цьому діапазоні немає,
    # остаточну ємність підбирають під майданчик, капітал і NPV.  Беремо найменший, що покриває пік.
    illus = min((e for e in SWEEP if e >= 0.9 * peak_avail), default=SWEEP[-1])
    yr, ed = annual_saving(illus, daily_peaks)
    print(
        f"\nІЛЮСТРАТИВНО (1 цикл): ~{illus} кВт·год — покриває ~весь вечірній пік ({peak_avail:.0f} кВт·год)."
    )
    print(
        f"  Валова економія ~{yr:,.0f} грн/рік; проста окупність {illus*CAPEX['середній']/yr:.1f} р (лише capex, середній сценарій)."
    )
    # чутливість до захопленого спреду (доставлена енергія — подобово усереднена)
    ed_avg = deliverable_per_day(illus, daily_peaks)
    print(
        "\nЧутливість валової економії до захопленого спреду (за ілюстративний розмір):"
    )
    for sp in (3, 4, 5, 6):
        print(f"  спред {sp} грн/кВт·год → ~{ed_avg*sp*365:,.0f} грн/рік")


if __name__ == "__main__":
    main()
