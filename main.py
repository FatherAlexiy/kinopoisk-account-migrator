#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from kinopoisk_export import DEFAULT_MAX_PAGES, run_export
from kinopoisk_import import DEFAULT_ITEM_DELAY_SEC, run_import


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kinopoisk Migrator: экспорт и/или импорт данных аккаунта.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--mode",
        choices=["all", "export", "import"],
        default="all",
        help="Режим работы: all (по умолчанию), export или import.",
    )

    # --- Общие параметры браузера ---
    browser = parser.add_argument_group("Браузер (общие)")
    browser.add_argument("--chromedriver", help="Путь к chromedriver. Опционально на современном Selenium.")
    browser.add_argument("--chrome-binary", help="Путь к бинарнику Chrome/Chromium.")

    # --- Параметры браузера для экспорта ---
    export_browser = parser.add_argument_group("Браузер для экспорта")
    export_browser.add_argument("--export-profile-dir", help="Chrome user-data-dir для ИСХОДНОГО аккаунта.")

    # --- Параметры браузера для импорта ---
    import_browser = parser.add_argument_group("Браузер для импорта")
    import_browser.add_argument("--import-profile-dir", help="Chrome user-data-dir для ЦЕЛЕВОГО аккаунта.")

    # --- Параметры экспорта ---
    exp = parser.add_argument_group("Экспорт")
    exp.add_argument("--export-output-dir", help="Куда сохранять файлы экспорта.")
    exp.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Максимум страниц пагинации при экспорте (default: {DEFAULT_MAX_PAGES}).",
    )

    # --- Параметры импорта ---
    imp = parser.add_argument_group("Импорт")
    imp.add_argument(
        "--input",
        help=(
            "Путь к kinopoisk_export.json или директории с ratings.json/watched.json/watchlist.json. "
            "Обязателен при --mode import. При --mode all определяется автоматически из результата экспорта."
        ),
    )
    imp.add_argument("--import-output-dir", help="Куда сохранять отчёт и состояние импорта.")
    imp.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_ITEM_DELAY_SEC,
        help=f"Задержка между элементами при импорте, секунды (default: {DEFAULT_ITEM_DELAY_SEC}).",
    )
    imp.add_argument("--skip-ratings", action="store_true", help="Не импортировать оценки.")
    imp.add_argument("--skip-watched", action="store_true", help="Не импортировать просмотренное.")
    imp.add_argument("--skip-watchlist", action="store_true", help="Не импортировать 'буду смотреть'.")
    imp.add_argument("--dry-run", action="store_true", help="Только показать количество записей без реального импорта.")
    imp.add_argument("--no-resume", action="store_true", help="Игнорировать сохранённое состояние и начать импорт заново.")

    args = parser.parse_args()

    if args.mode == "import" and not args.input:
        parser.error("--input обязателен при --mode import")

    if args.delay < 0:
        parser.error("--delay не может быть отрицательным")

    return args


def main() -> int:
    args = parse_args()

    export_profile = args.export_profile_dir
    import_profile = args.import_profile_dir

    if args.mode == "export":
        print("=" * 60)
        print("РЕЖИМ: только экспорт")
        print("=" * 60)
        code, _ = run_export(
            chromedriver=args.chromedriver,
            chrome_binary=args.chrome_binary,
            profile_dir=export_profile,
            output_dir=args.export_output_dir,
            max_pages=args.max_pages,
        )
        return code

    if args.mode == "import":
        print("=" * 60)
        print("РЕЖИМ: только импорт")
        print("=" * 60)
        return run_import(
            input_path=args.input,
            chromedriver=args.chromedriver,
            chrome_binary=args.chrome_binary,
            profile_dir=import_profile,
            output_dir=args.import_output_dir,
            delay=args.delay,
            skip_ratings=args.skip_ratings,
            skip_watched=args.skip_watched,
            skip_watchlist=args.skip_watchlist,
            dry_run=args.dry_run,
            no_resume=args.no_resume,
        )

    print("=" * 60)
    print("РЕЖИМ: полная миграция (экспорт → импорт)")
    print("=" * 60)
    print("\nШАГ 1/2 — Экспорт из исходного аккаунта")
    print("-" * 60)

    export_code, export_dir = run_export(
        chromedriver=args.chromedriver,
        chrome_binary=args.chrome_binary,
        profile_dir=export_profile,
        output_dir=args.export_output_dir,
        max_pages=args.max_pages,
    )

    if export_code != 0:
        print(f"\nЭкспорт завершился с ошибкой (код {export_code}). Импорт отменён.")
        return export_code

    import_input = str(args.input) if args.input else str(export_dir)
    print(f"\nДанные для импорта: {import_input}")
    print("\nШАГ 2/2 — Импорт в целевой аккаунт")
    print("-" * 60)

    import_code = run_import(
        input_path=import_input,
        chromedriver=args.chromedriver,
        chrome_binary=args.chrome_binary,
        profile_dir=import_profile,
        output_dir=args.import_output_dir,
        delay=args.delay,
        skip_ratings=args.skip_ratings,
        skip_watched=args.skip_watched,
        skip_watchlist=args.skip_watchlist,
        dry_run=args.dry_run,
        no_resume=args.no_resume,
    )

    if import_code == 0:
        print("\nМиграция успешно завершена.")
    else:
        print(f"\nИмпорт завершился с ошибкой (код {import_code}).")

    return import_code


if __name__ == "__main__":
    sys.exit(main())
