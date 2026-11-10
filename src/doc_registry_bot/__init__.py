"""Регистрация КД: бот выдает обозначения по ГОСТ 2.201-80 и ведет журнал."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("doc-registry-bot")
except PackageNotFoundError:  # запуск из исходников без установки
    __version__ = "0.0.0"
