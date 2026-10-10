# core/download_types.py
"""Типы данных, связанные со скачиванием.

Тут только dataclass'ы и enum'ы — никакой логики. Просто чтобы код
в других модулях был читаемее.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence
from enum import IntEnum


class DownloadFileState(IntEnum):
    """Состояние одного файла в процессе загрузки."""

    DOWNLOADING = 0  # качается прямо сейчас
    VERIFYING = 1    # проверяем хэш
    RETRYING = 2     # упало, но пробуем ещё раз
    FAILED = 3       # сдались, файл не скачался


@dataclass
class FileProgressSnapshot:
    """Снимок прогресса по одному файлу. Используется для отображения."""

    asset_name: str
    state: DownloadFileState
    bytes_completed: int
    verified_bytes: int
    total_bytes: int
    attempt: int
    max_attempts: int
    detail: Optional[str] = None

    @property
    def fraction(self) -> float:
        """Доля скачанного от общего размера, от 0 до 1."""
        if self.total_bytes <= 0:
            return 0.0
        return min(self.bytes_completed / self.total_bytes, 1.0)

    @property
    def verify_fraction(self) -> float:
        """Доля проверенного от общего размера."""
        if self.total_bytes <= 0:
            return 0.0
        return min(self.verified_bytes / self.total_bytes, 1.0)


@dataclass
class RateMeter:
    """Измеритель скорости: текущее значение + история для сглаживания."""

    bytes_per_second: float
    history: Sequence[float] = field(default_factory=list)

    @property
    def peak(self) -> float:
        """Пиковая скорость за окно наблюдения."""
        return max(self.history) if self.history else 0.0


@dataclass
class DownloadProgress:
    """Общий прогресс по всей загрузке. Сюда стекаются все цифры."""

    bytes_completed: int
    total_bytes: int
    work_completed: float
    work_total: float
    files_completed: int
    files_failed: int
    total_files: int
    bytes_per_second: float
    network: RateMeter
    disk: RateMeter
    active_files: Sequence[FileProgressSnapshot]

    @property
    def fraction(self) -> float:
        """Общая доля выполненной работы, от 0 до 1."""
        if self.work_total <= 0:
            return 0.0
        return min(self.work_completed / self.work_total, 1.0)

    @property
    def eta(self) -> Optional[float]:
        """Сколько секунд осталось до конца. None, если скорость неизвестна."""
        if self.bytes_per_second <= 0 or self.work_total <= 0:
            return None
        remaining = self.work_total - self.work_completed
        if remaining <= 0:
            return 0.0
        return remaining / self.bytes_per_second


@dataclass
class DownloadFailure:
    """Один неудачный файл: что не скачалось и почему."""

    asset_name: str
    reason: str


@dataclass
class DownloadResult:
    """Итог операции скачивания."""

    succeeded: int
    skipped: int
    failures: Sequence[DownloadFailure]
    cancelled: bool

    @property
    def all_succeeded(self) -> bool:
        """True, если всё скачалось и ничего не отменили."""
        return not self.cancelled and len(self.failures) == 0


@dataclass
class DownloadOptions:
    """Настройки загрузчика. Разумные дефолты, но можно переопределить."""

    max_parallel_files: int = 4          # сколько файлов качать одновременно
    max_attempts_per_file: int = 4       # сколько раз пробовать один файл
    max_attempts_per_chunk: int = 4      # сколько раз пробовать один чанк
    request_timeout: float = 60.0        # таймаут запроса в секундах
    retry_base_delay: float = 2.0        # базовая пауза перед повтором
    verify_existing_files: bool = True   # проверять ли уже существующие файлы