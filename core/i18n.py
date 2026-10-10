# core/i18n.py
"""Интернационализация: загрузка JSON-файлов с переводами и функция t().

Все строки интерфейса живут в i18n/<код языка>.json. В коде мы дёргаем
t("ключ") и получаем перевод на текущем языке.
"""

import json
import os
from typing import Dict, Optional

# Поддерживаемые языки: код -> ключ для отображения названия.
LANGUAGES = {
    "zh-CN": "lang.zh",
    "en-US": "lang.en",
    "ru-RU": "lang.ru",
}

# Язык по умолчанию. Загружается до того, как пользователь выберет свой.
DEFAULT_LANG = "en-US"


class I18n:
    """Синглтон для хранения переводов и текущего языка."""

    _instance: Optional["I18n"] = None
    _translations: Dict[str, str] = {}
    _current_lang: str = DEFAULT_LANG

    def __new__(cls):
        # Синглтон: один экземпляр на всё приложение.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def load(cls, lang: str) -> bool:
        """Загружаем файл переводов для указанного языка.

        Возвращаем True, если всё ок. Если файла нет или он битый — False,
        и в консоль летит предупреждение.
        """
        if lang not in LANGUAGES:
            return False

        # Файлы лежат в <корень проекта>/i18n/<код>.json
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base_dir, "i18n", f"{lang}.json")

        if not os.path.exists(path):
            print(f"⚠️ Language file not found: {path}")
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                cls._translations = json.load(f)
            cls._current_lang = lang
            return True
        except Exception as e:
            print(f"⚠️ Failed to load language file: {e}")
            return False

    @classmethod
    def current(cls) -> str:
        """Текущий код языка. Нужен, чтобы выбирать названия игр и регионов."""
        return cls._current_lang

    @classmethod
    def t(cls, key: str, **kwargs) -> str:
        """Перевод по ключу. Поддерживает подстановки вида {name}.

        Если ключа нет — возвращаем сам ключ, чтобы сразу было видно,
        что забыли добавить перевод.
        """
        text = cls._translations.get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                # Если в шаблоне лишние или недостающие скобки — не падаем,
                # отдаём как есть.
                return text
        return text


def t(key: str, **kwargs) -> str:
    """Короткая обёртка, чтобы не писать I18n.t каждый раз."""
    return I18n.t(key, **kwargs)