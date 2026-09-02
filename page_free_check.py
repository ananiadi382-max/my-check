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
    CATEGORY_LABELS, KIND_LABELS, REGISTRY_BASENAMES,
    build_registry, find_registry_files, lookup_many,
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


def _registry_section():
    """Загружает реестр из data/ или через форму. Возвращает Registry или None."""
    paths = find_registry_files(REGISTRY_DIR)

    if paths:
        key = "|".join(f"{p}:{os.path.getmtime(p)}" for p in paths)
        registry = _load_registry_cached(tuple(paths), key)
        loaded = ", ".join(os.path.basename(p) for p in paths)
        st.success(
            f"Загружено {len(registry):,} диапазонов из файлов: {loaded}".replace(",", " ")
        )
        missing = [b for b in REGISTRY_BASENAMES
                   if not any(os.path.basename(p).startswith(b) for p in paths)]
        if missing:
            st.warning(
                "Не найдены файлы: " + ", ".join(missing) + ". "
                "Номера с этими кодами получат вердикт «Не проверен». "
                "Скачать можно на opendata.digital.gov.ru/registry/numeric/"
            )
        return registry

    st.warning(
        f"В папке {REGISTRY_DIR}/ нет файлов реестра. Положите туда "
        "DEF-9xx (мобильные), ABC-3xx и ABC-4xx (городские), ABC-8xx (8-800) "
        "в формате csv или xlsx — либо загрузите вручную."
    )
    uploads = st.file_uploader(
        "Файлы реестра", type=["xlsx", "xls", "csv"],
        accept_multiple_files=True, key="registry_upload",
    )
    if not uploads:
        return None

    payloads = tuple(u.getvalue() for u in uploads)
    registry = _load_uploaded_cached(payloads, f"upload:{sum(len(p) for p in payloads)}")
    st.success(f"Загружено {len(registry):,} диапазонов".replace(",", " "))
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
            "Номера — по одному в строке или через запятую",
            height=140,
            placeholder="79144671546\n84954809630",
        )
        phones = _extract_phones(text)
    else:
        uploaded = st.file_uploader(
            "Excel или CSV", type=["xlsx", "xls", "csv"], key="phones_upload"
        )
        if uploaded is not None:
            phones = _phones_from_file(uploaded)

    if not phones:
        return

    st.write(f"Номеров к проверке: {len(phones)}")

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
