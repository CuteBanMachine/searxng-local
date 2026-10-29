"""CLI: ``python -m searxng_local {install,status,update}``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import installer
from .exceptions import SearXNGError


def _dir_arg(value: str | None) -> Path:
    return Path(value).resolve() if value is not None else Path.cwd() / "searxng-local"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="searxng_local",
        description="Локальный SearXNG: установка и обслуживание (Windows 10+, Python 3.11+).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="установить SearXNG (идемпотентно)")
    p_install.add_argument(
        "--dir", help="каталог установки (по умолчанию ./searxng-local)"
    )
    p_install.add_argument(
        "--port", type=int, default=8888, help="порт в settings.yml (по умолчанию 8888)"
    )

    p_status = sub.add_parser("status", help="состояние установки")
    p_status.add_argument("--dir", help="каталог установки")

    p_update = sub.add_parser(
        "update", help="обновить SearXNG (git fetch + pip + патч)"
    )
    p_update.add_argument("--dir", help="каталог установки")

    args = parser.parse_args(argv)
    install_dir = _dir_arg(getattr(args, "dir", None))

    try:
        if args.command == "install":
            installer.ensure_supported_host()
            marker = installer.install(install_dir, args.port)
            print(
                f"Установлено: {install_dir} (порт {marker['port']}, commit {marker['commit']})"
            )
        elif args.command == "status":
            marker = installer.read_marker(install_dir)
            if marker is None:
                print(f"Не установлено: {install_dir}")
                return 1
            port = int(marker.get("port", 8888))
            listening = installer.port_in_use(port)
            print(f"Каталог:      {install_dir}")
            print(
                f"Порт:         {port} {'(слушает — запущен)' if listening else '(свободен)'}"
            )
            print(f"Commit:       {marker.get('commit', '?')}")
            print(
                f"settings.yml: "
                f"{'ок' if (install_dir / installer.SETTINGS_NAME).is_file() else 'ОТСУТСТВУЕТ'}"
            )
        elif args.command == "update":
            installer.ensure_supported_host()
            marker = installer.update(install_dir)
            print(f"Обновлено до commit {marker['commit']} (порт {marker['port']})")
    except SearXNGError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
