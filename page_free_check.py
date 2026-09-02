"""
Страница "Бесплатная проверка" — сверка номеров с реестром нумерации.
Подключается вызовом render() из основного app.py.
"""

from __future__ import annotations

import io
import os
import re

import pandas as pd
import streamlit as st

from numbering_plan import (
    CATEGORY_LABELS, KIND_LABELS, PREPARED_NAME, REGISTRY_BASENAMES,
    build_registry, extract_phones, find_registry_files, lookup_many,
)

REGISTRY_DIR = "data"

VERDICTS = {
    "suspicious": "🔴 Подозрительный",
    "attention": "🟠 Присмотреться",
    "ok": "🟢 Обычный",
    "unknown": "⚪ Не проверен",
}

ROW_COLORS = {
    "suspicious": "#ffebee",
    "attention": "#fff8e1",
    "ok": "#e8f5e9",
    "unknown": "#f5f5f5",
}

RISK_ORDER = {"suspicious": 0, "attention": 1, "unknown": 2, "ok": 3}


@st.cache_data(show_spinner="Загружаю реестр нумерации...")
def _load_registry_cached(paths: tuple, cache_key: str):
    return build_registry(paths)


@st.cache_data(show_spinner="Загружаю реестр нумерации...")
def _load_uploaded_cached(payloads: tuple, cache_key: str):
    return build_registry(payloads)


def _phones_from_file(uploaded) -> list[str]:
    name = uploaded.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded, dtype=str)
    else:
        df = pd.read_csv(uploaded, dtype=str, sep=None, engine="python")
    if df.empty:
        return []
    column = st.selectbox("Колонка с номерами", options=list(df.columns))
    cells = df[column].dropna().astype(str)
    return extract_phones("\n".join(cells))


def build_display(results: pd.DataFrame) -> pd.DataFrame:
    """Готовит таблицу для сотрудника: вердикт первой колонкой, техника убрана."""
    df = results.copy()
    df["Вердикт"] = df["risk"].map(VERDICTS)
    df["Номер"] = df["phone"]
    df["Тип"] = df["kind"].map(KIND_LABELS)
    df["Что это значит"] = df["note"]
    df["Оператор по реестру"] = df["operator"].fillna("—")
    df["Регион"] = df["region"].fillna("—").replace("-", "—")
    df["Категория"] = df["category"].map(CATEGORY_LABELS)

    df["_order"] = df["risk"].map(RISK_ORDER)
    df = df.sort_values(["_order", "Номер"]).reset_index(drop=True)

    return df[
        ["Вердикт", "Номер", "Тип", "Что это значит",
         "Оператор по реестру", "Регион", "Категория", "risk"]
    ]


def _style(display: pd.DataFrame):
    def color(row):
        return [f"background-color: {ROW_COLORS.get(row['risk'], '')}"] * len(row)

    return display.style.apply(color, axis=1).hide(axis="columns", subset=["risk"])


def _report_gaps(registry, paths) -> None:
    """Показывает, какие файлы не прочитались и каких не хватает."""
    for name, reason in registry.errors:
        st.error(f"Файл {name} прочитать не удалось: {reason}")

    if paths and PREPARED_NAME not in registry.sources:
        ok = set(registry.sources)
        missing = [
            base for base in REGISTRY_BASENAMES
            if not any(p.startswith(base) for p in ok)
        ]
        if missing:
            st.warning(
                "Нет данных по файлам: " + ", ".join(missing) + ". "
                "Номера с этими кодами получат вердикт «Не проверен». "
                "Скачать заново: opendata.digital.gov.ru/registry/numeric/"
            )


def _registry_section():
    """Загружает реестр из data/ или через форму. Возвращает Registry или None."""
    paths = find_registry_files(REGISTRY_DIR)

    if paths:
        key = "|".join(f"{p}:{os.path.getmtime(p)}" for p in paths)
        try:
            registry = _load_registry_cached(tuple(paths), key)
        except ValueError as exc:
            st.error(str(exc))
            return None

        st.success(
            f"Загружено {len(registry):,} диапазонов из файлов: "
            f"{', '.join(registry.sources)}".replace(",", " ", 1)
        )
        _report_gaps(registry, paths)
        return registry

    st.warning(
        f"В папке {REGISTRY_DIR}/ нет файлов реестра. Положите туда либо "
        f"подготовленный {PREPARED_NAME}, либо сырые выписки DEF-9xx, "
        "ABC-3xx, ABC-4xx и ABC-8xx — либо загрузите их вручную."
    )
    uploads = st.file_uploader(
        "Файлы реестра", type=["xlsx", "xls", "csv"],
        accept_multiple_files=True, key="registry_upload",
    )
    if not uploads:
        return None

    payloads = tuple(u.getvalue() for u in uploads)
    try:
        registry = _load_uploaded_cached(payloads, f"upload:{sum(len(p) for p in payloads)}")
    except ValueError as exc:
        st.error(str(exc))
        return None

    st.success(f"Загружено {len(registry):,} диапазонов".replace(",", " "))
    _report_gaps(registry, [])
    return registry


