"""
Поиск номера в реестре российской системы и плана нумерации (DEF-9xx).

Источник данных: https://opendata.digital.gov.ru/registry/numeric/
Формат файла: АВС/DEF;От;До;Емкость;Оператор;Регион

Кодировка и разделитель определяются автоматически: файлы с разных зеркал
приходят то в cp1251, то в UTF-8.

Проверка полностью локальная — обращений к внешним API нет.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

RAW_COLUMNS = ["code", "num_from", "num_to", "capacity", "operator", "region"]

ENCODINGS = ("utf-8-sig", "cp1251", "utf-8", "koi8-r")
SEPARATORS = (";", ",", "\t")

# Подстроки для распознавания операторов "большой четвёрки".
# Всё остальное в DEF-9xx — это MVNO, региональные и мелкие операторы.
BIG_FOUR_MARKERS = (
    "мтс",
    "мобильные телесистемы",
    "мегафон",
    "вымпел",
    "билайн",
    "т2 мобайл",
    "т2мобайл",
    "теле2",
    "tele2",
    "t2 ",
)

# Диапазоны меньше этого размера почти всегда принадлежат
# небольшим операторам, корпоративной связи или IP-телефонии.
SMALL_RANGE_THRESHOLD = 10_000


@dataclass
class LookupResult:
    phone: str
    found: bool
    code: Optional[str] = None
    operator: Optional[str] = None
    region: Optional[str] = None
    capacity: Optional[int] = None
    category: str = "unknown"       # mno | mvno_or_small | unknown_range | invalid
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_phone(raw) -> Optional[str]:
    """Приводит номер к виду 7XXXXXXXXXX. Возвращает None, если это не мобильный РФ."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("79"):
        return None
    return digits


def _read_bytes(source) -> bytes:
    """Читает содержимое пути, файлового объекта или bytes."""
    if isinstance(source, bytes):
        return source
    if hasattr(source, "read"):
        if hasattr(source, "seek"):
            source.seek(0)
        data = source.read()
        return data.encode("utf-8") if isinstance(data, str) else data
    with open(source, "rb") as fh:
        return fh.read()


def _decode(raw: bytes) -> str:
    """Подбирает кодировку: проверяем не только отсутствие ошибок, но и наличие кириллицы."""
    fallback = None
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if re.search(r"[А-Яа-я]", text[:5000]):
            return text
        if fallback is None:
            fallback = text
    if fallback is not None:
        return fallback
    return raw.decode("utf-8", errors="replace")


def _detect_separator(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    counts = {sep: head.count(sep) for sep in SEPARATORS}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ";"


def load_def_plan(source) -> pd.DataFrame:
    """
    Читает DEF-9xx.csv. source — путь, файловый объект или bytes.

    Возвращает DataFrame с колонками RAW_COLUMNS плюс start_key/end_key
    (целочисленные ключи вида код * 10^7 + номер), отсортированный по start_key.
    """
    text = _decode(_read_bytes(source))
    separator = _detect_separator(text)

    df = pd.read_csv(
        io.StringIO(text),
        sep=separator,
        header=0,
        names=RAW_COLUMNS,
        usecols=range(6),          # в поле "Регион" встречаются лишние разделители
        on_bad_lines="skip",
        engine="python",
        dtype={"code": str},
    )

    df["code"] = df["code"].astype(str).str.strip()
    for col in ("num_from", "num_to", "capacity"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "num_from", "num_to"])

    for col in ("operator", "region"):
        df[col] = df[col].astype(str).str.strip()

    code_num = pd.to_numeric(df["code"], errors="coerce")
    df = df[code_num.notna()].copy()
    code_num = code_num[code_num.notna()].astype("int64")

    df["start_key"] = code_num * 10_000_000 + df["num_from"].astype("int64")
    df["end_key"] = code_num * 10_000_000 + df["num_to"].astype("int64")

    if df.empty:
        raise ValueError(
            "Реестр прочитан, но ни одной валидной строки не найдено — "
            "проверьте, что это выписка DEF-9xx, а не другой файл."
        )

    return df.sort_values("start_key").reset_index(drop=True)


def _classify(operator: str, capacity: Optional[float]) -> tuple[str, str]:
    op = (operator or "").lower()
    is_big_four = any(marker in op for marker in BIG_FOUR_MARKERS)
    category = "mno" if is_big_four else "mvno_or_small"

    notes = []
    if not is_big_four:
        notes.append("не входит в большую четвёрку")
    if capacity is not None and not pd.isna(capacity) and capacity < SMALL_RANGE_THRESHOLD:
        notes.append(f"малый диапазон ({int(capacity)} номеров)")
    return category, "; ".join(notes)


def lookup(phone_raw, plan: pd.DataFrame) -> LookupResult:
    """Ищет один номер в реестре."""
    phone = normalize_phone(phone_raw)
    if phone is None:
        return LookupResult(
            phone=str(phone_raw),
            found=False,
            category="invalid",
            note="не похоже на мобильный номер РФ",
        )

    key = int(phone[1:4]) * 10_000_000 + int(phone[4:])
    starts = plan["start_key"].to_numpy()
    idx = int(np.searchsorted(starts, key, side="right")) - 1

    if idx < 0 or key > int(plan["end_key"].iat[idx]):
        return LookupResult(
            phone=phone,
            found=False,
            code=phone[1:4],
            category="unknown_range",
            note="номер не входит ни в один выделенный диапазон",
        )

    row = plan.iloc[idx]
    capacity = row["capacity"]
    category, note = _classify(row["operator"], capacity)

    return LookupResult(
        phone=phone,
        found=True,
        code=row["code"],
        operator=row["operator"],
        region=row["region"],
        capacity=None if pd.isna(capacity) else int(capacity),
        category=category,
        note=note,
    )


def lookup_many(phones, plan: pd.DataFrame) -> pd.DataFrame:
    """Пакетная проверка. Ограничений по количеству нет — всё считается локально."""
    return pd.DataFrame([lookup(p, plan).as_dict() for p in phones])
