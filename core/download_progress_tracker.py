# core/download_progress_tracker.py
"""Трекер прогресса загрузки.

Собирает статистику по всем файлам: сколько скачано, сколько проверено,
какая скорость, сколько осталось. Отдаёт снимки наружу через callback.
"""

import time
import threading
import math
from typing import Optional, List, Dict, Any, Sequence
from collections import deque

from .download_types import (
    DownloadProgress, FileProgressSnapshot, DownloadFileState,
    RateMeter
)


class ProgressTracker:
    """Следит за прогрессом всей загрузки и периодически отчитывается."""

    VERIFY_WEIGHT = 0.15      # насколько проверка хэша "весит" по сравнению с байтами
    REPORT_INTERVAL_MS = 0.15 # минимум между отчётами, сек
    HEARTBEAT_MS = 0.25       # как часто тикает фоновый поток, сек
    HISTORY_SAMPLES = 60      # сколько замеров скорости хранить

    def __init__(
        self,
        progress_callback: Optional[callable] = None,
        total_bytes: int = 0,
        verifiable_bytes: int = 0,
        total_files: int = 0
    ):
        self.sink = progress_callback
        self.total_bytes = total_bytes
        self.verifiable_bytes = verifiable_bytes
        self.total_files = total_files
        # Общий объём работы: байты + проверка с коэффициентом.
        self.work_total = total_bytes + verifiable_bytes * self.VERIFY_WEIGHT

        self.clock_start = time.monotonic()
        self._lock = threading.Lock()
        self.active: Dict[int, 'FileScope'] = {}
        self._next_scope_id = 0

        # Счётчики.
        self.completed_bytes = 0
        self.verified_bytes = 0
        self.files_completed = 0
        self.files_failed = 0

        self.network_bytes = 0
        self.disk_bytes = 0

        # История скоростей для сглаживания.
        self.network_history = deque(maxlen=self.HISTORY_SAMPLES)
        self.disk_history = deque(maxlen=self.HISTORY_SAMPLES)
        self.network_rate = 0.0
        self.disk_rate = 0.0
        self.last_sample_network = 0
        self.last_sample_disk = 0

        self.last_report_time: Optional[float] = None
        self.last_sample_work = 0.0
        self.last_sample_time: Optional[float] = None
        self.work_per_second = 0.0

        # Фоновый поток для heartbeat-отчётов.
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = True

        if progress_callback:
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Фоновый цикл: раз в HEARTBEAT_MS замеряем скорость и шлём отчёт."""
        while self._running:
            time.sleep(self.HEARTBEAT_MS)
            self._sample_rates()
            self.report()

    def create_scope(self, asset_name: str, asset_size: int, has_hash: bool, max_attempts: int):
        """Создаём область учёта для одного файла."""
        scope_id = self._next_scope_id
        self._next_scope_id += 1
        scope = self.FileScope(
            self, scope_id, asset_name, asset_size,
            has_hash, max_attempts
        )
        with self._lock:
            self.active[scope_id] = scope
        return scope

    def file_finished(self, scope: 'FileScope', success: bool):
        """Файл завершён: убираем из активных и обновляем счётчики."""
        with self._lock:
            self.active.pop(scope.id, None)
        if success:
            self.files_completed += 1
        else:
            self.files_failed += 1
        self.report(force=True)

    def add_bytes(self, delta: int):
        """Добавляем (или отнимаем) переданные байты."""
        if delta:
            self.completed_bytes += delta

    def add_verified(self, delta: int):
        """Добавляем проверенные байты."""
        if delta:
            self.verified_bytes += delta

    def add_network(self, delta: int):
        """Учёт сетевого трафика."""
        if delta > 0:
            self.network_bytes += delta

    def add_disk(self, delta: int):
        """Учёт записи на диск."""
        if delta > 0:
            self.disk_bytes += delta

    def current_work(self) -> float:
        """Текущий объём выполненной работы с учётом веса проверки."""
        bytes_done = min(self.completed_bytes, self.total_bytes)
        verified = min(self.verified_bytes, self.verifiable_bytes)
        return bytes_done + verified * self.VERIFY_WEIGHT

    def _sample_rates(self):
        """Замеряем мгновенные скорости и сглаживаем их."""
        with self._lock:
            now = time.monotonic()
            if self.last_sample_time is None:
                # Первый замер — просто запоминаем точку отсчёта.
                self.last_sample_time = now
                self.last_sample_network = self.network_bytes
                self.last_sample_disk = self.disk_bytes
                self.last_sample_work = self.current_work()
                return

            elapsed = now - self.last_sample_time
            if elapsed < 0.05:
                # Слишком часто — пропускаем, чтобы не дёргать зря.
                return

            net = self.network_bytes
            disk = self.disk_bytes
            work = self.current_work()

            self.network_rate = max(0, (net - self.last_sample_network) / elapsed)
            self.disk_rate = max(0, (disk - self.last_sample_disk) / elapsed)

            # Сглаживаем общую скорость экспоненциальным средним.
            instant = (work - self.last_sample_work) / elapsed
            self.work_per_second = (
                instant if self.work_per_second <= 0
                else self.work_per_second * 0.7 + instant * 0.3
            )

            self.network_history.append(self.network_rate)
            self.disk_history.append(self.disk_rate)

            self.last_sample_network = net
            self.last_sample_disk = disk
            self.last_sample_work = work
            self.last_sample_time = now

    def report(self, force: bool = False):
        """Отправляем снимок прогресса наружу. force=True шлём без задержки."""
        if self.sink is None:
            return

        now = time.monotonic()
        with self._lock:
            # Ограничиваем частоту отчётов, кроме случаев force.
            if not force and self.last_report_time is not None:
                if now - self.last_report_time < self.REPORT_INTERVAL_MS:
                    return

            self.last_report_time = now

            bytes_done = min(self.completed_bytes, self.total_bytes)

            active_files = []
            for scope in self.active.values():
                active_files.append(scope.snapshot())

            progress = DownloadProgress(
                bytes_completed=bytes_done,
                total_bytes=self.total_bytes,
                work_completed=self.current_work(),
                work_total=self.work_total,
                files_completed=self.files_completed,
                files_failed=self.files_failed,
                total_files=self.total_files,
                bytes_per_second=max(0, self.work_per_second),
                network=RateMeter(self.network_rate, list(self.network_history)),
                disk=RateMeter(self.disk_rate, list(self.disk_history)),
                active_files=active_files
            )

            self.sink(progress)

    def close(self):
        """Останавливаем фоновый поток и ждём его завершения."""
        self._running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=1.0)

    class FileScope:
        """Учёт прогресса по одному файлу."""

        def __init__(
            self,
            owner: 'ProgressTracker',
            scope_id: int,
            asset_name: str,
            asset_size: int,
            has_hash: bool,
            max_attempts: int
        ):
            self.owner = owner
            self.id = scope_id
            self.asset_name = asset_name
            self.asset_size = max(0, asset_size)
            self.has_hash = has_hash
            self.max_attempts = max_attempts

            self.counted = 0          # сколько уже зачтено как скачанное
            self.verify_counted = 0   # сколько зачтено как проверенное
            self.state = DownloadFileState.DOWNLOADING
            self.attempt = 1
            self.detail: Optional[str] = None

        def set_state(self, state: DownloadFileState, detail: Optional[str] = None):
            """Меняем состояние файла и сразу шлём отчёт."""
            self.state = state
            self.detail = detail
            self.owner.report(force=True)

        def set_detail(self, detail: Optional[str]):
            """Обновляем текстовое пояснение (например, "чанк 3/10")."""
            self.detail = detail
            self.owner.report()

        def set_attempt(self, attempt: int):
            """Запоминаем номер попытки."""
            self.attempt = attempt

        def count_network(self, bytes_: int):
            """Учёт сетевых байт для этого файла."""
            self.owner.add_network(bytes_)

        def count_disk(self, bytes_: int):
            """Учёт записанных на диск байт."""
            self.owner.add_disk(bytes_)

        def snapshot(self) -> FileProgressSnapshot:
            """Снимок текущего прогресса файла."""
            return FileProgressSnapshot(
                asset_name=self.asset_name,
                state=self.state,
                bytes_completed=min(self.counted, self.asset_size),
                verified_bytes=min(self.verify_counted, self.asset_size),
                total_bytes=self.asset_size,
                attempt=self.attempt,
                max_attempts=self.max_attempts,
                detail=self.detail
            )

        def advance(self, bytes_: int):
            """Продвигаем прогресс скачивания."""
            self._credit("counted", bytes_, self.owner.add_bytes)

        def advance_verify(self, bytes_: int):
            """Продвигаем прогресс проверки."""
            self._credit("verify_counted", bytes_, self.owner.add_verified)

        def _credit(self, field: str, bytes_: int, push_func: callable):
            """Начисляем байты в счётчик, не выходя за размер файла."""
            if bytes_ <= 0:
                return

            current = getattr(self, field)
            room = self.asset_size - current
            if room <= 0:
                return

            delta = min(bytes_, room)
            setattr(self, field, current + delta)
            push_func(delta)
            self.owner.report()

        def rewind(self):
            """Откатываем счётчики — нужно при повторной попытке."""
            if self.counted > 0:
                self.owner.add_bytes(-self.counted)
                self.counted = 0

            if self.verify_counted > 0:
                self.owner.add_verified(-self.verify_counted)
                self.verify_counted = 0

            self.owner.report(force=True)

        def rewind_verification(self):
            """Откатываем только счётчик проверки."""
            if self.verify_counted > 0:
                self.owner.add_verified(-self.verify_counted)
                self.verify_counted = 0
                self.owner.report(force=True)

        def complete(self):
            """Файл полностью готов: досчитываем остатки, чтобы не было дыр."""
            remaining = self.asset_size - self.counted
            if remaining > 0:
                self.counted = self.asset_size
                self.owner.add_bytes(remaining)

            if self.has_hash:
                verify_remaining = self.asset_size - self.verify_counted
                if verify_remaining > 0:
                    self.verify_counted = self.asset_size
                    self.owner.add_verified(verify_remaining)

            self.owner.report(force=True)