def render() -> None:
    st.header("Бесплатная проверка по реестру нумерации")
    st.caption(
        "Сверка номера с выпиской из реестра российской системы и плана нумерации "
        "(Минцифры). Работает и для мобильных, и для городских номеров. "
        "Проверка локальная, лимитов и расходов по API нет."
    )

    registry = _registry_section()
    if registry is None:
        st.stop()

    with st.expander("Как читать вердикт"):
        st.markdown(
            "**🔴 Подозрительный** — номер не входит ни в один выделенный оператору "
            "диапазон, записан некорректно или это платный сервисный номер. "
            "Скорее всего выдуман.\n\n"
            "**🟠 Присмотреться** — либо диапазон мелкого оператора без розничного "
            "бренда (часто виртуальные номера и IP-телефония), либо номер 8-800 и "
            "городской номер небольшой компании: рабочий телефон, но не личный. "
            "Сам по себе не приговор — смотрите вместе с тем, берут ли трубку и "
            "есть ли аккаунт в мессенджере.\n\n"
            "**🟢 Обычный** — диапазон крупного оператора или виртуального "
            "оператора с массовой розницей (Yota, Т-Мобайл, СберМобайл и подобные).\n\n"
            "**⚪ Не проверен** — не загружен файл реестра с таким кодом. "
            "Это не признак проблемы с номером.\n\n"
            "Проверка показывает, кому выделен диапазон. Существует ли SIM и "
            "включена ли она — это отдельная HLR-проверка, и она работает только "
            "для мобильных."
        )

    # --- ввод номеров ---
    mode = st.radio("Источник номеров", ["Список", "Файл"], horizontal=True)
    phones: list[str] = []

    if mode == "Список":
        text = st.text_area(
            "Номера — в столбик, через запятую или как скопировалось из CRM",
            height=140,
            placeholder="79144671546\n8 (495) 480-96-30\n+[7 902 112-93-38](callto://+79021129338)",
        )
        phones = extract_phones(text)
        if text.strip() and not phones:
            st.warning("В тексте не нашлось ни одного номера — проверьте, что скопировалось.")
    else:
        uploaded = st.file_uploader(
            "Excel или CSV", type=["xlsx", "xls", "csv"], key="phones_upload"
        )
        if uploaded is not None:
            phones = _phones_from_file(uploaded)

    if not phones:
        return

    st.write(f"Распознано номеров: {len(phones)} (повторы отброшены)")

    if not st.button("Проверить по реестру", type="primary"):
        return

    results = lookup_many(phones, registry)
    display = build_display(results)

    counts = results["risk"].value_counts()
    flagged = int(counts.get("suspicious", 0)) + int(counts.get("attention", 0))

    cols = st.columns(4)
    cols[0].metric("🔴 Подозрительные", int(counts.get("suspicious", 0)))
    cols[1].metric("🟠 Присмотреться", int(counts.get("attention", 0)))
    cols[2].metric("🟢 Обычные", int(counts.get("ok", 0)))
    cols[3].metric("⚪ Не проверены", int(counts.get("unknown", 0)))

    if flagged:
        st.warning(f"Требуют внимания: {flagged} из {len(results)}. Они наверху таблицы.")
    else:
        st.success("Все номера из диапазонов крупных или розничных операторов.")

    only_flagged = st.checkbox("Показать только требующие внимания")
    table = display[display["risk"].isin(["suspicious", "attention"])] if only_flagged else display

    st.dataframe(_style(table), use_container_width=True, hide_index=True)

    st.info(
        "Реестр показывает, кому изначально выделен диапазон. Из-за переносимости "
        "номеров фактический оператор может отличаться — его даёт только HLR-проверка."
    )

    buffer = io.BytesIO()
    display.drop(columns=["risk"]).to_excel(buffer, index=False)
    st.download_button(
        "Скачать результат (Excel)",
        data=buffer.getvalue(),
        file_name="registry_check.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
