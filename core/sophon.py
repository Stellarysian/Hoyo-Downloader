# core/sophon.py
"""Клиент Sophon API.

Тут всё общение с официальными серверами: получаем список веток игры,
собираем build по версии, качаем и разбираем манифест.
"""

import json
from typing import Tuple

from .utils import get_game_id, get_sophon_config
from .sophon_manifest_pb2 import SophonManifestProto
import zstandard as zstd


class Sophon:
    """Обёртка над Sophon API.

    Все методы — classmethod, потому что клиент общий на всё приложение.
    Сессию aiohttp создаём лениво, при первом запросе.
    """

    _client = None
    _branch = "main"  # основная ветка, кроме неё бывает pre_download

    @classmethod
    async def _get_client(cls):
        """Ленивая инициализация HTTP-сессии.

        Ограничиваем число соединений, чтобы не забить сеть и не получить
        бан от сервера.
        """
        import aiohttp
        if cls._client is None:
            connector = aiohttp.TCPConnector(
                limit=10,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                force_close=True,
            )
            timeout = aiohttp.ClientTimeout(total=120, connect=30)
            cls._client = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
        return cls._client

    @classmethod
    async def get_game_branches(cls, game: str, region: str) -> dict:
        """Запрашиваем getGameBranches для конкретной игры и региона.

        Возвращаем сырой JSON, дальше с ним работает Meta.
        """
        game_id = get_game_id(region, game)
        cfg = get_sophon_config(region)

        url = (
            f"{cfg['apiBase']}"
            f"?launcher_id={cfg['launcherId']}"
            f"&game_ids[]={game_id}"
        )

        session = await cls._get_client()
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    @classmethod
    async def get_build(
        cls,
        region: str,
        package_id: str,
        password: str,
        version: str,
        pre_download: bool = False,
    ) -> dict:
        """Запрашиваем getBuild для конкретной версии.

        ВАЖНО: version должен быть полным тегом вида "5.5.0", а не "5.5".
        Иначе API ответит 404 или пустотой.
        """
        cfg = get_sophon_config(region)
        branch = "pre_download" if pre_download else cls._branch

        url = (
            f"{cfg['sophonBase']}"
            f"?branch={branch.replace('_', '')}"
            f"&package_id={package_id}"
            f"&password={password}"
            f"&plat_app={cfg['platApp']}"
        )
        # Для pre_download тег не передаём — API сам знает, что отдавать.
        if not pre_download:
            url += f"&tag={version}"

        session = await cls._get_client()
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    @classmethod
    async def get_manifest(
        cls,
        game: str,
        version: str,
        region: str,
        category_id: str,
    ) -> Tuple[SophonManifestProto, str]:
        """Качаем и разбираем манифест для конкретной категории.

        version — полный тег, можно с пометкой " (pre-download)".
        Возвращаем (распарсенный манифест, базовый URL для чанков).
        """
        # Определяем, предзагрузка это или нет.
        pre_download = False
        clean_version = version
        if version.endswith(" (pre-download)"):
            pre_download = True
            clean_version = version.replace(" (pre-download)", "")

        # 1. Получаем ветки — только для текущего региона.
        branches = await cls.get_game_branches(game, region)
        branch_list = branches.get("data", {}).get("game_branches", [])
        if not branch_list:
            raise RuntimeError(f"getGameBranches returned no branches for {game}/{region}")

        branch = branch_list[0]
        branch_key = "pre_download" if pre_download else "main"
        branch_data = branch.get(branch_key)
        if not branch_data:
            raise RuntimeError(
                f"Branch '{branch_key}' not available for {game}/{region}"
            )

        # package_id и password берём из ветки — они разные для main и pre_download.
        package_id = branch_data["package_id"]
        password = branch_data["password"]

        # 2. Получаем build по этой версии.
        build_json = await cls.get_build(
            region, package_id, password, clean_version, pre_download
        )
        game_data = build_json["data"]

        # 3. Ищем нужную категорию (game, zh-cn, en-us и т.д.).
        manifest_info = None
        for m in game_data["manifests"]:
            if m["category_id"] == category_id:
                manifest_info = m
                break
        if manifest_info is None:
            raise ValueError(f"Category {category_id} not found in build")

        # 4. Собираем URL для чанков и манифеста.
        download_url = f"{manifest_info['chunk_download']['url_prefix']}/$0"
        if manifest_info["chunk_download"].get("url_suffix"):
            download_url += f"?{manifest_info['chunk_download']['url_suffix']}"

        manifest_url = (
            f"{manifest_info['manifest_download']['url_prefix']}"
            f"/{manifest_info['manifest']['id']}"
        )
        if manifest_info["manifest_download"].get("url_suffix"):
            manifest_url += f"?{manifest_info['manifest_download']['url_suffix']}"

        # 5. Качаем манифест, распаковываем zstd, парсим protobuf.
        session = await cls._get_client()
        async with session.get(manifest_url) as response:
            response.raise_for_status()
            compressed = await response.read()

        decompressed = zstd.ZstdDecompressor().decompress(compressed)
        manifest = SophonManifestProto()
        manifest.ParseFromString(decompressed)

        return manifest, download_url

    @classmethod
    async def close(cls):
        """Закрываем HTTP-сессию. Вызывается при выходе из программы."""
        if cls._client is not None:
            await cls._client.close()
            cls._client = None