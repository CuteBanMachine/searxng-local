"""Демонстрация searxng-local.

Первый запуск скачает и установит SearXNG в ./searxng-local
(нужны git в PATH и интернет; несколько минут).
Дальнейшие запуски используют готовую установку.

Запуск из корня пакета:
    python examples/demo.py
"""

from __future__ import annotations

import asyncio

from searxng import SearXNG


async def run(instance: SearXNG) -> None:
    data = await instance.search(
        "python 3.11 what's new",
    )
    results = data.get("results", [])
    print(f"Найдено результатов: {len(results)}")
    for item in results[:5]:
        print("-", item.get("title"))
        print(" ", item.get("url"))


def main() -> None:
    # with: запуск процесса при входе, гарантированная остановка при выходе
    with SearXNG() as sx:
        print(f"SearXNG: {sx!r}")
        asyncio.run(run(sx))


if __name__ == "__main__":
    main()
