"""Установка, обновление и диагностика локального SearXNG.

Воспроизводит установку в духе репозитория Kerberos255/searxng-windows,
но независимо от него:

1. ``git clone --depth 1`` upstream SearXNG -> ``<install_dir>/repo``;
2. venv от текущего интерпретатора -> ``<install_dir>/.venv``;
3. ``pip install -r requirements.txt`` + ``pip install -e .``;
4. Windows-патч ``searx/valkeydb.py`` (опциональный ``import pwd``);
5. генерация ``settings.yml`` со случайным ``secret_key`` и заданным портом.

Все шаги идемпотентны: завершённые пропускаются, поэтому ``install()``
можно вызывать повторно для «ремонта» частичной установки.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .exceptions import (
    GitNotFoundError,
    InstallError,
    NotInstalledError,
    UnsupportedPlatformError,
)

SEARXNG_REPO_URL = "https://github.com/searxng/searxng.git"

#: Минимальная версия Python для venv SearXNG: upstream импортирует stdlib-модуль
#: ``tomllib``, которого нет в 3.10 и старше.
MINIMUM_PYTHON = (3, 11)

REPO_DIR_NAME = "repo"
VENV_DIR_NAME = ".venv"
SETTINGS_NAME = "settings.yml"
LOGS_DIR_NAME = "logs"
MARKER_NAME = ".searxng-local.json"

MARKER_VERSION = 1

_TEMPLATE_PATH = Path(__file__).with_name("settings.template.yml")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Windows-патч searx/valkeydb.py: модуль pwd существует только на Unix.
# Эквивалент scripts/apply-windows-patch.ps1 из Kerberos255/searxng-windows.
# ---------------------------------------------------------------------------

_PWD_IMPORT_RE = re.compile(r"(?m)^import pwd\s*$")
_PWD_IMPORT_REPLACEMENT = "try:\n    import pwd\nexcept ImportError:\n    pwd = None\n"

_PWD_BLOCK_RE = re.compile(
    r"(?P<indent>[ \t]+)_CLIENT = None[ \t]*\n"
    r"[ \t]+_pw = pwd\.getpwuid\(os\.getuid\(\)\)[ \t]*\n"
    r"[ \t]+logger\.exception\([^\n]*\)[ \t]*\n"
)


def _pwd_block_guard(match: re.Match[str]) -> str:
    indent = match.group("indent")
    return (
        f"{indent}_CLIENT = None\n"
        f"{indent}if pwd is not None:\n"
        f"{indent}    _pw = pwd.getpwuid(os.getuid())\n"
        f"{indent}    user_name, user_id = _pw.pw_name, _pw.pw_uid\n"
        f"{indent}else:\n"
        f"{indent}    user_name, user_id = os.environ.get('USERNAME', 'unknown'), os.getpid()\n"
        f"{indent}logger.exception(\"[%s (%s)] can't connect valkey DB ...\", user_name, user_id)\n"
    )


def apply_windows_patch(repo_dir: Path) -> bool:
    """Делает ``import pwd`` опциональным в ``searx/valkeydb.py``.

    Идемпотентно: на уже пропатченном файле ничего не делает.
    Возвращает ``True``, если файл был изменён.
    """
    target = repo_dir / "searx" / "valkeydb.py"
    if not target.is_file():
        raise InstallError(
            f"Не найден {target}: похоже, структура upstream SearXNG изменилась."
        )
    original = target.read_text(encoding="utf-8")
    text = _PWD_IMPORT_RE.sub(_PWD_IMPORT_REPLACEMENT, original)
    text = _PWD_BLOCK_RE.sub(_pwd_block_guard, text)

    if _PWD_IMPORT_RE.search(text):
        raise InstallError("Патч valkeydb.py не применился: остался голый 'import pwd'.")
    if "_pw = pwd.getpwuid" in text and "if pwd is not None:" not in text:
        raise InstallError(
            "Патч valkeydb.py не применился: блок pwd.getpwuid остался без защиты "
            "(upstream изменился?)."
        )

    if text != original:
        target.write_text(text, encoding="utf-8", newline="\n")
        return True
    return False


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def is_git_repo(repo_dir: Path | str) -> bool:
    """Признак завершённого клона: .git/HEAD существует.

    Наличия одного каталога .git недостаточно: после сбоя клонирования
    (или неудачного удаления) может остаться .git/objects без HEAD.
    """
    return (Path(repo_dir) / ".git" / "HEAD").is_file()


def is_checkout_complete(repo_dir: Path | str) -> bool:
    """Клон доведён до конца: репозиторий есть и рабочее дерево выложено.

    Проверяется индикаторный файл searx/webapp.py: после --no-checkout-клона
    .git/HEAD существует уже до выкладки дерева, поэтому одного is_git_repo
    для решения «клонить заново или нет» недостаточно.
    """
    repo_dir = Path(repo_dir)
    return is_git_repo(repo_dir) and (repo_dir / "searx" / "webapp.py").is_file()


def _rmtree_on_error(func, target, _exc) -> None:  # pragma: no cover - зависит от ОС
    """Обработчик rmtree: снимает read-only атрибут и повторяет удаление.

    git помечает pack-файлы в .git/objects только-for-reading; обычный
    shutil.rmtree на Windows такие файлы не удаляет.
    Совместим с onerror (3.11) и onexc (3.12+).
    """
    try:
        os.chmod(target, stat.S_IWRITE)
        func(target)
    except OSError:
        pass  # окончательное решение примет цикл ретраев в _rmtree_strict


def _rmtree_force(path: Path) -> None:
    kwargs = (
        {"onexc": _rmtree_on_error} if sys.version_info >= (3, 12) else {"onerror": _rmtree_on_error}
    )
    shutil.rmtree(path, **kwargs)


def _rmtree_strict(path: Path, *, retries: int = 3, delay: float = 1.0) -> None:
    """Удаляет каталог (read-only включительно); после ретраев — InstallError.

    Сразу после смерти git-процесса файлы клона могут оставаться залоченными,
    поэтому ошибку удаления нужно отличать от успеха и ретраить.
    """
    for _ in range(retries):
        try:
            _rmtree_force(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay)
    raise InstallError(
        f"Не удалось удалить каталог {path} (файлы заняты другим процессом?). "
        "Удалите его вручную и повторите установку."
    )


def venv_python(install_dir: Path | str) -> Path:
    """Путь к python.exe виртуального окружения установки."""
    return Path(install_dir) / VENV_DIR_NAME / "Scripts" / "python.exe"


def read_marker(install_dir: Path | str) -> dict[str, Any] | None:
    """Читает маркер установки (None — установка отсутствует/повреждена)."""
    marker = Path(install_dir) / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """``True``, если на ``host:port`` уже кто-то слушает."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


