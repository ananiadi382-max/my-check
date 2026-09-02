"""
Поиск номера в реестре российской системы и плана нумерации.

Источник данных: https://opendata.digital.gov.ru/registry/numeric/
Файлы: DEF-9xx (мобильные), ABC-3xx и ABC-4xx (городские), ABC-8xx (8-800 и сервисные).
Формат: АВС/DEF;От;До;Емкость;Оператор;Регион

Формат файла определяется автоматически по сигнатуре: поддерживаются
CSV (cp1251 или UTF-8) и XLSX. Разделитель в CSV тоже определяется сам.

Проверка полностью локальная — обращений к внешним API нет.
"""

from __future__ import annotations

import gzip
import io
import os
import re
from dataclasses import dataclass, asdict, field
from typing import Iterable, Optional

import numpy as np
import pandas as pd

VERSION = "2026-09-02.8"

RAW_COLUMNS = ["code", "num_from", "num_to", "capacity", "operator", "region"]

ENCODINGS = ("utf-8-sig", "cp1251", "utf-8", "koi8-r")
SEPARATORS = (";", ",", "\t")

XLSX_SIGNATURE = b"PK\x03\x04"          # xlsx — это zip-архив
XLS_SIGNATURE = b"\xd0\xcf\x11\xe0"     # старый бинарный формат Excel

# Имена файлов реестра, которые ищем в папке data/ (в любом из двух форматов).
REGISTRY_BASENAMES = ("DEF-9xx", "ABC-3xx", "ABC-4xx", "ABC-8xx")
REGISTRY_EXTENSIONS = (".csv.gz", ".xlsx", ".csv")

# Один сжатый файл со всеми четырьмя выписками — его и кладём в репозиторий.
# Создаётся скриптом prepare_registry.py.
PREPARED_NAME = "registry.csv.gz"

# --- Классификация операторов -------------------------------------------------
#
# Списки заполняются подстроками в нижнем регистре — при появлении новых
# операторов достаточно дописать сюда строку, менять код не нужно.

# Большая четвёрка мобильных.
BIG_FOUR_MARKERS = (
    "мтс", "мобильные телесистемы", "мегафон", "вымпел", "билайн",
    "т2 мобайл", "т2мобайл", "теле2", "tele2",
)

# Виртуальные операторы с массовой розницей: юридически MVNO, но абонент
# у них — обычный человек с симкой из салона или банковского приложения.
RETAIL_MVNO_MARKERS = (
    "скартел",              # Yota, дочерняя компания МегаФона
    "т-моб", "тинькофф",    # мобильный оператор Т-Банка
    "сбербанк-телеком", "сбербанк телеком", "сбермобайл", "поговорим",
    "екатеринбург-2000", "мотив",
    "ростелеком", "газпром",
    "волна", "к-телеком", "миранда-медиа",   # Крым
    "таттелеком", "летай", "вайнах телеком",
    "почта россии", "лайфстрим",
)

# Крупные операторы фиксированной связи — для городских номеров.
GEO_MAJOR_MARKERS = BIG_FOUR_MARKERS + (
    "ростелеком", "мгтс", "московская городская телефонная",
    "транстелеком", "эр-телеком", "дом.ру", "комкор", "акадо",
    "башинформсвязь", "таттелеком", "центральный телеграф",
    "обит", "мастертел", "орион телеком", "новотелеком",
)

# Диапазоны меньше этого размера почти всегда принадлежат
# небольшим операторам, корпоративной связи или IP-телефонии.
SMALL_RANGE_THRESHOLD = 10_000

KIND_LABELS = {
    "mobile": "Мобильный",
    "geo": "Городской",
    "toll_free": "8-800",
    "service": "Сервисный",
}

CATEGORY_LABELS = {
    "mno": "Крупный оператор",
    "mvno_retail": "Розничный виртуальный оператор",
    "mvno_other": "Мелкий оператор без розничного бренда",
    "geo_major": "Городской, крупный оператор",
    "geo_small": "Городской, мелкий оператор",
    "toll_free": "Бесплатная линия компании",
    "service": "Сервисный номер с доп. оплатой",
    "unknown_range": "Вне выделенных диапазонов",
    "not_covered": "Реестр не загружен",
    "invalid": "Некорректный номер",
}

RISK_LABELS = {
    "ok": "Обычный",
    "attention": "Присмотреться",
    "suspicious": "Подозрительный",
    "unknown": "Не проверен",
}


