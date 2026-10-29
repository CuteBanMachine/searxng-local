"""Класс :class:`SearXNG`: жизненный цикл локального процесса и асинхронный поиск."""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from . import installer
from .exceptions import (
    ConfigMismatchError,
    NotInstalledError,
    PortInUseError,
    ProcessCrashedError,
    SearchError,
    SearXNGError,
    StartTimeoutError,
    UnsupportedPlatformError,
)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PORT_RE = re.compile(r"(?m)^\s*port:\s*(\d+)\s*(?:#.*)?$")

# здоровье проверяем в обход любых системных прокси
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_environment() -> None:
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"searxng-local поддерживает только Windows (получено: {sys.platform})."
        )
    if sys.version_info < (3, 11):
        raise UnsupportedPlatformError("Требуется Python 3.11+.")


def _parse_port(text: str) -> int | None:
    """Достаёт server.port из текста settings.yml (first match, без PyYAML)."""
    match = _PORT_RE.search(text)
    return int(match.group(1)) if match else None


def _probe_http(url: str, timeout: float = 2.0) -> bool:
    """``True``, если сервер уже отвечает (любой HTTP-статус < 500)."""
    try:
        with _NO_PROXY_OPENER.open(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        return True  # сервер ответил — значит, поднялся
    except (urllib.error.URLError, OSError):
        return False


def _join_list(value: str | Sequence[str]) -> str:
    return value if isinstance(value, str) else ",".join(str(item) for item in value)


class SearXNG:
    """Локальный SearXNG: автоустановка, запуск процесса, асинхронный поиск.

    Синхронный запуск (процесс + ожидание готовности) и async-поиск::

        with SearXNG() as sx:                      # запуск процесса
            data = asyncio.run(sx.search("python"))

        async with SearXNG() as sx:                # то же, полностью async-контекст
            data = await sx.search("python")

    При выходе из контекста, ``stop()``/``aclose()``, ``atexit`` или сборке
    мусора процесс SearXNG останавливается. Единственное исключение — жёсткое
    убийство родительского процесса (``taskkill /F``): тогда дочерний процесс
    может остаться висеть (см. README, раздел «Ограничения»).

    Параметры конструктора:

        install_dir:     каталог установки (по умолчанию ``<cwd>/searxng-local``).
        port:            порт; фиксируется в settings.yml при первой установке
                         (по умолчанию 8888). Несовпадение с существующей
                         установкой -> ``ConfigMismatchError``.
        settings_path:   свой settings.yml вместо сгенерированного; порт тогда
                         читается из этого файла (параметр ``port`` игнорируется).
        start_timeout:   сколько секунд ждать готовности SearXNG (по умолчанию 60).
        auto_install:    разрешить автоматическую установку при её отсутствии
                         (по умолчанию True; иначе ``NotInstalledError``).
    """

    def __init__(
        self,
        install_dir: str | Path | None = None,
        *,
        port: int = 8888,
        settings_path: str | Path | None = None,
        start_timeout: float = 60.0,
        auto_install: bool = True,
    ) -> None:
        _check_environment()
        self._install_dir = (
            Path(install_dir).resolve()
            if install_dir is not None
            else Path.cwd() / "searxng-local"
        )
        self._requested_port = int(port)
        self._custom_settings = (
            Path(settings_path).resolve() if settings_path is not None else None
        )
        self._start_timeout = float(start_timeout)
        self._proc: subprocess.Popen | None = None
        self._client: httpx.AsyncClient | None = None
        self._log_handles: list[Any] = []
        self._closed = False
        self._port = self._requested_port

        # --- установка (или «ремонт» частичной) ---
        needs_install = installer.read_marker(
            self._install_dir
        ) is None or not installer.is_checkout_complete(
            self._install_dir / installer.REPO_DIR_NAME
        )
        if needs_install:
            if not auto_install:
                raise NotInstalledError(
                    f"SearXNG не установлен в {self._install_dir}. Запустите "
                    "`python -m searxng_local install` или передайте auto_install=True."
                )
            installer.install(self._install_dir, self._requested_port)

        # --- настройки и порт ---
        self._settings_file = self._custom_settings or (
            self._install_dir / installer.SETTINGS_NAME
        )
        if not self._settings_file.is_file():
            raise NotInstalledError(f"Файл настроек не найден: {self._settings_file}")
        parsed_port = _parse_port(self._settings_file.read_text(encoding="utf-8"))
        if self._custom_settings is not None:
            if parsed_port is not None:
                self._port = parsed_port
        elif parsed_port is not None and parsed_port != self._requested_port:
            raise ConfigMismatchError(
                f"Установка в {self._install_dir} использует порт {parsed_port}, "
                f"запрошен {self._requested_port}. Создайте SearXNG(port={parsed_port}) "
                f"или укажите другой install_dir."
            )

        if installer.port_in_use(self._port):
            raise PortInUseError(
                f"127.0.0.1:{self._port} уже занят. Найдите процесс: "
                f"`netstat -ano | findstr :{self._port}` и остановите его, "
                f"либо используйте другой порт (новая установка)."
            )

        # --- запуск и ожидание готовности ---
        self._spawn()
        try:
            self._wait_ready_sync()
        except BaseException:
            self._terminate_process()
            raise
        atexit.register(self._atexit_stop)

    # ------------------------------------------------------------------
    # Публичное API
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Базовый URL инстанса, например ``http://127.0.0.1:8888``."""
        return f"http://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        """Фактический порт (из settings.yml)."""
        return self._port

    @property
    def install_dir(self) -> Path:
        """Каталог установки SearXNG."""
        return self._install_dir

    @property
    def pid(self) -> int | None:
        """PID процесса SearXNG, если им владеет этот инстанс."""
        return self._proc.pid if self._proc is not None else None

    @property
    def is_running(self) -> bool:
        """Жив ли запущенный этим инстансом процесс."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def log_path(self) -> Path:
        """Файл stdout-лога процесса SearXNG."""
        return self._install_dir / installer.LOGS_DIR_NAME / "searxng.log"

    @property
    def err_log_path(self) -> Path:
        """Файл stderr-лога процесса SearXNG."""
        return self._install_dir / installer.LOGS_DIR_NAME / "searxng.err.log"

    async def search(
        self,
        query: str,
        *,
        categories: str | Sequence[str] | None = None,
        engines: str | Sequence[str] | None = None,
        language: str | None = None,
        pageno: int = 1,
        safesearch: int | None = None,
        time_range: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Поиск через ``GET /search?format=json``; возвращает ответ как dict.

        Аргументы пробрасываются в query-string SearXNG:

            query:       текст запроса (обязателен);
            categories:  категория или список, напр. ``"general"``, ``["it", "news"]``;
            engines:     движок или список, напр. ``"bing"``, ``["google", "qwant"]``;
            language:    код языка/региона, напр. ``"ru-RU"``;
            pageno:      номер страницы результатов (по умолчанию 1);
            safesearch:  0/1/2;
            time_range:  ``day`` / ``week`` / ``month`` / ``year``;
            timeout:     таймаут HTTP-запроса, сек (по умолчанию 30).

        Ошибки сети/HTTP оборачиваются в :class:`SearchError`.
        """
        params: dict[str, Any] = {"q": query, "format": "json", "pageno": int(pageno)}
        if categories is not None:
            params["categories"] = _join_list(categories)
        if engines is not None:
            params["engines"] = _join_list(engines)
        if language is not None:
            params["language"] = language
        if safesearch is not None:
            params["safesearch"] = int(safesearch)
        if time_range is not None:
            params["time_range"] = time_range

        client = self._ensure_client()
        try:
            resp = await client.get("/search", params=params, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SearchError(f"Запрос поиска не удался: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise SearchError("SearXNG вернул некорректный JSON.") from exc
        if not isinstance(data, dict):
            raise SearchError("SearXNG вернул JSON не-объект.")
        return data

    async def is_ready(self) -> bool:
        """Отвечает ли инстанс уже сейчас (``GET /healthz``)."""
        try:
            client = self._ensure_client()
        except SearXNGError:
            return False
        try:
            resp = await client.get("/healthz", timeout=2.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def wait_ready(self, timeout: float | None = None) -> None:
        """Async-вариант ожидания готовности (после конструктора избыточен)."""
        timeout_s = self._start_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ProcessCrashedError(
                    f"SearXNG завершился (код {self._proc.returncode}). "
                    f"Логи: {self.err_log_path}"
                )
            if await self.is_ready():
                return
            await asyncio.sleep(0.25)
        raise StartTimeoutError(
            f"SearXNG не ответил за {timeout_s:.0f} c. "
            f"Логи: {self.log_path}, {self.err_log_path}"
        )

    def stop(self) -> None:
        """Останавливает процесс и закрывает HTTP-клиент. Безопасен повторно."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self._atexit_stop)
        self._close_client_sync()
        self._terminate_process()

    async def aclose(self) -> None:
        """Async-остановка: то же, что :meth:`stop`, но клиент закрывается корректно."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self._atexit_stop)
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()
        self._terminate_process()

    # ------------------------------------------------------------------
    # Контекстные менеджеры и деструктор
    # ------------------------------------------------------------------

    def __enter__(self) -> SearXNG:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    async def __aenter__(self) -> SearXNG:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _atexit_stop(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 — atexit не должен падать
            pass

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 — деструктор не должен падать
            pass

    def __repr__(self) -> str:
        state = "running" if self.is_running else "stopped"
        return f"<SearXNG {self.base_url} [{state}] install_dir={str(self._install_dir)!r}>"

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise SearXNGError("Инстанс уже остановлен; создайте новый SearXNG().")
        if self._client is None:
            # trust_env=False: локальный трафик не должен уходить в системный прокси
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=30.0, trust_env=False
            )
        return self._client

    def _spawn(self) -> None:
        repo_dir = self._install_dir / installer.REPO_DIR_NAME
        python = sys.executable
        if not (repo_dir / "searx" / "webapp.py").is_file():
            raise NotInstalledError(
                f"Установка в {self._install_dir} неполна (нет venv или searx/webapp.py). "
                "Удалите каталог установки — при следующем запуске она пройдёт заново."
            )
        logs_dir = self._install_dir / installer.LOGS_DIR_NAME
        logs_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SEARXNG_SETTINGS_PATH"] = str(self._settings_file)
        env["PYTHONPATH"] = str(repo_dir) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"

        out = open(logs_dir / "searxng.log", "ab")
        err = open(logs_dir / "searxng.err.log", "ab")
        self._log_handles = [out, err]
        try:
            self._proc = subprocess.Popen(
                [str(python), "searx/webapp.py"],
                cwd=str(repo_dir),
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
        except BaseException:
            self._close_log_handles()
            raise

    def _wait_ready_sync(self) -> None:
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ProcessCrashedError(
                    f"SearXNG завершился при старте (код {self._proc.returncode}). "
                    f"Логи: {self.err_log_path}"
                )
            if _probe_http(f"{self.base_url}/healthz"):
                return
            time.sleep(0.25)
        raise StartTimeoutError(
            f"SearXNG не ответил за {self._start_timeout:.0f} c. "
            f"Логи: {self.log_path}, {self.err_log_path}"
        )

    def _close_client_sync(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(client.aclose())
        else:
            try:
                asyncio.run(client.aclose())
            except Exception:  # noqa: BLE001 — лучшее усилие
                pass

    def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self._close_log_handles()

    def _close_log_handles(self) -> None:
        for handle in self._log_handles:
            try:
                handle.close()
            except OSError:
                pass
        self._log_handles = []
