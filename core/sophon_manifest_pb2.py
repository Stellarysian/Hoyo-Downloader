# core/sophon_manifest_pb2.py
"""Ручной парсер SophonManifestProto.

Настоящий protobuf-код сгенерирован не был, поэтому разбираем поля руками.
Формат простой: varint-теги + length-delimited для строк и вложенных сообщений.
"""

import struct
from typing import List, Optional


class SophonManifestAssetChunk:
    """Один чанк внутри файла.

    Чанк — это кусок данных, который качается отдельно и кладётся по смещению
    ChunkOnFileOffset внутри итогового файла.
    """

    def __init__(self):
        # Имя чанка в хранилище (обычно хэш).
        self.ChunkName: str = ""
        # MD5 распакованных данных чанка.
        self.ChunkDecompressedHashMd5: str = ""
        # Смещение внутри итогового файла, куда писать этот чанк.
        self.ChunkOnFileOffset: int = 0
        # Размер сжатых данных (то, что реально лежит на сервере).
        self.ChunkSize: int = 0
        # Размер после распаковки (то, что должно получиться в файле).
        self.ChunkSizeDecompressed: int = 0


class SophonManifestAssetProperty:
    """Один файл в манифесте.

    Если AssetType == 0 и AssetHashMd5 пустой — это директория, а не файл.
    """

    def __init__(self):
        # Путь файла относительно корня игры.
        self.AssetName: str = ""
        # Список чанков, из которых собирается файл.
        self.AssetChunks: List[SophonManifestAssetChunk] = []
        # Тип ассета. 0 — обычный файл, 64 — директория.
        self.AssetType: int = 0
        # Итоговый размер файла.
        self.AssetSize: int = 0
        # MD5 всего файла (может быть пустым).
        self.AssetHashMd5: str = ""


class SophonManifestProto:
    """Корневое сообщение манифеста. Внутри — только список Assets."""

    def __init__(self):
        self.Assets: List[SophonManifestAssetProperty] = []

    def ParseFromString(self, data: bytes):
        """Разбираем сырые байты в список ассетов.

        Идём по потоку, читаем тег, смотрим номер поля и тип. Поле 1 — это
        Assets, всё остальное пропускаем (нам оно не нужно).
        """
        self.Assets = []

        i = 0
        while i < len(data):
            # Читаем тег: старшие биты — номер поля, младшие 3 — тип.
            tag, i = self._read_varint(data, i)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if field_number == 1:  # Assets
                # Length-delimited: сначала длина, потом сами данные.
                length, i = self._read_varint(data, i)
                end = i + length
                asset = self._parse_asset(data, i, end)
                self.Assets.append(asset)
                i = end
            else:
                # Неизвестное поле — аккуратно пропускаем, чтобы не сломаться.
                if wire_type == 0:  # varint
                    _, i = self._read_varint(data, i)
                elif wire_type == 1:  # 64-бит
                    i += 8
                elif wire_type == 2:  # length-delimited
                    length, i = self._read_varint(data, i)
                    i += length
                elif wire_type == 5:  # 32-бит
                    i += 4
                else:
                    # Что-то незнакомое — дальше идти смысла нет.
                    break

    def _read_varint(self, data: bytes, pos: int) -> tuple:
        """Читаем varint из потока.

        Возвращаем (значение, новая_позиция). Varint — это число,
        у которого старший бит каждого байта означает "продолжение следует".
        """
        result = 0
        shift = 0
        while True:
            if pos >= len(data):
                # Данные кончились неожиданно — возвращаем что есть.
                return 0, pos
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                break
            shift += 7
        return result, pos

    def _parse_asset(self, data: bytes, start: int, end: int) -> SophonManifestAssetProperty:
        """Разбираем одно сообщение ассета в диапазоне [start, end)."""
        asset = SophonManifestAssetProperty()
        i = start

        while i < end:
            tag, i = self._read_varint(data, i)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if field_number == 1:  # AssetName
                length, i = self._read_varint(data, i)
                asset.AssetName = data[i:i + length].decode('utf-8')
                i += length
            elif field_number == 2:  # AssetChunks (повторяющееся)
                length, i = self._read_varint(data, i)
                chunk_end = i + length
                chunk = self._parse_chunk(data, i, chunk_end)
                asset.AssetChunks.append(chunk)
                i = chunk_end
            elif field_number == 3:  # AssetType
                value, i = self._read_varint(data, i)
                asset.AssetType = value
            elif field_number == 4:  # AssetSize
                value, i = self._read_varint(data, i)
                asset.AssetSize = value
            elif field_number == 5:  # AssetHashMd5
                length, i = self._read_varint(data, i)
                asset.AssetHashMd5 = data[i:i + length].decode('utf-8')
                i += length
            else:
                # Неизвестное поле — пропускаем по общим правилам.
                if wire_type == 0:
                    _, i = self._read_varint(data, i)
                elif wire_type == 1:
                    i += 8
                elif wire_type == 2:
                    length, i = self._read_varint(data, i)
                    i += length
                elif wire_type == 5:
                    i += 4
                else:
                    break

        return asset

    def _parse_chunk(self, data: bytes, start: int, end: int) -> SophonManifestAssetChunk:
        """Разбираем одно сообщение чанка в диапазоне [start, end)."""
        chunk = SophonManifestAssetChunk()
        i = start

        while i < end:
            tag, i = self._read_varint(data, i)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if field_number == 1:  # ChunkName
                length, i = self._read_varint(data, i)
                chunk.ChunkName = data[i:i + length].decode('utf-8')
                i += length
            elif field_number == 2:  # ChunkDecompressedHashMd5
                length, i = self._read_varint(data, i)
                chunk.ChunkDecompressedHashMd5 = data[i:i + length].decode('utf-8')
                i += length
            elif field_number == 3:  # ChunkOnFileOffset
                value, i = self._read_varint(data, i)
                chunk.ChunkOnFileOffset = value
            elif field_number == 4:  # ChunkSize
                value, i = self._read_varint(data, i)
                chunk.ChunkSize = value
            elif field_number == 5:  # ChunkSizeDecompressed
                value, i = self._read_varint(data, i)
                chunk.ChunkSizeDecompressed = value
            else:
                # Всё незнакомое — пропускаем.
                if wire_type == 0:
                    _, i = self._read_varint(data, i)
                elif wire_type == 1:
                    i += 8
                elif wire_type == 2:
                    length, i = self._read_varint(data, i)
                    i += length
                elif wire_type == 5:
                    i += 4
                else:
                    break

        return chunk