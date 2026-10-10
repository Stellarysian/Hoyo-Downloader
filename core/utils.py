# core/utils.py
"""Разные утилиты и константы.

Всё, что не тянет на отдельный модуль, но нужно нескольким файлам сразу:
таблицы игр и регионов, форматирование размеров, подсчёт MD5.
"""

import hashlib
import os
from typing import Dict, Tuple, Optional, Callable, List

# ---------------------------------------------------------------------------
# Таблица game_id по паре (регион, игра).
# Эти ID — то, что уходит в параметр game_ids[] при запросе getGameBranches.
# Если добавишь новую игру, не забудь дописать её сюда, иначе API её не поймёт.
# ---------------------------------------------------------------------------
GAME_MAP: Dict[Tuple[str, str], str] = {
    ("OS", "nap"): "U5hbdsT9W7",
    ("CN", "nap"): "x6znKlJ0xK",
    ("OS", "hkrpg"): "4ziysqXOQ8",
    ("CN", "hkrpg"): "64kMb5iAWu",
    ("OS", "hk4e"): "gopR6Cufr3",
    ("CN", "hk4e"): "1Z8W5NHUQb",
    ("OS", "bh3"): "5TIVvvcwtM",
    ("CN", "bh3"): "osvnlOc0S8",
}

# ---------------------------------------------------------------------------
# Базовые адреса API для каждого региона.
# OS — международка (hoyoverse.com), CN — китайский (mihoyo.com).
# launcherId и platApp — обязательные параметры, без них API откажет.
# ---------------------------------------------------------------------------
SOPHON_MAP: Dict[str, Dict[str, str]] = {
    "OS": {
        "apiBase": "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches",
        "sophonBase": "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild",
        "launcherId": "VYTpXlbWo8",
        "platApp": "ddxf6vlr1reo",
    },
    "CN": {
        "apiBase": "https://hyp-api.mihoyo.com/hyp/hyp-connect/api/getGameBranches",
        "sophonBase": "https://api-takumi.mihoyo.com/downloader/sophon_chunk/api/getBuild",
        "launcherId": "jGHBHlcOq1",
        "platApp": "ddxf5qt290cg",
    },
}

# Списки поддерживаемых игр и регионов. Нужны для проверок и для UI.
SUPPORTED_GAMES = ["hk4e", "hkrpg", "nap", "bh3"]
SUPPORTED_REGIONS = ["OS", "CN"]


def validate_region(region: str) -> bool:
    """Проверяем, что регион вообще существует. Если нет — не лезем дальше."""
    return region in SOPHON_MAP


def get_game_id(region: str, game: str) -> str:
    """Достаём game_id по паре регион+игра.

    Если такой пары нет — кидаем ValueError, чтобы не молчать и не отправить
    в API мусор.
    """
    key = (region, game)
    if key not in GAME_MAP:
        raise ValueError(f"Unsupported game/region: {game}/{region}")
    return GAME_MAP[key]


def get_sophon_config(region: str) -> Dict[str, str]:
    """Возвращаем словарь с apiBase/sophonBase/launcherId/platApp для региона."""
    if region not in SOPHON_MAP:
        raise ValueError(f"Unsupported region: {region}")
    return SOPHON_MAP[region]


def format_size(bytes_: int) -> str:
    """Превращаем число байт в человекочитаемую строку: 1.23 GB и т.п.

    Используем двоичные приставки (1 KB = 1024 B), так привычнее в консоли.
    """
    if bytes_ >= 1 << 30:
        return f"{bytes_ / (1 << 30):.2f} GB"
    elif bytes_ >= 1 << 20:
        return f"{bytes_ / (1 << 20):.2f} MB"
    elif bytes_ >= 1 << 10:
        return f"{bytes_ / (1 << 10):.2f} KB"
    return f"{bytes_} B"


def get_md5(data: bytes) -> str:
    """MD5 от куска байт. Возвращаем в нижнем регистре, чтобы сравнивать было удобно."""
    return hashlib.md5(data).hexdigest().lower()


async def get_file_md5_async(
    path: str,
    on_bytes_read: Optional[Callable[[int], None]] = None,
) -> str:
    """Считаем MD5 файла асинхронно, по кускам.

    Читаем блоками по 4 МБ, чтобы не сожрать всю память на больших файлах.
    Если передан on_bytes_read — дёргаем его после каждого блока, чтобы
    прогресс-бар мог показать, сколько уже прочитано.
    """
    hasher = hashlib.md5()
    chunk_size = 4 << 20  # 4 МБ за раз — норм баланс между скоростью и памятью

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            if on_bytes_read:
                on_bytes_read(len(chunk))

    return hasher.hexdigest().lower()


def try_delete(path: str) -> None:
    """Пытаемся удалить файл и молча глотаем ошибки.

    Нужно для случаев, когда файл может быть занят или уже удалён —
    падать из-за этого не хочется.
    """
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass