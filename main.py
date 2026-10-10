# main.py
"""Интерактивный загрузчик игр Hoyo — скачивание, починка, обновление.
Поддерживает несколько языков интерфейса (китайский, английский, русский).
"""

import asyncio
import argparse
import os
from typing import List, Optional

from core.dispatch import Dispatch
from core.meta import Meta
from core.sophon import Sophon
from core.download import Downloader
from core.repair import FileRepair
from core.download_types import DownloadOptions
from core.sophon_manifest_pb2 import SophonManifestProto
from core.utils import format_size
from core.i18n import I18n, t, LANGUAGES, DEFAULT_LANG

# ---------------------------------------------------------------------------
# Игры и регионы. Всё держим в одном месте, чтобы потом не бегать по коду
# и не искать, где что записано. Добавишь новую игру — правишь только тут.
# ---------------------------------------------------------------------------
GAMES = ["hk4e", "hkrpg", "nap", "bh3"]

# Человекочитаемые названия игр на каждом языке. Ключ — код языка,
# значение — как игра называется для пользователя.
GAME_NAMES = {
    "hk4e": {"zh-CN": "原神", "en-US": "Genshin Impact", "ru-RU": "Genshin Impact"},
    "hkrpg": {"zh-CN": "崩坏：星穹铁道", "en-US": "Honkai: Star Rail", "ru-RU": "Honkai: Star Rail"},
    "nap": {"zh-CN": "绝区零", "en-US": "Zenless Zone Zero", "ru-RU": "Zenless Zone Zero"},
    "bh3": {"zh-CN": "崩坏3", "en-US": "Honkai Impact 3", "ru-RU": "Honkai Impact 3"},
}

# Регионы. OS — международный (глобалка), CN — китайский.
REGIONS = ["OS", "CN"]

# Как называть регионы на разных языках. Опять же, всё в одном словаре.
REGION_NAMES = {
    "OS": {"zh-CN": "国际服", "en-US": "Global", "ru-RU": "Глобальный"},
    "CN": {"zh-CN": "国服", "en-US": "China", "ru-RU": "Китай"},
}


