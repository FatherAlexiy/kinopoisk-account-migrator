#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException

from kp_common import (
    LOGIN_URL,
    RatedMovie,
    SimpleMovie,
    build_output_dir,
    create_driver,
    normalize_movie_url,
)


DEFAULT_MAX_PAGES = 500
PAGE_LOAD_DELAY_SEC = 1.5


def prompt_manual_login(driver: webdriver.Chrome) -> None:
    print("\n[1/5] Открываю Кинопоиск для ручного входа...")
    driver.get(LOGIN_URL)
    print(
        "Войдите в нужный аккаунт в открывшемся окне браузера.\n"
        "После входа вручную откройте страницу своего профиля\n"
        "(в URL должно быть что-то вроде https://www.kinopoisk.ru/user/<id>/ )\n"
        "и вернитесь в терминал."
    )
    input("Нажмите Enter после того, как будете на странице профиля... ")


def detect_user_id(driver: webdriver.Chrome) -> str:
    current = driver.current_url
    match = re.search(r"/user/(\d+)/", current)
    if match:
        return match.group(1)

    page = driver.page_source
    match = re.search(r"/user/(\d+)/", page)
    if match:
        return match.group(1)

    raise RuntimeError(
        "Не удалось определить user_id. Откройте страницу профиля вида "
        "https://www.kinopoisk.ru/user/<id>/ и запустите скрипт снова."
    )


def soup_from_driver(driver: webdriver.Chrome) -> BeautifulSoup:
    return BeautifulSoup(driver.page_source, "html.parser")


def select_links(soup: BeautifulSoup, selectors: Sequence[str]) -> List[str]:
    for selector in selectors:
        nodes = soup.select(selector)
        links = [n.get("href", "") for n in nodes if n.get("href")]
        if links:
            return links
    return []


def parse_ratings_page(soup: BeautifulSoup) -> List[RatedMovie]:
    items: List[RatedMovie] = []
    for row in soup.select("div.profileFilmsList div.item"):
        link_node = row.select_one("div.nameRus > a")
        rating_node = row.select_one(".myVote")
        if not link_node or not rating_node:
            continue
        href = link_node.get("href", "")
        rating = rating_node.get_text(strip=True)
        if href and re.fullmatch(r"\d{1,2}", rating) and 1 <= int(rating) <= 10:
            items.append(RatedMovie(url=normalize_movie_url(href), rating=rating))
    return items


def parse_simple_page(soup: BeautifulSoup) -> List[SimpleMovie]:
    links = select_links(
        soup,
        selectors=[
            "div.info > div > font > a",
            "a.name",
            "div.nameRus > a",
            "div.info a[href*='/film/']",
        ],
    )
    return [SimpleMovie(url=normalize_movie_url(url)) for url in links]


def scrape_paginated(
    driver: webdriver.Chrome,
    url_template: str,
    parser: Callable[[BeautifulSoup], List],
    label: str,
    max_pages: int,
) -> List:
    results: List = []
    seen_keys = set()

    for page_num in range(1, max_pages + 1):
        page_url = url_template.format(page_num)
        print(f"[{label}] page {page_num}: {page_url}")
        driver.get(page_url)
        time.sleep(PAGE_LOAD_DELAY_SEC)
        soup = soup_from_driver(driver)
        page_items = parser(soup)

        if not page_items:
            break

        new_on_page = 0
        for item in page_items:
            key = tuple(asdict(item).items())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(item)
            new_on_page += 1

        if new_on_page == 0:
            break

    return results


