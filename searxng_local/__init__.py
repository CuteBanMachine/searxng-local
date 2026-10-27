"""searxng-local — встраиваемый локальный SearXNG (Windows 10+, Python 3.11+).

Обёртка над собственно установленным upstream SearXNG (без Docker):
автоустановка в каталог проекта, запуск процесса в конструкторе,
асинхронный поиск через JSON API и гарантированная остановка процесса
при выходе (stop / with / async with / atexit / __del__).
"""

from .core import SearXNG
from .exceptions import (
    ConfigMismatchError,
    GitNotFoundError,
    InstallError,
    NotInstalledError,
    PortInUseError,
    ProcessCrashedError,
    SearchError,
    SearXNGError,
    StartTimeoutError,
    UnsupportedPlatformError,
)
from .installer import install as install
from .installer import read_marker
from .installer import update as update

__version__ = "0.1.0"

__all__ = [
    "SearXNG",
    "SearXNGError",
    "UnsupportedPlatformError",
    "GitNotFoundError",
    "InstallError",
    "NotInstalledError",
    "ConfigMismatchError",
    "PortInUseError",
    "StartTimeoutError",
    "ProcessCrashedError",
    "SearchError",
    "install",
    "update",
    "read_marker",
    "__version__",
]
