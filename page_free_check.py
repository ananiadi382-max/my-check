"""
Страница "Бесплатная проверка" — сверка номеров с реестром нумерации DEF-9xx.
Подключается вызовом render() из основного app.py.
"""

from __future__ import annotations

import io
import os
import re

import pandas as pd
import streamlit as st

from numbering_plan import load_def_plan, lookup_many

DEFAULT_CSV_PATH = "data/DEF-9xx.csv"

CATEGORY_LABELS = {
    "mno": "Крупный оператор",
    "mvno_or_small": "Виртуальный / мелкий оператор",
    "unknown_range": "Вне выделенных диапазонов",
    "invalid": "Некорректный номер",
}

ROW_COLORS = {
    "mno": "#e8f5e9",
    "mvno_or_small": "#fff8e1",
    "unknown_range": "#ffebee",
    "invalid": "#f5f5f5",
}


@st.cache_data(show_spinner="Загружаю реестр нумерации...")
def _load_plan_cached(path_or_bytes, cache_key: str):
    return load_def_plan(path_or_bytes)


def _extract_phones(text: str) -> list[str]:
    return [chunk for chunk in re.split(r"[\s,;]+", text or "") if chunk.strip()]


def _phones_from_file(uploaded) -> list[str]:
    name = uploaded.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded, dtype=str)
    else:
        df = pd.read_csv(uploaded, dtype=str, sep=None, engine="python")
    if df.empty:
        return []
    column = st.selectbox("Колонка с номерами", options=list(df.columns))
    return df[column].dropna().astype(str).tolist()


def _style_rows(df: pd.DataFrame):
    def color(row):
        return [f"background-color: {ROW_COLORS.get(row['category'], '')}"] * len(row)

    return df.style.apply(color, axis=1)


def render() -> None:
    st.header("Бесплатная проверка по реестру нумерации")
    st.caption(
        "Сверка номера с выпиской из реестра российской системы и плана нумерации "
        "(DEF-9xx, Минцифры). Проверка локальная, лимитов и расходов по API нет."
    )

    # --- источник реестра ---
    plan = None
    if os.path.exists(DEFAULT_CSV_PATH):
        plan = _load_plan_cached(DEFAULT_CSV_PATH, f"file:{os.path.getmtime(DEFAULT_CSV_PATH)}")
        st.success(f"Реестр загружен: {len(plan):,} диапазонов".replace(",", " "))
    else:
        st.warning(f"Файл {DEFAULT_CSV_PATH} не найден в репозитории — загрузите его вручную.")
        registry_file = st.file_uploader("DEF-9xx.csv", type=["csv"], key="registry_upload")
        if registry_file is not None:
            data = registry_file.getvalue()
            plan = _load_plan_cached(io.BytesIO(data), f"upload:{len(data)}")
            st.success(f"Реестр загружен: {len(plan):,} диапазонов".replace(",", " "))

    if plan is None:
        st.stop()

    # --- ввод номеров ---
    mode = st.radio("Источник номеров", ["Список", "Файл"], horizontal=True)
    phones: list[str] = []

    if mode == "Список":
        text = st.text_area(
            "Номера — по одному в строке или через запятую",
            height=140,
            placeholder="79144671546\n79380999135",
        )
        phones = _extract_phones(text)
    else:
        uploaded = st.file_uploader("Excel или CSV", type=["xlsx", "xls", "csv"], key="phones_upload")
        if uploaded is not None:
            phones = _phones_from_file(uploaded)

    if not phones:
        return

    st.write(f"Номеров к проверке: {len(phones)}")

    if not st.button("Проверить по реестру", type="primary"):
        return

    results = lookup_many(phones, plan)
    results["Категория"] = results["category"].map(CATEGORY_LABELS)

    # --- сводка ---
    counts = results["category"].value_counts()
    cols = st.columns(4)
    for col, key in zip(cols, ["mno", "mvno_or_small", "unknown_range", "invalid"]):
        col.metric(CATEGORY_LABELS[key], int(counts.get(key, 0)))

    display = results.rename(
        columns={
            "phone": "Номер",
            "code": "Код",
            "operator": "Оператор по реестру",
            "region": "Регион",
            "capacity": "Ёмкость диапазона",
            "note": "Примечание",
        }
    )[["Номер", "Код", "Оператор по реестру", "Регион", "Ёмкость диапазона", "Категория", "Примечание", "category"]]

    st.dataframe(
        _style_rows(display).hide(axis="columns", subset=["category"]),
        use_container_width=True,
        hide_index=True,
    )

    st.info(
        "Реестр показывает, кому изначально выделен диапазон. Из-за переносимости "
        "номеров (MNP) фактический оператор может отличаться — его даёт только HLR-проверка."
    )

    buffer = io.BytesIO()
    display.drop(columns=["category"]).to_excel(buffer, index=False)
    st.download_button(
        "Скачать результат (Excel)",
        data=buffer.getvalue(),
        file_name="registry_check.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