@dataclass
class Registry:
    """Объединённый реестр из нескольких файлов плюс информация о покрытии."""
    ranges: pd.DataFrame
    codes: frozenset = field(default_factory=frozenset)
    sources: tuple = ()
    errors: tuple = ()      # [(имя файла, причина)] — что прочитать не удалось

    def __len__(self) -> int:
        return len(self.ranges)

    @property
    def groups(self) -> set:
        """Первые цифры кодов — какие файлы реестра фактически загружены."""
        return {code[0] for code in self.codes}

    def covers(self, code: str) -> bool:
        """Загружен ли файл, в котором вообще может быть этот код."""
        return bool(code) and code[0] in self.groups


@dataclass
class LookupResult:
    phone: str
    found: bool
    kind: str = "mobile"          # mobile | geo | toll_free | service
    code: Optional[str] = None
    operator: Optional[str] = None
    region: Optional[str] = None
    capacity: Optional[int] = None
    category: str = "unknown"
    risk: str = "ok"              # ok | attention | suspicious | unknown
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# --- Разбор номера ------------------------------------------------------------

def parse_phone(raw) -> Optional[tuple[str, str, str]]:
    """
    Возвращает (нормализованный_номер, код, остаток) или None.

    Понимает мобильные и городские: 8 800 ..., +7 495 ..., 8 (912) ...
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    if len(digits) != 11:
        return None
    return digits, digits[1:4], digits[4:]


def normalize_phone(raw) -> Optional[str]:
    parsed = parse_phone(raw)
    return parsed[0] if parsed else None


# Символы, которые могут встречаться внутри записи номера: пробелы (включая
# неразрывный), дефисы всех начертаний, скобки, точки.
_PHONE_CHARS = r"\d\s\u00a0\-\u2010-\u2015\(\)\."
PHONE_RUN_RE = re.compile(rf"[+\d][{_PHONE_CHARS}]{{7,}}\d")


def _split_digits(digits: str) -> list[str]:
    """Разбивает слипшуюся цепочку цифр на отдельные номера."""
    if len(digits) <= 11:
        return [digits]

    chunks, i = [], 0
    while len(digits) - i >= 10:
        take = 11 if digits[i] in "78" and len(digits) - i >= 11 else 10
        chunks.append(digits[i:i + take])
        i += take
    return chunks


def extract_phones(text: str) -> list[str]:
    """
    Вытаскивает номера из произвольного текста.

    Понимает списки в столбик, через запятую, с пробелами и скобками, а также
    ссылки из CRM вида +[7 902 112-93-38](callto://+79021129338) — там номер
    записан дважды, в результат он попадёт один раз.
    """
    seen: set = set()
    found: list[str] = []

    for line in (text or "").splitlines():
        for run in PHONE_RUN_RE.findall(line):
            digits = re.sub(r"\D", "", run)
            for chunk in _split_digits(digits):
                if len(chunk) < 10:
                    continue
                key = normalize_phone(chunk) or chunk
                if key in seen:
                    continue
                seen.add(key)
                found.append(chunk)

    return found


def number_kind(code: str) -> str:
    """
    Тип номера по коду.

    Внимание: на "8" начинаются коды многих городов — 812 Санкт-Петербург,
    843 Казань, 8362 Йошкар-Ола. Сервисные коды только трёхзначные из
    блока 800-809, всё остальное географическое.
    """
    if code.startswith("9"):
        return "mobile"
    if code == "800":
        return "toll_free"
    if len(code) == 3 and code.startswith("80"):
        return "service"
    return "geo"


# --- Чтение файлов ------------------------------------------------------------

GZIP_SIGNATURE = b"\x1f\x8b"


def _read_bytes(source) -> bytes:
    """Читает путь, файловый объект или bytes. Gzip распаковывается прозрачно."""
    if isinstance(source, bytes):
        raw = source
    elif hasattr(source, "read"):
        if hasattr(source, "seek"):
            source.seek(0)
        data = source.read()
        raw = data.encode("utf-8") if isinstance(data, str) else data
    else:
        with open(source, "rb") as fh:
            raw = fh.read()

    if raw.startswith(GZIP_SIGNATURE):
        raw = gzip.decompress(raw)
    return raw


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
    """
    Подбирает разделитель по структуре, а не по количеству символов.

    Считать вхождения нельзя: в выписке ABC-8xx поле территории содержит
    перечисления регионов через запятую, и запятых в файле больше, чем
    точек с запятой. Поэтому берём первый разделитель, при котором КАЖДАЯ
    строка распадается минимум на шесть полей.
    """
    lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
    if not lines:
        return ";"

    for sep in SEPARATORS:
        if min(line.count(sep) + 1 for line in lines) >= 6:
            return sep

    # Ни один не дал шести полей во всех строках — берём тот,
    # что даёт больше всего полей в типичной строке.
    widths = {sep: sorted(line.count(sep) + 1 for line in lines)[len(lines) // 2]
              for sep in SEPARATORS}
    best = max(widths, key=widths.get)
    return best if widths[best] > 1 else ";"


def _looks_like_html(raw: bytes) -> bool:
    head = raw[:2000].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head[:500]


def load_def_plan(source) -> pd.DataFrame:
    """Читает один файл выписки (CSV или XLSX) в DataFrame с ключами start_key/end_key."""
    raw = _read_bytes(source)

    if not raw.strip():
        raise ValueError("Файл пустой.")

    if _looks_like_html(raw):
        raise ValueError(
            "Это HTML-страница, а не выписка из реестра. Скорее всего при скачивании "
            "вернулась страница с ошибкой — файл нужно скачать заново."
        )

    if raw.startswith(XLSX_SIGNATURE) or raw.startswith(XLS_SIGNATURE):
        df = pd.read_excel(io.BytesIO(raw), header=None, dtype=str)
        if df.shape[1] < 6:
            raise ValueError(
                f"В файле только {df.shape[1]} колонок, а нужно минимум 6 "
                "(код, от, до, ёмкость, оператор, регион)."
            )
        df = df.iloc[:, :6]
        df.columns = RAW_COLUMNS
    else:
        text = _decode(raw)
        separator = _detect_separator(text)

        # Шапку не разбираем: она у разных выписок отличается числом полей.
        # Строка заголовка отсеется сама — в ней "От" не превращается в число.
        head_lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
        widest = max((ln.count(separator) + 1 for ln in head_lines), default=0)
        if widest < 6:
            raise ValueError(
                f"В файле только {widest} колонок, а нужно минимум 6. "
                "Похоже, это не выписка из реестра нумерации."
            )

        df = pd.read_csv(
            io.StringIO(text), sep=separator, header=None,
            names=RAW_COLUMNS, usecols=range(6),   # в "Регион" бывают лишние разделители
            on_bad_lines="skip", engine="python", dtype={"code": str},
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

    # Национальный номер всегда 10 цифр: код + абонентский номер.
    # Код бывает трёх- (495), четырёх- (8362) и пятизначным, поэтому
    # ширину абонентской части считаем от длины кода, а не фиксируем.
    width = 10 - df["code"].str.len()
    valid = (width >= 4) & (width <= 8)
    df, code_num, width = df[valid].copy(), code_num[valid], width[valid]

    multiplier = np.power(10, width.to_numpy(), dtype="int64")
    df["start_key"] = code_num.to_numpy() * multiplier + df["num_from"].astype("int64").to_numpy()
    df["end_key"] = code_num.to_numpy() * multiplier + df["num_to"].astype("int64").to_numpy()

    # Отсекаем строки, где абонентская часть не влезает в свою разрядность.
    df = df[(df["start_key"] >= 10**9) & (df["end_key"] < 10**10)]

    if df.empty:
        raise ValueError(
            "Файл прочитан, но ни одной валидной строки не найдено — "
            "проверьте, что это выписка из реестра нумерации."
        )

    return df.sort_values("start_key").reset_index(drop=True)


def find_registry_files(directory: str = "data") -> list[str]:
    """
    Возвращает пути к файлам реестра в папке.

    Если есть подготовленный registry.csv.gz — используется только он.
    Иначе собираем всё, что нашлось из сырых выписок.
    """
    prepared = os.path.join(directory, PREPARED_NAME)
    if os.path.exists(prepared):
        return [prepared]

    found = []
    for base in REGISTRY_BASENAMES:
        for ext in REGISTRY_EXTENSIONS:
            path = os.path.join(directory, base + ext)
            if os.path.exists(path):
                found.append(path)
                break
    return found


def load_registry(directory: str = "data") -> Optional[Registry]:
    """Собирает Registry из папки. Возвращает None, если файлов нет."""
    paths = find_registry_files(directory)
    return build_registry(paths) if paths else None


def build_registry(sources: Iterable) -> Registry:
    """
    Собирает Registry из нескольких файлов (пути, файловые объекты или bytes).

    Битый файл не роняет остальные: он попадает в registry.errors,
    а приложение продолжает работать с тем, что удалось прочитать.
    """
    frames, names, errors = [], [], []
    for source in sources:
        label = os.path.basename(source) if isinstance(source, str) else "загруженный файл"
        try:
            frames.append(load_def_plan(source))
            names.append(label)
        except Exception as exc:
            errors.append((label, str(exc)))

    if not frames:
        details = "; ".join(f"{name} — {msg}" for name, msg in errors)
        raise ValueError(
            "Не удалось прочитать ни один файл реестра. " + (details or "Файлы не переданы.")
        )

    ranges = pd.concat(frames, ignore_index=True).sort_values("start_key").reset_index(drop=True)
    return Registry(
        ranges=ranges,
        codes=frozenset(ranges["code"].unique()),
        sources=tuple(names),
        errors=tuple(errors),
    )


# --- Классификация ------------------------------------------------------------

def _classify_mobile(operator: str, capacity: Optional[float]) -> tuple[str, str, str]:
    op = (operator or "").lower()

    if any(marker in op for marker in BIG_FOUR_MARKERS):
        return "mno", "ok", "Диапазон крупного федерального оператора."

    if any(marker in op for marker in RETAIL_MVNO_MARKERS):
        return "mvno_retail", "ok", "Виртуальный оператор, но с массовой розницей — обычный абонент."

    reason = (
        "Диапазон принадлежит небольшому оператору без розничного бренда. "
        "Такие ёмкости часто уходят под виртуальные номера и IP-телефонию."
    )
    if capacity is not None and not pd.isna(capacity) and capacity < SMALL_RANGE_THRESHOLD:
        reason += f" Диапазон совсем небольшой — {int(capacity)} номеров."
    return "mvno_other", "attention", reason


def _classify_geo(operator: str, capacity: Optional[float]) -> tuple[str, str, str]:
    op = (operator or "").lower()

    if any(marker in op for marker in GEO_MAJOR_MARKERS):
        return "geo_major", "ok", "Городской номер крупного оператора связи."

    reason = (
        "Городской номер небольшого оператора. У таких компаний это обычно "
        "виртуальная АТС или IP-телефония — номер рабочий, но не личный."
    )
    if capacity is not None and not pd.isna(capacity) and capacity < SMALL_RANGE_THRESHOLD:
        reason += f" Диапазон небольшой — {int(capacity)} номеров."
    return "geo_small", "attention", reason


def _classify_service(kind: str) -> tuple[str, str, str]:
    if kind == "toll_free":
        return (
            "toll_free", "attention",
            "Это бесплатная линия 8-800, то есть телефон компании, а не личный "
            "номер человека. Дозвониться до конкретного лида по нему не выйдет.",
        )
    return (
        "service", "suspicious",
        "Сервисный номер с дополнительной оплатой за звонок. В карточке лида "
        "такому номеру взяться неоткуда.",
    )


def lookup(phone_raw, registry: Registry) -> LookupResult:
    """Ищет один номер в реестре."""
    parsed = parse_phone(phone_raw)
    if parsed is None:
        return LookupResult(
            phone=str(phone_raw), found=False, category="invalid", risk="suspicious",
            note="Не похоже на телефонный номер РФ — проверьте, как он записан в карточке лида.",
        )

    phone, prefix, _ = parsed          # prefix — первые три цифры, ещё не код
    national = int(phone[1:])          # 10 цифр: код + абонентский номер

    starts = registry.ranges["start_key"].to_numpy()
    idx = int(np.searchsorted(starts, national, side="right")) - 1
    hit = idx >= 0 and national <= int(registry.ranges["end_key"].iat[idx])

    if hit:
        row = registry.ranges.iloc[idx]
        code = str(row["code"])                 # настоящий код: 495, 8362, ...
        kind = number_kind(code)
        capacity = None if pd.isna(row["capacity"]) else int(row["capacity"])

        if kind in ("toll_free", "service"):
            category, risk, note = _classify_service(kind)
        else:
            classifier = _classify_mobile if kind == "mobile" else _classify_geo
            category, risk, note = classifier(row["operator"], row["capacity"])

        return LookupResult(
            phone=phone, found=True, kind=kind, code=code,
            operator=row["operator"], region=row["region"], capacity=capacity,
            category=category, risk=risk, note=note,
        )

    # Не нашли: код определить не по чему, судим по первым трём цифрам.
    kind = number_kind(prefix)

    if kind in ("toll_free", "service"):
        category, risk, note = _classify_service(kind)
        return LookupResult(
            phone=phone, found=False, kind=kind, code=prefix,
            category=category, risk=risk, note=note,
        )

    if not registry.covers(prefix):
        return LookupResult(
            phone=phone, found=False, kind=kind, code=prefix,
            category="not_covered", risk="unknown",
            note=f"Проверить не удалось: не загружен файл реестра с кодами на {prefix[0]}.",
        )

    return LookupResult(
        phone=phone, found=False, kind=kind, code=prefix,
        category="unknown_range", risk="suspicious",
        note="Номер не входит ни в один диапазон, выделенный операторам. "
             "Скорее всего выдуман или записан с ошибкой.",
    )


def lookup_many(phones, registry: Registry) -> pd.DataFrame:
    """Пакетная проверка. Ограничений по количеству нет — всё считается локально."""
    return pd.DataFrame([lookup(p, registry).as_dict() for p in phones])
