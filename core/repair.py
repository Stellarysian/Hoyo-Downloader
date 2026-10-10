# core/repair.py
"""Починка и обновление уже установленной игры.

Сканируем папку, сравниваем файлы с манифестом, докачиваем только битые
или отсутствующие. Есть режим dry-run — только показать, что нужно чинить.
"""

import os
import hashlib
import asyncio
from typing import List, Tuple, Dict, Optional, Set
from dataclasses import dataclass
from pathlib import Path

from .sophon_manifest_pb2 import SophonManifestProto, SophonManifestAssetProperty
from .download import Downloader
from .download_types import DownloadOptions, DownloadResult
from .utils import format_size, get_file_md5_async


@dataclass
class FileStatus:
    """Статус одного файла: есть ли он, совпадает ли размер и хэш."""

    asset: SophonManifestAssetProperty
    local_path: str
    exists: bool
    size_match: bool
    hash_match: bool
    needs_repair: bool


@dataclass
class RepairReport:
    """Отчёт о сканировании: что нашли, что чинить, сколько качать."""

    total_files: int
    missing_files: List[FileStatus]
    corrupt_files: List[FileStatus]
    size_mismatch_files: List[FileStatus]
    hash_mismatch_files: List[FileStatus]
    up_to_date_files: List[FileStatus]
    total_size_to_download: int

    @property
    def needs_repair_count(self) -> int:
        """Сколько всего файлов требуют вмешательства."""
        return len(self.missing_files) + len(self.corrupt_files) + len(self.size_mismatch_files) + len(self.hash_mismatch_files)

    def print_report(self):
        """Печатаем подробный отчёт в консоль."""
        print("\n" + "=" * 60)
        print("ОТЧЁТ О ПОЧИНКЕ")
        print("=" * 60)
        print(f"Всего файлов в манифесте: {self.total_files}")
        print(f"Актуальных файлов: {len(self.up_to_date_files)}")
        print(f"Отсутствующих: {len(self.missing_files)}")
        print(f"С несовпадением размера: {len(self.size_mismatch_files)}")
        print(f"С несовпадением хэша: {len(self.hash_mismatch_files)}")
        print(f"Битых: {len(self.corrupt_files)}")
        print(f"Всего требуют починки: {self.needs_repair_count}")
        print(f"Объём для скачивания: {format_size(self.total_size_to_download)}")

        if self.needs_repair_count > 0:
            print("\n" + "-" * 60)
            print("ПОДРОБНОСТИ:")
            print("-" * 60)

            if self.missing_files:
                print(f"\n[Отсутствующие файлы] ({len(self.missing_files)})")
                for f in self.missing_files[:10]:  # показываем первые 10
                    print(f"  - {f.asset.AssetName} ({format_size(f.asset.AssetSize)})")
                if len(self.missing_files) > 10:
                    print(f"  ... и ещё {len(self.missing_files) - 10}")

            if self.size_mismatch_files:
                print(f"\n[Несовпадение размера] ({len(self.size_mismatch_files)})")
                for f in self.size_mismatch_files[:10]:
                    local_size = os.path.getsize(f.local_path) if os.path.exists(f.local_path) else 0
                    print(f"  - {f.asset.AssetName}: локально {format_size(local_size)} vs ожидалось {format_size(f.asset.AssetSize)}")
                if len(self.size_mismatch_files) > 10:
                    print(f"  ... и ещё {len(self.size_mismatch_files) - 10}")

            if self.hash_mismatch_files:
                print(f"\n[Несовпадение хэша] ({len(self.hash_mismatch_files)})")
                for f in self.hash_mismatch_files[:10]:
                    print(f"  - {f.asset.AssetName}")
                if len(self.hash_mismatch_files) > 10:
                    print(f"  ... и ещё {len(self.hash_mismatch_files) - 10}")


