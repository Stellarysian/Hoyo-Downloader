# core/dispatch.py
"""Заглушка для Dispatch.

Раньше тут была логика получения данных с официальных серверов, но сейчас
всё это делает Sophon. Оставили пустышку, чтобы main.py не падал при вызове
Dispatch.close().
"""

import asyncio
import json
import aiohttp
from typing import List, Tuple, Optional
from packaging import version as pkg_version

from .utils import SOPHON_MAP, GAME_MAP
from .sophon_manifest_pb2 import SophonManifestProto, SophonManifestAssetProperty


class Dispatch:
    """Пустая обёртка. Всё, что тут есть — закрытие сессии."""

    _client = None
    _dispatch_data = None

    @classmethod
    async def _get_client(cls):
        """Лениво создаём сессию, если её ещё нет."""
        import aiohttp
        if cls._client is None:
            connector = aiohttp.TCPConnector(
                limit=10,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                force_close=True
            )
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            cls._client = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            )
        return cls._client

    @classmethod
    async def get_dispatch_data(cls):
        """Заглушка: возвращаем пустую структуру.

        Реальные данные сейчас берутся из Sophon, так что этот метод
        никто не использует. Оставлен на случай, если пригодится позже.
        """
        if cls._dispatch_data is not None:
            return cls._dispatch_data

        cls._dispatch_data = {
            "hkrpg": {
                "minVersion": "1.0.0",
                "hashes": {},
                "sophonHashes": {},
                "scatterURL": "",
                "filesIndexOptions": {"index": ""},
                "urls": {"full": {}, "update": {}},
                "updatesHashes": {},
                "updatesHashesSpecial": {}
            },
            "hk4e": {
                "minVersion": "1.0.0",
                "hashes": {},
                "sophonHashes": {},
                "scatterURL": "",
                "filesIndexOptions": {"index": ""},
                "urls": {"full": {}, "update": {}},
                "updatesHashes": {},
                "updatesHashesSpecial": {}
            },
            "nap": {
                "minVersion": "1.0.0",
                "hashes": {},
                "sophonHashes": {},
                "scatterURL": "",
                "filesIndexOptions": {"index": ""},
                "urls": {"full": {}, "update": {}},
                "updatesHashes": {},
                "updatesHashesSpecial": {}
            },
            "bh3": {
                "minVersion": "1.0.0",
                "hashes": {},
                "sophonHashes": {},
                "scatterURL": "",
                "filesIndexOptions": {"index": ""},
                "urls": {"full": {}, "update": {}},
                "updatesHashes": {},
                "updatesHashesSpecial": {}
            }
        }

        return cls._dispatch_data

    @classmethod
    async def get_dispatch_versions(cls, game: str) -> List[str]:
        """Заглушка. Реальные версии берём из Meta.get_versions."""
        return []

    @classmethod
    async def get_packages(cls, game: str, version: str) -> List[str]:
        """Заглушка. Реальные пакеты берём из Meta.get_packages."""
        return ["Files", "ZIP", "Update"]

    @classmethod
    async def get_files(cls, game: str, version: str, mode: str) -> Tuple[SophonManifestProto, str]:
        """Заглушка. Манифест берём из Sophon.get_manifest."""
        return SophonManifestProto(), ""

    @classmethod
    async def close(cls):
        """Закрываем сессию, если она была создана."""
        if cls._client is not None:
            await cls._client.close()
            cls._client = None