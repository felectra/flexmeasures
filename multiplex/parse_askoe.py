"""Розбір АСКОЕ-файлів «Добовий графік Мультиплекс» у чисту 30-хвилинну серію навантаження.

Вхід: місячні широкі CSV (utf-8-sig, роздільник «;», кома-десяткові) у півот-розкладці —
рядки = 48 півгодинних інтервалів, стовпці = день × точка обліку × канал (A+/R+).
Беремо канал активного імпорту (A+) точки «Вв1 0.4 кВ» як повне споживання об'єкта (підтверджено
власником), по одному значенню на 30-хв інтервал на добу.
Вихід: довга серія (timestamp, kwh_30min, kw) у CSV, плюс зведена статистика й добовий профіль.

Запуск:  python3 multiplex/parse_askoe.py
Вихідний CSV (`data/load_30min.csv`) — похідні клієнтські дані, у git не комітяться (gitignore).
"""

import collections
import csv
import os
import re
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TOTAL_POINT = "Вв1 0.4 кВ"  # підтверджено = ввід 0.4 кВ усього об'єкта
CHANNEL = "A+"  # активний імпорт
OUT = os.path.join(DATA_DIR, "load_30min.csv")


def _ffill(row):
    """Forward-fill sparse header cells (точка/канал стоять лише на 1-й колонці блоку)."""
    out, last = [], ""
    for i, v in enumerate(row):
        v = (v or "").strip()
        if i == 0:
            out.append(v)
            continue
        if v:
            last = v
        out.append(last)
    return out


def _num(s):
    """Кома-десяткове -> float; порожнє/нечислове -> None (реальний пропуск)."""
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Рядок-слот: col0 виду 'HH:MM-...'. Регекс надійно відсіює підсумкові/сторонні рядки.
_SLOT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-")


def _parse_date(d):
    """Обидва наявні формати заголовка: 'DD.MM.YYYY' (січ–квіт) і 'M/D/YY' (трав–серп, US)."""
    d = (d or "").strip()
    try:
        if "." in d:
            return datetime.strptime(d, "%d.%m.%Y")
        if "/" in d:
            return datetime.strptime(d, "%m/%d/%y")
    except ValueError:
        return None
    return None


def parse_file(path):
    """Повертає список (datetime, kwh_or_None) для точки TOTAL_POINT, каналу A+."""
    rows = list(csv.reader(open(path, encoding="utf-8-sig"), delimiter=";"))
    tochka, kanal, dates = _ffill(rows[1]), _ffill(rows[2]), rows[3]
    cols = {}  # дата -> індекс колонки A+ цієї точки
    for i in range(1, len(tochka)):
        if tochka[i] == TOTAL_POINT and kanal[i].startswith(CHANNEL):
            d = (dates[i] or "").strip()
            if d:
                cols[d] = i
    records = []
    for r in rows[4:]:
        if not r:
            continue
        mslot = _SLOT_RE.match(r[0] or "")
        if not mslot:
            continue
        h, m = int(mslot.group(1)), int(mslot.group(2))
        for d, ci in cols.items():
            day = _parse_date(d)
            if day is None:
                continue
            val = _num(r[ci]) if ci < len(r) else None
            records.append((day + timedelta(hours=h, minutes=m), val))
    return records


def main():
    files = sorted(
        f
        for f in os.listdir(DATA_DIR)
        if f.lower().endswith(".csv") and f != os.path.basename(OUT)
    )
    bytime = {}
    for f in files:
        rec = parse_file(os.path.join(DATA_DIR, f))
        got = sum(1 for _, v in rec if v is not None)
        print(f"{f}: {len(rec)} слотів, {got} зі значенням")
        for ts, v in rec:
            # один місяць = унікальні дати, але страхуємось: не затираємо значення пропуском
            if v is not None or ts not in bytime:
                bytime[ts] = v
    series = sorted(bytime.items())

    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "kwh_30min", "kw"])
        for ts, v in series:
            w.writerow(
                [
                    ts.isoformat(),
                    "" if v is None else f"{v:.5f}",
                    "" if v is None else f"{v * 2:.5f}",
                ]
            )

    vals = [v for _, v in series if v is not None]
    n = len(series)
    miss = n - len(vals)
    print(f"\nзаписано {OUT}: {n} слотів, {miss} пропусків ({100 * miss / n:.1f}%)")
    if not vals:
        return
    print(f"діапазон: {series[0][0]}  ..  {series[-1][0]}")
    print(
        f"кВт·год/30хв: сер {sum(vals) / len(vals):.2f}  min {min(vals):.2f}  max {max(vals):.2f}"
    )
    print(
        f"потужність кВт: сер {2 * sum(vals) / len(vals):.1f}  пік {2 * max(vals):.1f}"
    )

    # енергія по місяцях (кВт·год)
    permonth = collections.defaultdict(float)
    for ts, v in series:
        if v is not None:
            permonth[ts.strftime("%Y-%m")] += v
    print("\nмісяць    кВт·год")
    for mth in sorted(permonth):
        print(f"{mth}   {permonth[mth]:>10.0f}")

    # середній добовий профіль (кВт) по годинах
    prof = collections.defaultdict(list)
    for ts, v in series:
        if v is not None:
            prof[ts.hour].append(v * 2)
    print("\nгодина  сер_кВт")
    for h in range(24):
        if prof[h]:
            print(f"{h:>2}:00   {sum(prof[h]) / len(prof[h]):>6.1f}")


if __name__ == "__main__":
    main()