class InteractiveDownloader:
    """Главный класс интерактивного загрузчика.

    Держит в себе downloader и repairer, плюс кэш версий, чтобы лишний раз
    не дёргать API, если пользователь вернулся в меню и выбрал то же самое.
    """

    def __init__(self):
        # Собственно загрузчик файлов. Умеет качать по Sophon-чанкам.
        self.downloader = Downloader()
        # Починщик — сравнивает локальные файлы с манифестом и докачивает битые.
        self.repairer = FileRepair()
        # Кэш версий: ключ "игра_регион" -> (versions, package_id, password, pre_password)
        # Живёт только на время сессии, между запусками не сохраняется.
        self.version_cache = {}

    # -----------------------------------------------------------------------
    # Мелкие утилиты для UI. Тут ничего сложного — просто чистим экран и
    # печатаем заголовки/меню, чтобы код команд не разрастался.
    # -----------------------------------------------------------------------

    def clear_screen(self):
        """Чистим консоль. На винде cls, на всём остальном clear."""
        os.system("cls" if os.name == "nt" else "clear")

    def print_header(self):
        """Печатаем шапку с названием программы. Сверху и снизу — разделители."""
        print("=" * 60)
        print(f"  {t('app.title')} - {t('app.subtitle')}")
        print("=" * 60)
        print()

    def print_menu(self):
        """Главное меню. Номера пунктов должны совпадать с тем, что мы
        потом ловим в run() — иначе пользователь нажмёт 5, а попадёт на выход.
        """
        print(f"{t('menu.prompt')}:")
        print(f"  1. {t('menu.download')}")
        print(f"  2. {t('menu.repair')}")
        print(f"  3. {t('menu.versions')}")
        print(f"  4. {t('menu.packages')}")
        print(f"  5. {t('menu.language')}")
        print(f"  6. {t('menu.exit')}")
        print()

    def get_int_input(self, prompt_key: str, default: int = None, min_val: int = 1) -> int:
        """Спрашиваем у пользователя целое число.

        prompt_key — ключ перевода для вопроса.
        default — что вернуть, если пользователь просто нажал Enter.
        min_val — минимально допустимое значение, защита от дурака.
        """
        prompt = t(prompt_key)
        if default is not None:
            # Показываем дефолт прямо в вопросе, чтобы не гадать.
            prompt = f"{prompt} [{t('download.output_default')}: {default}]"
        while True:
            try:
                raw = input(f"{prompt}: ").strip()
                # Пустой ввод — отдаём дефолт, если он есть.
                if not raw and default is not None:
                    return default
                num = int(raw)
                if num < min_val:
                    print(t("common.invalid_number"))
                    continue
                return num
            except ValueError:
                # Не число — говорим об этом и просим ещё раз.
                print(t("common.invalid_number"))

    def get_bool_input(self, prompt_key: str, default: bool = True) -> bool:
        """Спрашиваем да/нет. Понимаем y/yes/true/1 и n/no/false/0.

        Пустой ввод — возвращаем default. Всё остальное — ругаемся.
        """
        default_str = "Y/n" if default else "y/N"
        while True:
            raw = input(f"{t(prompt_key)} ({default_str}): ").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes", "true", "1"):
                return True
            if raw in ("n", "no", "false", "0"):
                return False
            print(t("common.invalid_choice"))

    # -----------------------------------------------------------------------
    # Выбор игры, региона, версии, пакетов. Тут основная логика интерфейса.
    # -----------------------------------------------------------------------

    async def select_game(self) -> str:
        """Показываем список игр и ждём, пока пользователь ткнёт номер.

        Возвращаем код игры (hk4e/hkrpg/nap/bh3), а не её название.
        """
        print(f"\n{t('game.prompt')}:")
        for i, game in enumerate(GAMES, 1):
            # Название берём на текущем языке интерфейса.
            print(f"  {i}. {GAME_NAMES[game][I18n.current()]}")
        while True:
            try:
                choice = input(f"\n{t('game.select')}: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(GAMES):
                    return GAMES[idx]
                # Ввели число, но не из диапазона.
                print(t("game.invalid", max=len(GAMES)))
            except ValueError:
                # Ввели не число.
                print(t("common.invalid_number"))

    async def select_region(self) -> str:
        """Выбор региона: OS (глобалка) или CN (китайский).

        Важно: регион потом уходит во все запросы к API. Если тут накосячить,
        можно случайно скачать не то, что хотел.
        """
        print(f"\n{t('region.prompt')}:")
        for i, region in enumerate(REGIONS, 1):
            print(f"  {i}. {REGION_NAMES[region][I18n.current()]}")
        while True:
            try:
                choice = input(f"\n{t('region.select')}: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(REGIONS):
                    return REGIONS[idx]
                print(t("common.invalid_choice"))
            except ValueError:
                print(t("common.invalid_number"))

    async def select_version(self, game: str, region: str):
        """Выбор версии игры.

        Возвращаем кортеж:
            version        — как показываем пользователю (может быть с пометкой pre-download)
            clean_version  — версия без пометки, для API
            is_pre         — флаг предзагрузки
            (package_id, password, pre_password) — данные для запроса манифеста

        Если пользователь ничего не выбрал (или версий нет) — вернём None-ы.
        """
        # Кэшируем по паре игра+регион, чтобы не долбить API повторно.
        cache_key = f"{game}_{region}"
        if cache_key not in self.version_cache:
            print(f"\n{t('version.fetching', game=GAME_NAMES[game][I18n.current()], region=REGION_NAMES[region][I18n.current()])}")
            try:
                versions, package_id, password, pre_password = await Meta.get_versions(game, region)
            except Exception as e:
                # Сеть отвалилась или API поменялся — говорим об этом и выходим.
                print(t("version.fetch_error", error=e))
                return None, None, None, None
            self.version_cache[cache_key] = (versions, package_id, password, pre_password)
        else:
            versions, package_id, password, pre_password = self.version_cache[cache_key]

        # Пустой список — значит API ничего не отдал, дальше идти некуда.
        if not versions:
            print(t("version.none"))
            return None, None, None, None

        # Печатаем список. Последний пункт — ручной ввод версии.
        print(f"\n{t('version.prompt')} ({len(versions)}):")
        for i, v in enumerate(versions, 1):
            print(f"  {i}. {v}")
        print(f"  {len(versions) + 1}. {t('version.manual')}")

        while True:
            raw = input(f"\n{t('version.select')}: ").strip()
            if not raw:
                # Пустой ввод — не хотим угадывать, просим ещё раз.
                print(t("version.invalid"))
                continue

            # Если ввели не число — считаем, что это версия, введённая руками.
            if not raw.isdigit():
                version = raw
                break

            idx = int(raw) - 1
            if 0 <= idx < len(versions):
                # Нормальный выбор из списка.
                version = versions[idx]
                break
            elif idx == len(versions):
                # Пользователь выбрал "ручной ввод".
                manual = input(f"{t('version.manual.prompt')}: ").strip()
                if manual:
                    version = manual
                    break
                # Пусто — возвращаемся к выбору.
                continue
            else:
                print(t("common.invalid_choice"))

        # Помечаем pre-download и отрезаем пометку для API.
        is_pre = version.endswith(" (pre-download)")
        clean = version.replace(" (pre-download)", "")
        return version, clean, is_pre, (package_id, password, pre_password)

    async def select_categories(self, game: str, region: str, version: str,
                                package_id: str, password: str) -> List[str]:
        """Выбор пакетов для скачивания.

        Пакеты — это category_id из API. Например, "game" — сама игра,
        "zh-cn" — китайская озвучка, и т.д.

        Понимаем такие шорткаты:
            all    — взять всё
            game   — только игровые ресурсы
            audio  — только озвучки
            числа через запятую — конкретные пакеты по номерам
        """
        print(f"\n{t('package.fetching')}")
        packages = await Meta.get_packages(region, version, package_id, password)

        if not packages:
            print(t("package.none"))
            return []

        # Печатаем список с пометкой типа пакета.
        print(f"\n{t('package.found')} ({len(packages)}):")
        for i, pkg in enumerate(packages, 1):
            matching = pkg[1]
            if matching == "game":
                type_label = t("package.type.game")
            elif matching in ("zh-cn", "en-us", "ja-jp", "ko-kr"):
                type_label = t("package.type.audio")
            else:
                type_label = t("package.type.resource")
            print(f"  {i}. [{type_label}] {pkg[2]} (ID: {pkg[0]}, matching: {pkg[1]})")

        # Подсказки по вводу.
        print(f"\n{t('package.select')}")
        print(f"  {t('package.example')}")
        print(f"  {t('package.all')}")
        print(f"  {t('package.game')}")
        print(f"  {t('package.audio')}")

        while True:
            raw = input(f"{t('package.prompt')}: ").strip().lower()

            # Шорткат "all".
            if raw == "all":
                return [p[0] for p in packages]

            # Шорткат "game".
            if raw == "game":
                sel = [p[0] for p in packages if p[1] == "game"]
                if sel:
                    return sel
                # Нет игрового пакета — не молчим, говорим пользователю.
                print(t("package.invalid"))
                continue

            # Шорткат "audio".
            if raw == "audio":
                sel = [p[0] for p in packages if p[1] in ("zh-cn", "en-us", "ja-jp", "ko-kr")]
                if sel:
                    return sel
                print(t("package.invalid"))
                continue

            # Пользователь мог ввести китайскую запятую — заменяем на обычную.
            raw = raw.replace("，", ",")
            try:
                # Разбираем список номеров.
                indices = [int(x.strip()) for x in raw.split(",") if x.strip()]
                # Берём соответствующие category_id, отсекая выход за границы.
                selected = [packages[i - 1][0] for i in indices if 1 <= i <= len(packages)]
                if selected:
                    return selected
                print(t("package.invalid"))
            except ValueError:
                print(t("common.invalid_number"))

    # -----------------------------------------------------------------------
    # Команды главного меню. Каждая — отдельный сценарий.
    # -----------------------------------------------------------------------

    async def cmd_download(self):
        """Скачивание игры с нуля.

        Спрашиваем игру, регион, версию, пакеты, куда класть файлы.
        Потом тянем манифесты и запускаем загрузчик.
        """
        self.clear_screen()
        self.print_header()
        print(f">>> {t('download.title')}\n")

        game = await self.select_game()
        region = await self.select_region()

        version, clean_version, is_pre, pkg_info = await self.select_version(game, region)
        if not version:
            # Пользователь не выбрал версию — выходим тихо.
            return
        package_id, password, pre_password = pkg_info

        selected = await self.select_categories(game, region, clean_version, package_id, password)
        if not selected:
            return

        # Куда складывать. По умолчанию — папка с именем игры в текущей директории.
        default_dir = os.path.join(os.getcwd(), game)
        out_raw = input(f"\n{t('download.output_dir')} [{t('download.output_default')}: {default_dir}]: ").strip()
        output_dir = out_raw or default_dir

        # Параметры загрузки.
        parallel = self.get_int_input("download.parallel", default=8)
        retries = self.get_int_input("download.retries", default=4)
        verify = self.get_bool_input("download.verify", default=True)

        # Показываем, что собираемся делать, и просим подтверждение.
        print(f"\n{t('download.preparing', game=GAME_NAMES[game][I18n.current()], version=version)}")
        print(f"{t('download.packages')}: {', '.join(selected)}")
        print(f"{t('download.output')}: {output_dir}")

        if not self.get_bool_input("download.confirm", default=True):
            print(t("download.cancelled"))
            return

        # Собираем ассеты из всех выбранных пакетов в один список.
        all_assets = []
        all_download_url = None

        for category_id in selected:
            print(f"\n--- {t('download.fetching_manifest')}: {category_id} ---")
            manifest, download_url = await Sophon.get_manifest(
                game, version, region, category_id
            )
            print(t("download.files_in_package", count=len(manifest.Assets)))
            all_assets.extend(manifest.Assets)
            # URL для чанков один и тот же для всех категорий, запоминаем последний.
            all_download_url = download_url

        if not all_assets:
            print(t("download.no_files"))
            return

        print(f"\n{t('download.total_files', count=len(all_assets))}")

        # Склеиваем всё в один манифест — загрузчику так удобнее.
        combined = SophonManifestProto()
        combined.Assets = all_assets

        options = DownloadOptions(
            max_parallel_files=parallel,
            max_attempts_per_file=retries,
            verify_existing_files=verify,
        )

        # Погнали.
        result = await self.downloader.download_files_async(
            combined.Assets, all_download_url, output_dir,
            progress_callback=None, options=options,
        )

        # Итог.
        print()
        if result.all_succeeded:
            print(f"✅ {t('download.done')}")
        else:
            print(f"⚠️ {t('download.done_with_failures', count=len(result.failures))}")
            for f in result.failures:
                print(f"  - {t('download.failed_item', name=f.asset_name, reason=f.reason)}")

    async def cmd_repair(self):
        """Починка или обновление уже установленной игры.

        Сканируем папку, сравниваем с манифестом, докачиваем только битое.
        Есть режим dry-run: только показать, что нужно чинить, без загрузки.
        """
        self.clear_screen()
        self.print_header()
        print(f">>> {t('repair.title')}\n")

        game = await self.select_game()
        region = await self.select_region()

        version, clean_version, is_pre, pkg_info = await self.select_version(game, region)
        if not version:
            return
        package_id, password, pre_password = pkg_info

        packages = await Meta.get_packages(region, clean_version, package_id, password)

        # По умолчанию чиним игру и китайскую озвучку — это самый частый случай.
        default_categories = []
        for pkg in packages:
            if pkg[1] == "game":
                default_categories.append(pkg[0])
            elif pkg[1] == "zh-cn":
                default_categories.append(pkg[0])
        # Если ничего не нашли — берём хоть что-то, чтобы не упасть.
        if not default_categories:
            default_categories = [packages[0][0]]

        print(f"\n{t('repair.default_packages')}: {', '.join(default_categories)}")
        if self.get_bool_input("repair.custom_packages", default=False):
            selected = await self.select_categories(game, region, clean_version, package_id, password)
        else:
            selected = default_categories

        if not selected:
            return

        # Куда чинить. Тут уже без дефолта — путь к игре пользователь знает лучше.
        path = input(f"{t('repair.game_path')}: ").strip()
        if not path:
            print(f"❌ {t('repair.path_required')}")
            return
        if not os.path.exists(path):
            print(f"⚠️ {t('repair.path_missing', path=path)}")
            if not self.get_bool_input("repair.create_dir", default=True):
                return
            os.makedirs(path, exist_ok=True)

        # Параметры.
        parallel = self.get_int_input("download.parallel", default=8)
        retries = self.get_int_input("download.retries", default=4)
        verify = self.get_bool_input("repair.verify_hash", default=True)
        dry_run = self.get_bool_input("repair.dry_run", default=False)

        print(f"\n{t('repair.preparing', game=GAME_NAMES[game][I18n.current()], version=version)}")
        print(f"{t('download.packages')}: {', '.join(selected)}")
        print(f"{t('download.output')}: {path}")
        if dry_run:
            print(f"{t('repair.dry_run')}")

        if not self.get_bool_input("repair.confirm", default=True):
            print(t("repair.cancelled"))
            return

        # Тянем манифесты так же, как при скачивании.
        all_assets = []
        all_download_url = None
        for category_id in selected:
            print(f"\n--- {t('download.fetching_manifest')}: {category_id} ---")
            manifest, download_url = await Sophon.get_manifest(
                game, version, region, category_id
            )
            print(t("download.files_in_package", count=len(manifest.Assets)))
            all_assets.extend(manifest.Assets)
            all_download_url = download_url

        if not all_assets:
            print(t("download.no_files"))
            return

        print(f"\n{t('download.total_files', count=len(all_assets))}")

        combined = SophonManifestProto()
        combined.Assets = all_assets

        options = DownloadOptions(
            max_parallel_files=parallel,
            max_attempts_per_file=retries,
            verify_existing_files=verify,
        )

        # Отдаём всё репейреру, он сам разберётся, что чинить.
        report, result = await self.repairer.repair_files(
            combined, all_download_url, path,
            progress_callback=None, options=options,
            verify_hash=verify, dry_run=dry_run,
        )

        # Печатаем итог. Для dry-run — только статистику, без загрузки.
        print()
        if dry_run:
            print(f"✅ {t('repair.scan_done')}")
            print(f"{t('repair.need_repair')}: {report.needs_repair_count}")
            print(f"{t('repair.need_download_size')}: {format_size(report.total_size_to_download)}")
        elif result and result.all_succeeded:
            print(f"✅ {t('repair.done')}")
        elif result:
            print(f"⚠️ {t('repair.done_with_failures', count=len(result.failures))}")
            for f in result.failures:
                print(f"  - {t('download.failed_item', name=f.asset_name, reason=f.reason)}")
        else:
            print(f"✅ {t('repair.no_repair_needed')}")

    async def cmd_versions(self):
        """Просто показать список доступных версий и данные для API.

        Ничего не качает, только читает getGameBranches.
        """
        self.clear_screen()
        self.print_header()
        print(f">>> {t('versions.title')}\n")

        game = await self.select_game()
        region = await self.select_region()

        print(f"\n{t('versions.fetching', game=GAME_NAMES[game][I18n.current()], region=REGION_NAMES[region][I18n.current()])}")
        versions, package_id, password, pre_password = await Meta.get_versions(game, region)

        # Показываем package_id и пароли — полезно при отладке.
        print(f"\n📦 {t('versions.package_id')}: {package_id}")
        print(f"🔑 {t('versions.password')}: {password}")
        if pre_password:
            print(f"🔑 {t('versions.pre_password')}: {pre_password}")

        print(f"\n📋 {t('versions.list')} ({len(versions)}):")
        for v in versions:
            print(f"  - {v}")

        print()
        input(t("common.back"))

    async def cmd_packages(self):
        """Показать список пакетов для выбранной версии.

        Тоже ничего не качает, просто печатает category_id и названия.
        """
        self.clear_screen()
        self.print_header()
        print(f">>> {t('packages.title')}\n")

        game = await self.select_game()
        region = await self.select_region()

        version, clean_version, is_pre, pkg_info = await self.select_version(game, region)
        if not version:
            return
        package_id, password, pre_password = pkg_info

        print(f"\n{t('packages.fetching')}")
        packages = await Meta.get_packages(region, clean_version, package_id, password)

        print(f"\n📦 {t('packages.list')} ({len(packages)}):")
        for pkg in packages:
            # Определяем тип пакета по matching_field.
            if pkg[1] == "game":
                type_label = t("package.type.game")
            elif pkg[1] in ("zh-cn", "en-us", "ja-jp", "ko-kr"):
                type_label = t("package.type.audio")
            else:
                type_label = t("package.type.resource")
            print(f"  [{type_label}] ID: {pkg[0]} | {pkg[2]} (matching: {pkg[1]})")

        print()
        input(t("common.back"))

    async def cmd_language(self):
        """Смена языка интерфейса.

        Перезагружает JSON с переводами и возвращается в меню.
        """
        self.clear_screen()
        self.print_header()
        print(f">>> {t('menu.language')}\n")
        print(t("lang.prompt") + ":")
        for i, (code, key) in enumerate(LANGUAGES.items(), 1):
            print(f"  {i}. {t(key)}")
        while True:
            try:
                choice = input(f"\n{t('lang.select')}: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(LANGUAGES):
                    new_lang = list(LANGUAGES.keys())[idx]
                    if I18n.load(new_lang):
                        print(f"✅ {t('lang.prompt')}: {t(LANGUAGES[new_lang])}")
                    break
                print(t("common.invalid_choice"))
            except ValueError:
                print(t("common.invalid_number"))
        input(t("common.continue"))

    # -----------------------------------------------------------------------
    # Главный цикл. Сначала выбор языка, потом меню до победного.
    # -----------------------------------------------------------------------

    async def run(self):
        """Точка входа в интерактив. Крутится, пока пользователь не выйдет."""
        # Первым делом — выбор языка. Показываем все варианты сразу,
        # без переводов, потому что язык ещё не выбран.
        self.clear_screen()
        print("=" * 60)
        print("  Hoyo Game Downloader")
        print("=" * 60)
        print()
        print("Select language / 选择语言 / Выберите язык:")
        for i, (code, key) in enumerate(LANGUAGES.items(), 1):
            print(f"  {i}. {t(key)}")
        while True:
            try:
                choice = input("\n> ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(LANGUAGES):
                    I18n.load(list(LANGUAGES.keys())[idx])
                    break
                print("Invalid choice")
            except ValueError:
                print("Invalid choice")

        # Основной цикл меню.
        while True:
            self.clear_screen()
            self.print_header()
            self.print_menu()

            choice = input(f"{t('menu.prompt')} (1-6): ").strip()

            if choice == "1":
                await self.cmd_download()
            elif choice == "2":
                await self.cmd_repair()
            elif choice == "3":
                await self.cmd_versions()
            elif choice == "4":
                await self.cmd_packages()
            elif choice == "5":
                # Смена языка — отдельная ветка, после неё сразу в меню.
                await self.cmd_language()
                continue
            elif choice == "6":
                # Выход: закрываем все сетевые сессии и прощаемся.
                print(f"\n👋 {t('common.goodbye')}")
                await self.downloader.close()
                await self.repairer.close()
                await Dispatch.close()
                await Sophon.close()
                break
            else:
                print(t("common.invalid_choice"))
                await asyncio.sleep(1)

            # Пауза после команды, чтобы пользователь успел прочитать вывод.
            if choice not in ("6",):
                input(f"\n{t('common.continue')}")


def main():
    """Точка входа. Разбираем аргументы и запускаем либо CLI, либо интерактив."""
    parser = argparse.ArgumentParser(
        description="Hoyo Game Downloader - Interactive Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cli", action="store_true", help="CLI mode (non-interactive)")
    args = parser.parse_args()

    if args.cli:
        # CLI-режим живёт в отдельном файле, если его нет — говорим об этом.
        try:
            from main_cli import cli_main
            cli_main()
        except ImportError:
            print("CLI mode file main_cli.py not found.")
    else:
        # Язык по умолчанию. Загружается до того, как пользователь выберет свой.
        I18n.load(DEFAULT_LANG)
        app = InteractiveDownloader()
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            # Ctrl+C — нормальный способ выйти, не пугаем стектрейсом.
            print(f"\n\n👋 {t('common.goodbye')}")
        except Exception as e:
            # Всё остальное — показываем как ошибку.
            print(f"\n❌ {t('common.error', error=e)}")


if __name__ == "__main__":
    main()