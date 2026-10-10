# core/meta.py
"""Метаданные игр: версии и пакеты.

Собираем информацию из официального API: какие версии бывают, какие
исторические версии доступны, какие пакеты есть в каждой версии.
"""

from typing import List, Tuple

from packaging import version as pkg_version

from .sophon import Sophon


class Meta:
    """Обёртка над getGameBranches и getBuild — только для чтения метаданных."""

    @classmethod
    async def get_versions(
        cls, game: str, region: str
    ) -> Tuple[List[str], str, str, str]:
        """Собираем список доступных версий игры.

        Возвращаем кортеж:
            versions      — список версий (может включать пометку pre-download)
            package_id    — id пакета для основной ветки
            password      — пароль для основной ветки
            pre_password  — пароль для предзагрузки (пусто, если её нет)

        Исторические версии берём из diff_tags — это то, что API отдаёт
        вместе с main.tag, но о чём легко забыть.
        """
        game_meta = await Sophon.get_game_branches(game, region)

        versions: List[str] = []
        package_id = ""
        password = ""
        pre_password = ""

        branches = game_meta.get("data", {}).get("game_branches", [])
        if not branches:
            # Пусто — значит API ничего не отдал, возвращаем пустоту.
            return [], "", "", ""

        for branch in branches:
            # --- основная ветка ---
            main = branch.get("main")
            if main:
                tag = main.get("tag")
                if tag:
                    versions.append(tag)

                # diff_tags — это как раз исторические версии, которые
                # можно запросить через getBuild с тегом.
                for dt in (main.get("diff_tags") or []):
                    if dt:
                        versions.append(dt)

                # Запоминаем package_id и password для основной ветки.
                if main.get("package_id"):
                    package_id = main["package_id"]
                if main.get("password"):
                    password = main["password"]

            # --- ветка предзагрузки ---
            pre = branch.get("pre_download")
            if pre:
                pre_tag = pre.get("tag")
                if pre_tag:
                    # Пометку добавляем, чтобы UI мог её отличить.
                    versions.append(f"{pre_tag} (pre-download)")
                if pre.get("password"):
                    pre_password = pre["password"]

        # Чистим дубликаты, сохраняя порядок.
        seen = set()
        unique: List[str] = []
        for v in versions:
            clean = v.replace(" (pre-download)", "")
            if not clean or clean in seen:
                continue
            seen.add(clean)
            unique.append(v)

        # Сортируем от новых к старым.
        def version_key(v: str):
            clean = v.replace(" (pre-download)", "")
            try:
                return pkg_version.parse(clean)
            except Exception:
                # Если версия нестандартная — отодвигаем её в конец.
                return pkg_version.parse("0.0.0")

        unique.sort(key=version_key, reverse=True)
        return unique, package_id, password, pre_password

    @classmethod
    async def get_packages(
        cls, region: str, version: str, package_id: str, password: str
    ) -> List[List[str]]:
        """Получаем список пакетов для конкретной версии.

        Пакет — это category_id + matching_field + название. Например,
        "game" — сама игра, "zh-cn" — китайская озвучка.
        """
        # Отрезаем пометку pre-download, если она есть.
        pre_download = False
        clean_version = version
        if version.endswith(" (pre-download)"):
            pre_download = True
            clean_version = version.replace(" (pre-download)", "")

        build = await Sophon.get_build(
            region, package_id, password, clean_version, pre_download
        )
        return cls._extract_packages(build)

    @classmethod
    def _extract_packages(cls, build: dict) -> List[List[str]]:
        """Вытаскиваем из build JSON список пакетов в удобном виде."""
        packages = []
        for package in build["data"]["manifests"]:
            packages.append([
                package["category_id"],
                package["matching_field"],
                package["category_name"],
            ])
        return packages