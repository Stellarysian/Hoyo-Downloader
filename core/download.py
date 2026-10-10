# core/download.py
"""Основная логика скачивания.

Тут живёт класс Downloader: он умеет качать файлы целиком или по Sophon-чанкам,
проверять хэши, ретраить упавшие куски и показывать прогресс.
"""

import asyncio
import aiohttp
import os
import shutil
import math
import random
import hashlib
import time
from pathlib import Path
from typing import List, Optional, Callable, Tuple, Set, Dict
import concurrent.futures

from .download_types import (
    DownloadOptions, DownloadResult, DownloadFailure,
    DownloadFileState, FileProgressSnapshot
)
from .download_progress_tracker import ProgressTracker
from .sophon_manifest_pb2 import (
    SophonManifestProto, SophonManifestAssetProperty,
    SophonManifestAssetChunk
)
from .utils import format_size, get_file_md5_async, try_delete


class Downloader:
    """Качает файлы по манифесту. Умеет и целые файлы, и Sophon-чанки."""

    # AssetType == 64 значит "это директория, а не файл".
    ASSET_TYPE_DIRECTORY = 64
    # Суффикс для недокачанных файлов, чтобы отличать их от готовых.
    PART_SUFFIX = ".meipart"

    def __init__(self, max_connections: int = 32):
        self.session: Optional[aiohttp.ClientSession] = None
        self.max_connections = max_connections
        self._connector: Optional[aiohttp.TCPConnector] = None
        # Поля для подсчёта прогресса и скорости.
        self._start_time: Optional[float] = None
        self._total_bytes_downloaded: int = 0
        self._last_bytes: int = 0
        self._last_time: Optional[float] = None
        self._speed_samples: List[float] = []
        self._active_files: Dict[str, str] = {}
        self._last_progress_line: str = ""

    async def _get_session(self) -> aiohttp.ClientSession:
        """Ленивая инициализация HTTP-сессии."""
        if self.session is None:
            self._connector = aiohttp.TCPConnector(
                limit=self.max_connections,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                force_close=True
            )
            # total=None, потому что качать можем долго; таймаут только на чтение.
            timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
            self.session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=timeout
            )
        return self.session

    def _format_time(self, seconds: float) -> str:
        """Превращаем секунды в человекочитаемую строку на русском."""
        if seconds < 0:
            return "?"
        if seconds < 60:
            return f"{seconds:.0f} сек"
        elif seconds < 3600:
            return f"{seconds/60:.1f} мин"
        elif seconds < 86400:
            return f"{seconds/3600:.1f} ч"
        else:
            return f"{seconds/86400:.1f} дн"

    def _format_speed(self, bytes_per_sec: float) -> str:
        """Форматируем скорость. Если 0 — значит, ещё ждём данных."""
        if bytes_per_sec <= 0:
            return "ожидание"
        if bytes_per_sec >= 1 << 30:
            return f"{bytes_per_sec/(1<<30):.2f} GB/s"
        elif bytes_per_sec >= 1 << 20:
            return f"{bytes_per_sec/(1<<20):.2f} MB/s"
        elif bytes_per_sec >= 1 << 10:
            return f"{bytes_per_sec/(1<<10):.2f} KB/s"
        else:
            return f"{bytes_per_sec:.0f} B/s"

    def _format_progress_line(self, progress) -> str:
        """Собираем одну строку прогресса: полоска, проценты, скорость, ETA."""
        # Если всё готово — строку не показываем.
        if progress.files_completed + progress.files_failed >= progress.total_files:
            return ""

        # Считаем среднюю скорость по последним замерам.
        now = time.time()
        if self._start_time is None:
            self._start_time = now

        if self._last_time and self._last_bytes > 0:
            delta_time = now - self._last_time
            if delta_time > 0.5:
                speed = (progress.bytes_completed - self._last_bytes) / delta_time
                self._speed_samples.append(speed)
                # Держим окно из 10 замеров, чтобы сгладить скачки.
                if len(self._speed_samples) > 10:
                    self._speed_samples.pop(0)
                self._last_bytes = progress.bytes_completed
                self._last_time = now
        else:
            self._last_bytes = progress.bytes_completed
            self._last_time = now

        avg_speed = sum(self._speed_samples) / len(self._speed_samples) if self._speed_samples else 0

        # Полоска прогресса.
        percent = progress.fraction * 100
        bar_len = 30
        filled = int(bar_len * progress.fraction)
        bar = "█" * filled + "░" * (bar_len - filled)

        # Оценка оставшегося времени.
        eta_str = "?"
        if avg_speed > 0 and progress.work_total > 0:
            remaining = progress.work_total - progress.work_completed
            if remaining > 0:
                eta_str = self._format_time(remaining / avg_speed)

        elapsed = self._format_time(time.time() - self._start_time)

        size_done = format_size(progress.bytes_completed)
        size_total = format_size(progress.total_bytes)
        speed_str = self._format_speed(avg_speed)

        # Показываем текущий файл, если есть активный.
        active_str = ""
        if progress.active_files:
            active = progress.active_files[0] if progress.active_files else None
            if active and active.state == DownloadFileState.DOWNLOADING:
                fname = active.asset_name
                # Длинные имена обрезаем, чтобы строка не уезжала.
                if len(fname) > 30:
                    fname = "..." + fname[-27:]
                active_str = f"\n📄 {fname} ({active.fraction*100:.1f}%)"

        return f"[{bar}] {percent:.1f}% ({size_done}/{size_total}) ⚡ {speed_str} ⏱ осталось {eta_str} прошло {elapsed} 📁 {progress.files_completed}/{progress.total_files}{active_str}"

    async def download_files_async(
        self,
        assets: List[SophonManifestAssetProperty],
        download_url: str,
        save_path: str,
        progress_callback: Optional[Callable] = None,
        options: Optional[DownloadOptions] = None,
        cancellation_event: Optional[asyncio.Event] = None
    ) -> DownloadResult:
        """Скачиваем список файлов в указанную папку.

        Возвращаем DownloadResult со статистикой: сколько получилось,
        сколько пропустили, что упало.
        """
        if options is None:
            options = DownloadOptions()

        root = os.path.abspath(save_path)
        os.makedirs(root, exist_ok=True)

        # Отделяем директории от файлов.
        files = [a for a in assets if a.AssetType != self.ASSET_TYPE_DIRECTORY]

        # Создаём все нужные подпапки заранее.
        for asset in assets:
            if asset.AssetType == self.ASSET_TYPE_DIRECTORY:
                path = self._resolve_path(root, asset.AssetName)
                if path:
                    os.makedirs(path, exist_ok=True)

        # Считаем общий объём для прогресса.
        total_size = sum(max(0, a.AssetSize) for a in files)
        verifiable_size = sum(
            max(0, a.AssetSize) for a in files if a.AssetHashMd5
        )

        print(f"\n📦 Начинаем качать {len(files)} файлов, всего {format_size(total_size)}")
        print("=" * 70)

        # Сбрасываем счётчики перед новой загрузкой.
        self._start_time = time.time()
        self._total_bytes_downloaded = 0
        self._last_bytes = 0
        self._last_time = self._start_time
        self._speed_samples = []
        self._last_progress_line = ""

        tracker = ProgressTracker(
            self._on_progress_update,
            total_size,
            verifiable_size,
            len(files)
        )

        failures = []
        skipped = 0
        cancelled = False
        completed_files = 0

        # Ограничиваем число одновременных загрузок.
        semaphore = asyncio.Semaphore(options.max_parallel_files)

        def custom_progress_callback(progress):
            """Внутренний колбэк: рисуем строку и передаём наружу, если просили."""
            line = self._format_progress_line(progress)
            if line:
                # \r возвращает курсор в начало строки — так строка обновляется на месте.
                print(f"\r{line}", end="", flush=True)

            if progress_callback:
                progress_callback(progress)

        tracker.sink = custom_progress_callback

        async def process_file(asset: SophonManifestAssetProperty):
            """Скачиваем один файл с ретраями."""
            nonlocal skipped, completed_files

            scope = tracker.create_scope(
                asset.AssetName,
                asset.AssetSize,
                bool(asset.AssetHashMd5),
                options.max_attempts_per_file
            )

            file_path = self._resolve_path(root, asset.AssetName)
            if file_path is None:
                # Подозрительный путь — пропускаем, чтобы не писать куда попало.
                print(f"\n❌ Пропуск {asset.AssetName}: некорректный путь")
                failures.append(DownloadFailure(asset.AssetName, "invalid path"))
                tracker.file_finished(scope, success=False)
                return

            last_error = None
            for attempt in range(1, options.max_attempts_per_file + 1):
                if cancellation_event and cancellation_event.is_set():
                    return

                scope.rewind()
                scope.set_attempt(attempt)
                scope.set_state(DownloadFileState.DOWNLOADING)

                try:
                    was_already_complete = await self._download_one(
                        asset, file_path, download_url, scope, options
                    )
                    scope.complete()
                    if was_already_complete:
                        skipped += 1
                    completed_files += 1
                    tracker.file_finished(scope, success=True)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    last_error = str(ex)
                    if attempt < options.max_attempts_per_file:
                        scope.set_state(DownloadFileState.RETRYING, last_error)
                        await self._delay_for_retry(options, attempt)
                    else:
                        print(f"\n❌ Ошибка {asset.AssetName}: {last_error}")

            scope.rewind()
            scope.set_state(DownloadFileState.FAILED, last_error)
            failures.append(DownloadFailure(asset.AssetName, last_error or "unknown error"))
            tracker.file_finished(scope, success=False)

        # Запускаем все задачи с ограничением параллелизма.
        tasks = []
        for asset in files:
            if cancellation_event and cancellation_event.is_set():
                cancelled = True
                break
            tasks.append(asyncio.create_task(self._process_with_semaphore(semaphore, process_file, asset)))

        if tasks:
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                cancelled = True

        tracker.report(force=True)
        tracker.close()

        # Итоговый отчёт.
        print()
        print("=" * 70)
        succeeded = len(files) - len(failures)
        elapsed = self._format_time(time.time() - self._start_time)

        if cancelled:
            print(f"⏹ Загрузка отменена (прошло: {elapsed})")
        elif not failures:
            print(f"✅ Готово! {succeeded} файлов ({skipped} уже были) за {elapsed}")
        else:
            print(f"⚠️ Загрузка завершена, но {len(failures)} файлов упали (прошло {elapsed})")
            for failure in failures[:5]:
                print(f"  - {failure.asset_name}: {failure.reason}")
            if len(failures) > 5:
                print(f"  ... и ещё {len(failures) - 5}")

        return DownloadResult(succeeded, skipped, failures, cancelled)

    def _on_progress_update(self, progress):
        """Заглушка — реальный колбэк подменяется в download_files_async."""
        pass

    async def _process_with_semaphore(self, semaphore, func, *args):
        """Обёртка: выполняем функцию, держа семафор."""
        async with semaphore:
            await func(*args)

    async def _download_one(
        self,
        asset: SophonManifestAssetProperty,
        file_path: str,
        download_url: str,
        scope: ProgressTracker.FileScope,
        options: DownloadOptions
    ) -> bool:
        """Скачиваем один файл. Возвращаем True, если он уже был готов."""
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        has_hash = bool(asset.AssetHashMd5)

        # Если файл уже есть и совпадает по хэшу — ничего не делаем.
        if os.path.exists(file_path):
            if await self._is_file_complete(asset, file_path, has_hash, scope, options):
                return True

        scope.set_state(DownloadFileState.DOWNLOADING)

        # Есть чанки — качаем по чанкам, нет — целиком.
        if asset.AssetChunks:
            await self._download_chunked(asset, file_path, download_url, scope, options)
        else:
            await self._download_whole(asset, file_path, download_url, scope, options)

        await self._verify_final(asset, file_path, has_hash, scope)
        return False

    async def _is_file_complete(
        self,
        asset: SophonManifestAssetProperty,
        file_path: str,
        has_hash: bool,
        scope: ProgressTracker.FileScope,
        options: DownloadOptions
    ) -> bool:
        """Проверяем, что файл на диске уже полный и правильный."""
        try:
            stat = os.stat(file_path)
            if stat.st_size != asset.AssetSize:
                return False
        except OSError:
            return False

        if not has_hash:
            # Хэша нет — доверяем размеру.
            return asset.AssetSize > 0

        if not options.verify_existing_files:
            # Пользователь попросил не проверять — считаем, что всё ок.
            return True

        scope.set_state(DownloadFileState.VERIFYING)
        actual = await self._hash_with_progress(file_path, asset.AssetSize, scope, credit_transfer=True)

        return actual == asset.AssetHashMd5.lower()

    async def _verify_final(
        self,
        asset: SophonManifestAssetProperty,
        file_path: str,
        has_hash: bool,
        scope: ProgressTracker.FileScope
    ):
        """Финальная проверка после скачивания. Если что-то не так — кидаем исключение."""
        if not os.path.exists(file_path):
            raise IOError(f"{asset.AssetName} исчез после загрузки.")

        stat = os.stat(file_path)
        if asset.AssetSize > 0 and stat.st_size != asset.AssetSize:
            raise IOError(f"{asset.AssetName}: {stat.st_size} байт, ожидалось {asset.AssetSize}.")

        if not has_hash:
            return

        scope.set_state(DownloadFileState.VERIFYING)
        actual = await self._hash_with_progress(file_path, asset.AssetSize, scope, credit_transfer=False)

        if actual != asset.AssetHashMd5.lower():
            # Битый файл лучше удалить, чтобы не мешал при следующем запуске.
            try_delete(file_path)
            raise ValueError(f"{asset.AssetName} не прошёл MD5 (получено {actual}, ожидалось {asset.AssetHashMd5})")

    async def _hash_with_progress(
        self,
        file_path: str,
        expected_size: int,
        scope: ProgressTracker.FileScope,
        credit_transfer: bool
    ) -> str:
        """Считаем MD5 файла и обновляем прогресс по ходу чтения."""
        total = max(1, expected_size)
        read_bytes = 0

        scope.rewind_verification()

        def on_bytes_read(bytes_read: int):
            nonlocal read_bytes
            read_bytes += bytes_read
            scope.count_disk(bytes_read)
            scope.advance_verify(bytes_read)
            if credit_transfer:
                # Если файл уже был готов, засчитываем чтение как "передачу".
                scope.advance(bytes_read)
            progress = min(100, read_bytes * 100 // total)
            scope.set_detail(f"проверка {progress}%")

        return await get_file_md5_async(file_path, on_bytes_read)

    async def _download_whole(
        self,
        asset: SophonManifestAssetProperty,
        file_path: str,
        download_url: str,
        scope: ProgressTracker.FileScope,
        options: DownloadOptions
    ):
        """Качаем файл целиком, без разбиения на чанки."""
        url = self._build_file_url(download_url, asset.AssetName)
        part_path = file_path + self.PART_SUFFIX

        session = await self._get_session()

        try:
            async with session.get(url) as response:
                response.raise_for_status()

                announced = int(response.headers.get("Content-Length", 0))
                written = 0

                # Пишем во временный файл, в конце переименуем.
                with open(part_path, "wb") as f:
                    chunk_size = 1 << 20  # по 1 МБ
                    while True:
                        try:
                            chunk = await response.content.read(chunk_size)
                            if not chunk:
                                break
                            scope.count_network(len(chunk))
                            f.write(chunk)
                            # Отдаём управление другим задачам, чтобы UI не завис.
                            await asyncio.sleep(0)
                            scope.count_disk(len(chunk))
                            written += len(chunk)
                            scope.advance(len(chunk))
                        except asyncio.TimeoutError:
                            raise TimeoutError(f"Таймаут при скачивании {asset.AssetName}")

                # Проверяем, что скачали столько, сколько обещал сервер.
                if announced and written != announced:
                    raise IOError(f"Соединение оборвалось после {written} из {announced} байт.")

            # Всё ок — переименовываем временный файл в итоговый.
            shutil.move(part_path, file_path)

        except Exception:
            # Что-то пошло не так — убираем временный файл, чтобы не мусорить.
            try_delete(part_path)
            raise

    async def _download_chunked(
        self,
        asset: SophonManifestAssetProperty,
        file_path: str,
        download_url: str,
        scope: ProgressTracker.FileScope,
        options: DownloadOptions
    ):
        """Качаем файл по Sophon-чанкам.

        Каждый чанк кладётся по своему смещению в итоговом файле. Если чанк
        уже на диске и хэш совпадает — пропускаем, не тратим трафик.
        """
        # Создаём файл нужного размера, чтобы можно было писать по смещениям.
        with open(file_path, "wb") as f:
            f.truncate(asset.AssetSize)

        chunk_index = 0
        total_chunks = len(asset.AssetChunks)

        for chunk in asset.AssetChunks:
            chunk_index += 1
            scope.set_detail(f"чанк {chunk_index}/{total_chunks}")

            size = int(chunk.ChunkSizeDecompressed)
            if size < 0 or chunk.ChunkOnFileOffset < 0:
                raise ValueError(f"Некорректный чанк {chunk.ChunkName}")

            if chunk.ChunkOnFileOffset + size > asset.AssetSize:
                raise ValueError(f"Чанк {chunk.ChunkName} не влезает в {asset.AssetName}")

            # Проверяем, нет ли уже этого чанка на диске.
            if await self._chunk_already_on_disk(file_path, chunk, size, scope):
                scope.advance(size)
                continue

            # Качаем и пишем.
            data = await self._fetch_chunk(chunk, download_url, size, scope, options)

            with open(file_path, "r+b") as f:
                f.seek(chunk.ChunkOnFileOffset)
                f.write(data[:size])

            scope.count_disk(size)
            scope.advance(size)

    async def _chunk_already_on_disk(
        self,
        file_path: str,
        chunk: SophonManifestAssetChunk,
        size: int,
        scope: ProgressTracker.FileScope
    ) -> bool:
        """Проверяем, что чанк уже лежит на диске и совпадает по MD5."""
        if not chunk.ChunkDecompressedHashMd5:
            return False

        try:
            with open(file_path, "rb") as f:
                f.seek(chunk.ChunkOnFileOffset)
                data = f.read(size)
                if len(data) != size:
                    return False
                scope.count_disk(len(data))

                actual_hash = hashlib.md5(data).hexdigest().lower()
                return actual_hash == chunk.ChunkDecompressedHashMd5.lower()
        except Exception:
            return False

    async def _fetch_chunk(
        self,
        chunk: SophonManifestAssetChunk,
        download_url: str,
        size: int,
        scope: ProgressTracker.FileScope,
        options: DownloadOptions
    ) -> bytes:
        """Качаем один чанк, распаковываем zstd и проверяем MD5."""
        import zstandard as zstd

        # В URL вместо $0 подставляем имя чанка.
        url = download_url.replace("$0", chunk.ChunkName)
        last_error = None

        session = await self._get_session()

        for attempt in range(1, options.max_attempts_per_chunk + 1):
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    compressed = await response.content.read()

                scope.count_network(len(compressed))

                # Распаковываем zstd.
                decompressor = zstd.ZstdDecompressor()
                plain = decompressor.decompress(compressed)

                if len(plain) != size:
                    raise ValueError(f"чанк распаковался в {len(plain)} байт, ожидалось {size}")

                # Проверяем MD5 распакованных данных.
                if chunk.ChunkDecompressedHashMd5:
                    actual_hash = hashlib.md5(plain).hexdigest().lower()
                    if actual_hash != chunk.ChunkDecompressedHashMd5.lower():
                        raise ValueError("MD5 чанка не совпал")

                return plain

            except Exception as ex:
                last_error = str(ex)
                if attempt < options.max_attempts_per_chunk:
                    await self._delay_for_retry(options, attempt)
                else:
                    raise

        raise IOError(f"Чанк {chunk.ChunkName} не скачался ({last_error})")

    def _build_file_url(self, download_url: str, asset_name: str) -> str:
        """Собираем полный URL файла из базового и имени ассета."""
        trimmed = download_url.rstrip("/")
        # Если базовый URL уже заканчивается на имя файла — не дублируем.
        if trimmed.endswith(asset_name):
            return trimmed
        return f"{trimmed}/{asset_name.replace('\\', '/').lstrip('/')}"

    def _resolve_path(self, root: str, asset_name: str) -> Optional[str]:
        """Превращаем имя ассета в абсолютный путь внутри root.

        Защита от directory traversal: если путь вылезает за пределы root,
        возвращаем None.
        """
        if not asset_name:
            return None

        relative = asset_name.replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)

        try:
            candidate = os.path.abspath(os.path.join(root, relative))
            prefix = os.path.abspath(root) + os.sep
            if not candidate.startswith(prefix):
                return None
            return candidate
        except Exception:
            return None

    async def _delay_for_retry(self, options: DownloadOptions, attempt: int):
        """Пауза перед повтором с экспоненциальной задержкой и джиттером."""
        seconds = options.retry_base_delay * math.pow(2, attempt - 1)
        # Ограничиваем сверху 30 секундами и добавляем случайный разброс.
        seconds = min(seconds, 30) * (0.8 + random.random() * 0.4)
        await asyncio.sleep(seconds)

    async def close(self):
        """Закрываем HTTP-сессию и коннектор."""
        if self.session:
            await self.session.close()
            self.session = None
        if self._connector:
            await self._connector.close()
            self._connector = None