def export_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(path: Path, rows: List[dict], fieldnames: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_export(
    chromedriver: Optional[str] = None,
    chrome_binary: Optional[str] = None,
    profile_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Tuple[int, Optional[Path]]:
    out_dir = build_output_dir(output_dir, "kinopoisk_export")

    try:
        driver = create_driver(chromedriver, chrome_binary, profile_dir)
    except WebDriverException as e:
        print("Не удалось запустить Chrome через Selenium.")
        print(f"Причина: {e}")
        print(
            "Проверьте, что установлен Chrome/Chromium, selenium, а также доступен chromedriver "
            "или Selenium Manager может его скачать автоматически."
        )
        return 2, None

    rated_movies: List[RatedMovie] = []
    watched: List[SimpleMovie] = []
    watchlist: List[SimpleMovie] = []
    user_id = "unknown"

    def _save_results(interrupted: bool = False) -> dict:
        rated_movies_rows = [asdict(x) for x in rated_movies]
        watched_rows = [asdict(x) for x in watched]
        watchlist_rows = [asdict(x) for x in watchlist]

        summary = {
            "exported_at": datetime.now().isoformat(),
            "user_id": user_id,
            "interrupted": interrupted,
            "counts": {
                "rated_movies": len(rated_movies_rows),
                "watched": len(watched_rows),
                "watchlist": len(watchlist_rows),
            },
            "notes": [
                "Script is based on Kinopoisk page scraping via Selenium.",
                "Kinopoisk layout changes may require selector updates.",
                "rated_movies — фильмы с оценкой пользователя.",
                "watched — все просмотренные (включая без оценки).",
                "watchlist — список 'буду смотреть'.",
            ],
        }

        export_json(out_dir / "ratings.json", rated_movies_rows)
        export_json(out_dir / "watched.json", watched_rows)
        export_json(out_dir / "watchlist.json", watchlist_rows)
        export_json(out_dir / "summary.json", summary)

        export_csv(out_dir / "ratings.csv", rated_movies_rows, ["url", "rating"])
        export_csv(out_dir / "watched.csv", watched_rows, ["url"])
        export_csv(out_dir / "watchlist.csv", watchlist_rows, ["url"])

        bundle = {
            "summary": summary,
            "ratings": rated_movies_rows,
            "watched": watched_rows,
            "watchlist": watchlist_rows,
        }
        export_json(out_dir / "kinopoisk_export.json", bundle)
        return summary

    try:
        prompt_manual_login(driver)
        user_id = detect_user_id(driver)
        print(f"\n[2/5] Определён user_id: {user_id}")

        ratings_url = f"https://www.kinopoisk.ru/user/{user_id}/votes/list/ord/date/vs/vote/page/{{}}/#list"
        watched_url = f"https://www.kinopoisk.ru/user/{user_id}/votes/list/ord/date/page/{{}}/#list"
        watchlist_url = f"https://www.kinopoisk.ru/user/{user_id}/movies/list/sort/default/vector/desc/page/{{}}/#list"

        print("\n[3/5] Собираю оценки...")
        rated_movies = scrape_paginated(driver, ratings_url, parse_ratings_page, "ratings", max_pages)

        print("\n[4/5] Собираю просмотренные...")
        watched = scrape_paginated(driver, watched_url, parse_simple_page, "watched", max_pages)

        print("\n[5/5] Собираю 'буду смотреть'...")
        watchlist = scrape_paginated(driver, watchlist_url, parse_simple_page, "watchlist", max_pages)

        summary = _save_results(interrupted=False)

        print("\nЭкспорт завершён.")
        print(f"Файлы сохранены в: {out_dir}")
        print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
        return 0, out_dir

    except KeyboardInterrupt:
        print("\nОстановлено пользователем. Сохраняю собранные данные...")
        summary = _save_results(interrupted=True)
        print(f"Частичные данные сохранены в: {out_dir}")
        print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
        return 130, out_dir
    except Exception as e:
        print(f"\nОшибка: {e}")
        if rated_movies or watched or watchlist:
            print("Сохраняю частично собранные данные...")
            _save_results(interrupted=True)
            print(f"Частичные данные сохранены в: {out_dir}")
        return 1, None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export rated movies and watchlist ('буду смотреть') from Kinopoisk.")
    parser.add_argument("--chromedriver", help="Path to chromedriver binary. Optional on modern Selenium.")
    parser.add_argument("--chrome-binary", help="Path to Chrome/Chromium binary if autodetection fails.")
    parser.add_argument("--profile-dir", help="Chrome user-data dir to reuse an existing browser profile.")
    parser.add_argument("--output-dir", help="Where to store exported files.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Safety cap for pagination (default: {DEFAULT_MAX_PAGES}).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    code, _ = run_export(
        chromedriver=args.chromedriver,
        chrome_binary=args.chrome_binary,
        profile_dir=args.profile_dir,
        output_dir=args.output_dir,
        max_pages=args.max_pages,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