_WIN_FORBIDDEN_CHARS = set('<>:"|?*')
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _windows_safe_relpath(path: str) -> bool:
    """Допустим ли git-путь как относительный путь в NTFS (проверка каждого сегмента)."""
    for segment in path.split("/"):
        if not segment:
            return False
        if any(char in _WIN_FORBIDDEN_CHARS for char in segment):
            return False
        if segment.endswith((" ", ".")):
            return False
        if segment.split(".")[0].upper() in _WIN_RESERVED_NAMES:
            return False
    return True


def _clone_windows_safe(git: str, repo_dir: Path) -> None:
    """Клонирует upstream и выкладывает рабочее дерево без невалидных путей.

    В дереве upstream SearXNG встречаются имена вроде
    ``utils/templates/etc/httpd/sites-available/searxng.conf:socket``:
    двоеточие в имени запрещено в NTFS, и обычный ``git clone`` падает с
    "invalid path ... unable to checkout working tree", а ``read-tree``/checkout
    не могут даже наполнить индекс.

    Обход (чистый plumbing, без checkout):
      1. ``git clone --depth 1 --no-checkout`` (HEAD есть, индекс пуст);
      2. ``ls-tree -r HEAD`` -> отфильтровать Windows-невалидные пути;
      3. ``update-index --index-info`` со сформированной лентой;
      4. ``checkout-index -a -f`` — выложить рабочее дерево из индекса.

    Отбрасываемые файлы — только шаблоны конфигураций внешних сервисов
    (apache/nginx/uwsgi), для запуска SearXNG не нужны.
    """
    _run([git, "clone", "--depth", "1", "--no-checkout", SEARXNG_REPO_URL, repo_dir])

    tree = _run([git, "-C", repo_dir, "ls-tree", "-r", "HEAD"])
    entries = [line for line in tree.splitlines() if line.strip()]
    keep: list[str] = []
    dropped: list[str] = []
    for line in entries:
        path = line.split("\t", 1)[1] if "\t" in line else ""
        (keep if _windows_safe_relpath(path) else dropped).append(line)

    feed = repo_dir.parent / f"{repo_dir.name}.index-info.tmp"
    feed.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    try:
        with feed.open("rb") as handle:
            _run(
                [git, "-C", repo_dir, "-c", "core.protectNTFS=false",
                 "update-index", "--index-info"],
                stdin=handle,
            )
    finally:
        feed.unlink(missing_ok=True)
    _run([git, "-C", repo_dir, "checkout-index", "-a", "-f"])

    if not (repo_dir / "searx" / "webapp.py").is_file():
        raise InstallError(
            "После checkout отсутствует searx/webapp.py — "
            "структура upstream SearXNG изменилась?"
        )


