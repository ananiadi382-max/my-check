"""
Готовит один сжатый файл реестра для репозитория.

Читает сырые выписки из data/ (DEF-9xx, ABC-3xx, ABC-4xx, ABC-8xx в csv или xlsx)
и складывает их в data/registry.csv.gz — один файл на несколько мегабайт вместо
шестидесяти. Приложение подхватывает его автоматически.

Запуск из папки проекта:
    python3 prepare_registry.py

Сырые CSV после этого в репозиторий класть не нужно.
"""

import gzip
import os
import sys

from numbering_plan import (
    PREPARED_NAME, RAW_COLUMNS, REGISTRY_BASENAMES,
    build_registry, find_registry_files,
)

DATA_DIR = "data"


def human(num_bytes: float) -> str:
    return f"{num_bytes / 1024 / 1024:.1f} МБ"


def main() -> int:
    target = os.path.join(DATA_DIR, PREPARED_NAME)

    raw_paths = [
        p for p in find_registry_files(DATA_DIR)
        if os.path.basename(p) != PREPARED_NAME
    ]
    if not raw_paths:
        print(f"В {DATA_DIR}/ нет сырых выписок. Ожидаются файлы: "
              + ", ".join(REGISTRY_BASENAMES))
        return 1

    print("Читаю:")
    for path in raw_paths:
        print(f"  {path} — {human(os.path.getsize(path))}")

    registry = build_registry(raw_paths)

    for name, reason in registry.errors:
        print(f"  ПРОПУЩЕН {name}: {reason}")

    if registry.errors:
        answer = input("Продолжить без этих файлов? [y/N] ").strip().lower()
        if answer != "y":
            print("Отменено.")
            return 1

    df = registry.ranges[RAW_COLUMNS]

    csv_bytes = df.to_csv(index=False, sep=";", encoding="utf-8").encode("utf-8")
    with gzip.open(target, "wb", compresslevel=9) as fh:
        fh.write(csv_bytes)

    before = sum(os.path.getsize(p) for p in raw_paths)
    after = os.path.getsize(target)

    print(f"\nГотово: {target}")
    print(f"  диапазонов: {len(df):,}".replace(",", " "))
    print(f"  было {human(before)} → стало {human(after)} "
          f"(в {before / after:.0f} раз меньше)")
    print("\nТеперь в репозиторий достаточно положить только этот файл.")
    print("Сырые CSV можно исключить, добавив в .gitignore:")
    print("  data/DEF-9xx.*\n  data/ABC-*.*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
