"""Иерархия исключений searxng-local."""


class SearXNGError(Exception):
    """Базовый класс всех ошибок пакета."""


class UnsupportedPlatformError(SearXNGError):
    """Платформа или версия Python не поддерживаются (нужны Windows и Python 3.11+)."""


class GitNotFoundError(SearXNGError):
    """git не найден в PATH."""


class InstallError(SearXNGError):
    """Сбой на одном из шагов установки SearXNG."""


class NotInstalledError(SearXNGError):
    """SearXNG не установлен (или установка неполна) в указанном каталоге."""


class ConfigMismatchError(SearXNGError):
    """Параметры запуска противоречат настройкам существующей установки."""


class PortInUseError(SearXNGError):
    """Порт уже занят другим процессом."""


class StartTimeoutError(SearXNGError):
    """SearXNG не ответил за отведённое время."""


class ProcessCrashedError(SearXNGError):
    """Процесс SearXNG аварийно завершился."""


class SearchError(SearXNGError):
    """HTTP-запрос поиска завершился неудачей."""