class FileRepair:
    """Сканирует и чинит файлы игры."""

    ASSET_TYPE_DIRECTORY = 64

    def __init__(self):
        self.downloader = Downloader()

    async def scan_files(
        self,
        manifest: SophonManifestProto,
        game_path: str,
        verify_hash: bool = True
    ) -> RepairReport:
        """Сканируем папку игры и сравниваем с манифестом.

        Возвращаем отчёт с разбивкой по типам проблем.
        """
        root = os.path.abspath(game_path)
        missing_files = []
        corrupt_files = []
        size_mismatch_files = []
        hash_mismatch_files = []
        up_to_date_files = []
        total_size = 0

        # Директории нас не интересуют — только файлы.
        files = [a for a in manifest.Assets if a.AssetType != self.ASSET_TYPE_DIRECTORY]
        total_files = len(files)

        print(f"Сканируем {total_files} файлов в {game_path}...")

        for i, asset in enumerate(files):
            # Обновляем строку на месте, чтобы не засорять консоль.
            percent = (i + 1) * 100 // total_files
            print(f"\rСканирование: {i + 1}/{total_files} файлов ({percent}%)...", end="", flush=True)

            local_path = self._resolve_path(root, asset.AssetName)
            if local_path is None:
                # Путь подозрительный — считаем, что файла нет.
                missing_files.append(FileStatus(
                    asset=asset,
                    local_path="",
                    exists=False,
                    size_match=False,
                    hash_match=False,
                    needs_repair=True
                ))
                total_size += asset.AssetSize
                continue

            exists = os.path.exists(local_path)

            if not exists:
                status = FileStatus(
                    asset=asset,
                    local_path=local_path,
                    exists=False,
                    size_match=False,
                    hash_match=False,
                    needs_repair=True
                )
                missing_files.append(status)
                total_size += asset.AssetSize
                continue

            # Проверяем размер.
            local_size = os.path.getsize(local_path)
            size_match = local_size == asset.AssetSize

            # Проверяем хэш, если он есть и если пользователь не отключил проверку.
            hash_match = True
            if verify_hash and asset.AssetHashMd5:
                try:
                    actual_hash = await get_file_md5_async(local_path)
                    hash_match = actual_hash == asset.AssetHashMd5.lower()
                except Exception:
                    hash_match = False

            needs_repair = not size_match or not hash_match

            if needs_repair:
                if not size_match:
                    status = FileStatus(
                        asset=asset,
                        local_path=local_path,
                        exists=True,
                        size_match=False,
                        hash_match=hash_match,
                        needs_repair=True
                    )
                    size_mismatch_files.append(status)
                elif not hash_match:
                    status = FileStatus(
                        asset=asset,
                        local_path=local_path,
                        exists=True,
                        size_match=True,
                        hash_match=False,
                        needs_repair=True
                    )
                    hash_mismatch_files.append(status)
                total_size += asset.AssetSize
            else:
                status = FileStatus(
                    asset=asset,
                    local_path=local_path,
                    exists=True,
                    size_match=True,
                    hash_match=True,
                    needs_repair=False
                )
                up_to_date_files.append(status)

        # Переводим строку после прогресса сканирования.
        print()

        return RepairReport(
            total_files=len(files),
            missing_files=missing_files,
            corrupt_files=corrupt_files,
            size_mismatch_files=size_mismatch_files,
            hash_mismatch_files=hash_mismatch_files,
            up_to_date_files=up_to_date_files,
            total_size_to_download=total_size
        )

    async def repair_files(
        self,
        manifest: SophonManifestProto,
        download_url: str,
        game_path: str,
        progress_callback=None,
        options: Optional[DownloadOptions] = None,
        verify_hash: bool = True,
        dry_run: bool = False
    ) -> Tuple[RepairReport, Optional[DownloadResult]]:
        """Починить игру: сканируем, потом докачиваем то, что нужно.

        Если dry_run=True — только сканируем и печатаем отчёт, ничего не качаем.
        """
        # Сначала сканируем.
        report = await self.scan_files(manifest, game_path, verify_hash)
        report.print_report()

        if report.needs_repair_count == 0:
            print("Все файлы актуальны. Починка не нужна.")
            return report, None

        if dry_run:
            print(f"\n[DRY RUN] Скачали бы {report.needs_repair_count} файлов ({format_size(report.total_size_to_download)})")
            return report, None

        # Собираем список ассетов, которые надо качать.
        assets_to_download = [
            f.asset for f in report.missing_files +
            report.size_mismatch_files +
            report.hash_mismatch_files
        ]

        print(f"\nКачаем {len(assets_to_download)} файлов для починки...")

        if options is None:
            options = DownloadOptions()

        result = await self.downloader.download_files_async(
            assets_to_download,
            download_url,
            game_path,
            progress_callback=progress_callback,
            options=options
        )

        return report, result

    def _resolve_path(self, root: str, asset_name: str) -> Optional[str]:
        """Превращаем имя ассета в путь внутри root с защитой от traversal."""
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

    async def close(self):
        """Закрываем загрузчик."""
        await self.downloader.close()