def ensure_supported_host() -> None:
    """Интерпретатор, создающий venv, должен быть 3.11+ (иначе ``tomllib`` нет)."""
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"searxng-local поддерживает только Windows (получено: {sys.platform})."
        )
    if sys.version_info < MINIMUM_PYTHON:
        raise UnsupportedPlatformError(
            f"Установку нужно запускать Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+ "
            f"(сейчас {sys.version.split()[0]}). Без 3.11+ в venv не будет stdlib-модуля "
            "tomllib, который импортирует upstream SearXNG. Например: `py -3.12 -m ...`."
        )


def _exe_version(exe: Path) -> tuple[int, int] | None:
    """``(major, minor)`` интерпретатора ``exe``; None, если определить не удалось."""
    try:
        proc = subprocess.run(
            [str(exe), "-IS", "-c", "import sys; print(*sys.version_info[:2])"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = (proc.stdout or "").split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return None


def venv_is_valid(install_dir: Path | str) -> bool:
    """venv есть и создан Python не старше :data:`MINIMUM_PYTHON`.

    venv от старого Python внешне неотличим от правильного, но SearXNG в нём
    падает на ``import tomllib`` — поэтому проверяем фактическую версию.
    """
    python = venv_python(install_dir)
    if not python.is_file():
        return False
    version = _exe_version(python)
    return version is not None and version >= MINIMUM_PYTHON


def ensure_git() -> str:
    """Путь к git или GitNotFoundError с подсказкой по установке."""
    git = shutil.which("git")
    if git is None:
        raise GitNotFoundError(
            "git не найден в PATH. Установите Git для Windows: "
            "https://git-scm.com/download/win или `winget install Git.Git`."
        )
    return git


def _run(
    cmd: list[str | Path], *, cwd: Path | None = None, stdin: Any = None
) -> str:
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=stdin,
        creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise InstallError(
            f"Команда завершилась с кодом {proc.returncode}: "
            f"{' '.join(str(part) for part in cmd)}\n{details}"
        )
    return proc.stdout or ""


def render_settings(dest: Path, port: int) -> None:
    """Создаёт settings.yml из шаблона пакета со случайным secret_key."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    dest.write_text(
        template.replace("{{PORT}}", str(int(port))).replace(
            "{{SECRET_KEY}}", secrets.token_urlsafe(48)
        ),
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# Установка и обновление
# ---------------------------------------------------------------------------


def install(install_dir: Path | str, port: int, *, force_pip: bool = False) -> dict[str, Any]:
    """Устанавливает SearXNG в ``install_dir`` (идемпотентно).

    Возвращает словарь-маркер установки. Шаги, уже выполненные ранее
    (по маркеру и наличию артефактов), пропускаются.
    """
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    ensure_supported_host()
    git = ensure_git()

    marker = read_marker(install_dir) or {}

    repo_dir = install_dir / REPO_DIR_NAME
    if not is_checkout_complete(repo_dir):
        _rmtree_strict(repo_dir)
        try:
            _clone_windows_safe(git, repo_dir)
        except InstallError as exc:
            try:
                _rmtree_force(repo_dir)
            except OSError:
                pass
            if repo_dir.exists():
                raise InstallError(
                    f"{exc}\nКроме того, не удалось полностью удалить повреждённый "
                    f"клон {repo_dir} — удалите каталог вручную и повторите."
                ) from exc
            raise
    commit = _run([git, "-C", repo_dir, "rev-parse", "--short", "HEAD"]).strip()

    # venv от Python <3.11 бесполезен (нет tomllib) — пересоздаём от текущего
    venv_rebuilt = False
    if (install_dir / VENV_DIR_NAME).exists() and not venv_is_valid(install_dir):
        _rmtree_strict(install_dir / VENV_DIR_NAME)
        venv_rebuilt = True

    python = venv_python(install_dir)
    if not python.is_file():
        _run([sys.executable, "-m", "venv", install_dir / VENV_DIR_NAME])
        venv_rebuilt = True
    if not venv_is_valid(install_dir):
        raise InstallError(
            f"venv {install_dir / VENV_DIR_NAME} не проходит проверку версии Python "
            f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+. Удалите каталог установки "
            "и повторите установку Python 3.11+."
        )

    if force_pip or venv_rebuilt or not marker.get("pip_done"):
        _run([python, "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"])
        _run([python, "-m", "pip", "install", "-r", repo_dir / "requirements.txt"])
        try:
            _run([python, "-m", "pip", "install", "-e", repo_dir])
        except InstallError:
            # у upstream бывают проблемы со сборкой при изоляции — ретрай
            _run([python, "-m", "pip", "install", "--no-build-isolation", "-e", repo_dir])

    apply_windows_patch(repo_dir)

    settings = install_dir / SETTINGS_NAME
    if not settings.is_file():
        render_settings(settings, port)

    version = _exe_version(python) or MINIMUM_PYTHON
    marker = {
        "version": MARKER_VERSION,
        "port": int(port),
        "commit": commit,
        "python": ".".join(str(part) for part in version),
        "patched": True,
        "pip_done": True,
    }
    (install_dir / MARKER_NAME).write_text(
        json.dumps(marker, indent=2), encoding="utf-8", newline="\n"
    )
    return marker


def update(install_dir: Path | str) -> dict[str, Any]:
    """Обновляет установленный SearXNG: свежий клон upstream + pip + патч.

    Порт и settings.yml берутся из существующей установки. Автообновления
    нет — только явный вызов.

    Почему не ``git pull``: индекс нельзя наполнить из нового HEAD, пока в
    дереве upstream есть невалидные для NTFS пути (см. _clone_windows_safe),
    поэтому обновление сводится к повторному клону с фильтрацией.
    """
    install_dir = Path(install_dir)
    marker = read_marker(install_dir)
    if marker is None:
        raise NotInstalledError(f"SearXNG не установлен в {install_dir}.")
    port = int(marker.get("port", 8888))

    git = ensure_git()
    repo_dir = install_dir / REPO_DIR_NAME
    _rmtree_strict(repo_dir)
    _clone_windows_safe(git, repo_dir)
    return install(install_dir, port, force_pip=True)
