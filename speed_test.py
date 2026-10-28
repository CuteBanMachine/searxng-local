"""Тест производительности локального SearXNG.
Проверяет, сколько одновременных запросов может обработать инстанс
до появления ошибок или значительного замедления.
"""

from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

from searxng import SearXNG, exceptions


class RequestResult(NamedTuple):
    success: bool
    duration: float
    error: str | None = None


async def perform_search(instance: SearXNG, query: str) -> RequestResult:
    """Выполняет один поиск и замеряет время."""
    start = time.perf_counter()
    try:
        # Используем короткий таймаут, чтобы тест не висел вечно при перегрузке
        await instance.search(query, timeout=15.0)
        return RequestResult(True, time.perf_counter() - start)
    except exceptions.SearchError as e:
        return RequestResult(False, time.perf_counter() - start, str(e))
    except Exception as e:
        return RequestResult(
            False, time.perf_counter() - start, f"Unexpected error: {type(e).__name__}"
        )


async def test_concurrency(
    instance: SearXNG, queries: list[str], concurrency: int
) -> tuple[float, float, float]:
    """Запускает N одновременных запросов и возвращает статистику."""

    tasks = [perform_search(instance, query) for query in queries]

    start_batch = time.perf_counter()
    results = await asyncio.gather(*tasks)
    total_duration = time.perf_counter() - start_batch

    successes = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    avg_latency = (
        sum(r.duration for r in successes) / len(successes) if successes else 0.0
    )
    success_rate = len(successes) / concurrency

    return total_duration, avg_latency, success_rate


async def main_test():
    # Расширенный диапазон для поиска точки перегиба
    concurrency_levels = [10, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200]
    iterations = 3

    print(
        f"{'Concurrency':<15} | {'Avg Total':<12} | {'Avg Latency':<15} | {'RPS':<10} | {'Success':<10}"
    )
    print("-" * 70)

    try:
        with SearXNG() as sx:
            print(f"Testing instance: {sx.base_url}")
            print(f"Iterations per level: {iterations}\n")

            for count in concurrency_levels:
                total_times = []
                latencies = []
                rates = []

                for i in range(iterations):
                    # Разные запросы для каждой итерации и каждого потока
                    queries = [f"perf_test_{count}_{i}_{j}" for j in range(count)]
                    t, l, r = await test_concurrency(sx, queries, count)
                    total_times.append(t)
                    latencies.append(l)
                    rates.append(r)

                avg_total = sum(total_times) / iterations
                avg_latency = sum(latencies) / iterations
                avg_rate = sum(rates) / iterations
                rps = count / avg_total if avg_total > 0 else 0

                print(
                    f"{count:<15} | {avg_total:<12.2f}s | {avg_latency:<15.2f}s | {rps:<10.2f} | {avg_rate:<10.1%}"
                )

                if avg_rate < 0.95:
                    print(f"\n[!] Критическое падение успеха при {count} потоках.")
                    break
    except Exception as e:
        print(f"Critical error: {e}")
    except Exception as e:
        print(f"Critical error: {e}")


if __name__ == "__main__":
    asyncio.run(main_test())
