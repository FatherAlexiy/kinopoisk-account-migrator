#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple, Union

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from kp_common import (
    LOGIN_URL,
    RatedMovie,
    SimpleMovie,
    build_output_dir,
    create_driver,
    normalize_movie_url,
)


ACTION_TIMEOUT_SEC = 6
POST_CLICK_DELAY_SEC = 0.8
DEFAULT_ITEM_DELAY_SEC = 1.0
REPORT_FILENAME = "import_report.json"
STATE_FILENAME = "import_state.json"


@dataclass
class ImportState:
    ratings_done: Set[str] = field(default_factory=set)
    watched_done: Set[str] = field(default_factory=set)
    watchlist_done: Set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "ratings_done": sorted(self.ratings_done),
            "watched_done": sorted(self.watched_done),
            "watchlist_done": sorted(self.watchlist_done),
        }

    @classmethod
    def from_json(cls, payload: dict) -> "ImportState":
        return cls(
            ratings_done=set(payload.get("ratings_done", [])),
            watched_done=set(payload.get("watched_done", [])),
            watchlist_done=set(payload.get("watchlist_done", [])),
        )


def load_state(path: Path) -> ImportState:
    if not path.exists():
        return ImportState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"WARNING: не удалось загрузить состояние из {path}: {e}\n"
            "Состояние будет сброшено. Используйте --no-resume чтобы подавить это предупреждение.",
            file=sys.stderr,
        )
        return ImportState()
    return ImportState.from_json(payload)


def save_state(path: Path, state: ImportState) -> None:
    path.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


def save_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def dedupe_ratings(items: Iterable[dict]) -> List[RatedMovie]:
    seen: Set[str] = set()
    result: List[RatedMovie] = []
    for item in items:
        url = normalize_movie_url(str(item.get("url", "")).strip())
        rating = str(item.get("rating", "")).strip()
        if not url:
            print(f"WARNING: пропущена запись без URL: {item}", file=sys.stderr)
            continue
        if not rating or not rating.isdigit() or not (1 <= int(rating) <= 10):
            print(f"WARNING: пропущена запись с невалидной оценкой {rating!r}: {url}", file=sys.stderr)
            continue
        if url in seen:
            print(f"WARNING: дубликат URL в ratings, пропускаем: {url}", file=sys.stderr)
            continue
        seen.add(url)
        result.append(RatedMovie(url=url, rating=rating))
    return result


def dedupe_simple(items: Iterable[dict]) -> List[SimpleMovie]:
    seen: Set[str] = set()
    result: List[SimpleMovie] = []
    for item in items:
        url = normalize_movie_url(str(item.get("url", "")).strip())
        if not url:
            print(f"WARNING: пропущена запись без URL: {item}", file=sys.stderr)
            continue
        if url in seen:
            print(f"WARNING: дубликат URL, пропускаем: {url}", file=sys.stderr)
            continue
        seen.add(url)
        result.append(SimpleMovie(url=url))
    return result


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Невалидный JSON в файле {path}: {e}") from e


def load_input(path_str: str) -> Tuple[List[RatedMovie], List[SimpleMovie], List[SimpleMovie]]:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"Путь не найден: {path}")

    if path.is_file():
        payload = _read_json(path)
        if not isinstance(payload, dict):
            raise ValueError(
                f"Ожидался JSON-объект (dict) в {path}, получен {type(payload).__name__}. "
                "Убедитесь, что передан bundle-файл kinopoisk_export.json, а не ratings.json/watched.json."
            )
        ratings = dedupe_ratings(payload.get("ratings", []))
        watched = dedupe_simple(payload.get("watched", []))
        watchlist = dedupe_simple(payload.get("watchlist", []))
        return ratings, watched, watchlist

    ratings_path = path / "ratings.json"
    watched_path = path / "watched.json"
    watchlist_path = path / "watchlist.json"

    ratings = dedupe_ratings(_read_json(ratings_path) if ratings_path.exists() else [])
    watched = dedupe_simple(_read_json(watched_path) if watched_path.exists() else [])
    watchlist = dedupe_simple(_read_json(watchlist_path) if watchlist_path.exists() else [])
    return ratings, watched, watchlist


def wait_body(driver: webdriver.Chrome) -> None:
    WebDriverWait(driver, ACTION_TIMEOUT_SEC).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def open_movie(driver: webdriver.Chrome, url: str) -> None:
    driver.get(url)
    wait_body(driver)
    time.sleep(POST_CLICK_DELAY_SEC)


def maybe_close_popups(driver: webdriver.Chrome) -> None:
    xpaths = [
        "//button[contains(., 'Понятно')]",
        "//button[contains(., 'Ок')]",
        "//button[contains(., 'OK')]",
        "//button[contains(., 'Закрыть')]",
        "//div[@role='dialog']//button",
    ]
    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for elem in elements[:2]:
                if elem.is_displayed():
                    js_click(driver, elem)
                    time.sleep(0.3)
                    return
        except Exception:
            continue


def js_click(driver: webdriver.Chrome, element: WebElement) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    driver.execute_script("arguments[0].click();", element)


def find_first(
    driver: webdriver.Chrome,
    selectors: Sequence[Tuple[str, str]],
    timeout: float = ACTION_TIMEOUT_SEC,
) -> Optional[WebElement]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for mode, selector in selectors:
            try:
                if mode == "css":
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                elif mode == "xpath":
                    elements = driver.find_elements(By.XPATH, selector)
                else:
                    raise ValueError(f"Unsupported selector mode: {mode}")

                for elem in elements:
                    try:
                        if elem.is_displayed():
                            return elem
                    except StaleElementReferenceException:
                        continue
            except ValueError:
                raise
            except Exception:
                pass
        time.sleep(0.25)
    return None


def click_first(
    driver: webdriver.Chrome,
    selectors: Sequence[Tuple[str, str]],
    timeout: float = ACTION_TIMEOUT_SEC,
) -> bool:
    elem = find_first(driver, selectors, timeout=timeout)
    if not elem:
        return False
    try:
        elem.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            js_click(driver, elem)
            return True
        except (JavascriptException, WebDriverException):
            return False


def get_texts(driver: webdriver.Chrome, selectors: Sequence[Tuple[str, str]]) -> List[str]:
    texts: List[str] = []
    for mode, selector in selectors:
        try:
            elements = (
                driver.find_elements(By.CSS_SELECTOR, selector)
                if mode == "css"
                else driver.find_elements(By.XPATH, selector)
            )
            for elem in elements:
                try:
                    text = elem.text.strip()
                except StaleElementReferenceException:
                    continue
                if text:
                    texts.append(text)
        except Exception:
            continue
    return texts


def attr_contains_true(element: WebElement, names: Sequence[str], needles: Sequence[str]) -> bool:
    for name in names:
        try:
            value = (element.get_attribute(name) or "").strip().lower()
        except StaleElementReferenceException:
            continue
        if not value:
            continue
        if value in {"true", "1", "yes"}:
            return True
        if any(needle in value for needle in needles):
            return True
    return False


def element_looks_active(element: WebElement) -> bool:
    for attr in ("aria-pressed", "aria-checked"):
        try:
            val = (element.get_attribute(attr) or "").strip().lower()
            if val in {"true", "1", "yes"}:
                return True
        except StaleElementReferenceException:
            pass
    return attr_contains_true(
        element,
        names=["class", "data-state", "data-active"],
        needles=["active", "selected", "checked", "added", "remove", "in_list", "done"],
    )


def current_rating(driver: webdriver.Chrome) -> Optional[str]:
    texts = get_texts(
        driver,
        [
            ("xpath", "//button[.//span[text()='Изменить оценку']]//span[contains(@class, 'styles_value')]"),
            ("css", ".myVote"),
            ("xpath", "//*[contains(@class, 'myVote') or contains(@data-testid, 'user-rating')]"),
        ],
    )
    for text in texts:
        stripped = text.strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 10:
            return stripped
    return None


def set_rating(driver: webdriver.Chrome, rating: str) -> Tuple[bool, str]:
    existing = current_rating(driver)
    if existing == rating:
        return True, "already_set"

    selectors = [
        ("css", f".s{rating}"),
        ("xpath", f"//*[contains(concat(' ', normalize-space(@class), ' '), ' s{rating} ')]"),
        ("xpath", f"//*[@data-value='{rating}']"),
        ("xpath", f"//button[@aria-label='{rating}' or @title='{rating}']"),
        ("xpath", f"//button[normalize-space(text())='{rating}']"),
        ("xpath", f"//span[normalize-space(text())='{rating}']/ancestor::*[self::button or self::a or self::div][1]"),
    ]

    if not click_first(driver, selectors):
        return False, "rating_control_not_found"

    time.sleep(POST_CLICK_DELAY_SEC)
    if current_rating(driver) == rating:
        return True, "set"
    return True, "clicked_unverified"


WATCHLIST_SELECTORS = [
    ("xpath", "//button[@title='Буду смотреть']"),
    ("xpath", "//button[@aria-label='Буду смотреть']"),
    ("xpath", "//button[contains(., 'Буду смотреть') or contains(., 'Хочу посмотреть')]"),
    ("xpath", "//a[contains(., 'Буду смотреть') or contains(., 'Хочу посмотреть')]"),
    ("xpath", "//*[contains(@data-testid, 'watchlist') or contains(@data-testid, 'bookmark')]"),
    ("css", ".addFolder"),
    ("css", "[class*='addFolder']"),
]

WATCHED_DROPDOWN_TRIGGER_SELECTORS = [
    ("xpath", "//button[.//span[@aria-label='Меню действий']]"),
    ("xpath", "//button[@aria-label='Меню действий']"),
]

WATCHED_MENU_ITEM_SELECTORS = [
    ("xpath", "//button[@data-testid='kp-ui-kit.MenuItem.button'][.//span[text()='Просмотрен']]"),
    ("xpath", "//button[.//span[@data-testid='kp-ui-kit.MenuItem.text' and text()='Просмотрен']]"),
]


def mark_watched(driver: webdriver.Chrome) -> Tuple[bool, str]:
    trigger = find_first(driver, WATCHED_DROPDOWN_TRIGGER_SELECTORS)
    if not trigger:
        return False, "watched_dropdown_not_found"

    try:
        trigger.click()
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            js_click(driver, trigger)
        except (JavascriptException, WebDriverException):
            return False, "watched_dropdown_click_failed"

    time.sleep(POST_CLICK_DELAY_SEC)

    menu_item = find_first(driver, WATCHED_MENU_ITEM_SELECTORS, timeout=ACTION_TIMEOUT_SEC)
    if not menu_item:
        return False, "watched_menu_item_not_found"

    if element_looks_active(menu_item):
        return True, "already_set"

    try:
        menu_item.click()
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            js_click(driver, menu_item)
        except (JavascriptException, WebDriverException):
            return False, "watched_click_failed"

    time.sleep(POST_CLICK_DELAY_SEC)

    item2 = find_first(driver, WATCHED_MENU_ITEM_SELECTORS, timeout=2.0)
    if item2 and element_looks_active(item2):
        return True, "set"

    return True, "clicked_unverified"


def toggle_mark(driver: webdriver.Chrome, selectors: Sequence[Tuple[str, str]], label: str) -> Tuple[bool, str]:
    elem = find_first(driver, selectors)
    if not elem:
        return False, f"{label}_control_not_found"

    if element_looks_active(elem):
        return True, "already_set"

    try:
        elem.click()
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            js_click(driver, elem)
        except (JavascriptException, WebDriverException):
            return False, f"{label}_click_failed"

    time.sleep(POST_CLICK_DELAY_SEC)
    refreshed = find_first(driver, selectors, timeout=2.0)
    if refreshed and element_looks_active(refreshed):
        return True, "set"
    return True, "clicked_unverified"


def _run_phase(
    driver: webdriver.Chrome,
    items: List[Union[RatedMovie, SimpleMovie]],
    action_fn: Callable[[Union[RatedMovie, SimpleMovie]], Tuple[bool, str]],
    done_set: Set[str],
    label: str,
    report_list: List[dict],
    state_path: Path,
    state: ImportState,
    delay: float,
) -> None:
    total = len(items)
    counts = {"ok": 0, "unverified": 0, "failed": 0, "skipped": 0}

    for idx, item in enumerate(items, start=1):
        url = item.url
        pct = idx * 100 // total
        prefix = f"[{label} {idx}/{total} {pct:3d}%]"

        if url in done_set:
            print(f"{prefix} SKIP {url} (уже импортировано)")
            counts["skipped"] += 1
            continue

        status = "failed"
        detail = "unknown"
        try:
            open_movie(driver, url)
            maybe_close_popups(driver)
            ok, detail = action_fn(item)
            if ok and detail in ("set", "already_set"):
                status = "ok"
                done_set.add(url)
                save_state(state_path, state)
            elif ok:
                status = "unverified"
        except Exception as exc:
            detail = f"exception: {exc}"

        counts[status] += 1

        entry: dict = {"url": url, "status": status, "detail": detail}
        if hasattr(item, "rating"):
            entry["rating"] = item.rating

        report_list.append(entry)

        rating_suffix = f" -> {item.rating}" if hasattr(item, "rating") else ""
        print(f"{prefix} {status.upper()} {url}{rating_suffix} ({detail})")

        if idx < total:
            time.sleep(delay)

    print(
        f"\n[{label}] Итог: "
        f"ok={counts['ok']}  unverified={counts['unverified']}  "
        f"failed={counts['failed']}  skipped={counts['skipped']}"
    )


def find_latest_import_dir() -> Optional[Path]:
    candidates = sorted(
        [
            d for d in Path.cwd().iterdir()
            if d.is_dir() and d.name.startswith("kinopoisk_import_") and (d / STATE_FILENAME).exists()
        ],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def prompt_manual_login(driver: webdriver.Chrome) -> None:
    print("\n[1/5] Открываю Кинопоиск для ручного входа в ЦЕЛЕВОЙ аккаунт...")
    driver.get(LOGIN_URL)
    print(
        "Войдите в аккаунт-получатель в открывшемся окне браузера.\n"
        "После входа можно просто остаться на любой странице Кинопоиска и вернуться в терминал."
    )
    input("Нажмите Enter после входа... ")


def run_import(
    input_path: str,
    chromedriver: Optional[str] = None,
    chrome_binary: Optional[str] = None,
    profile_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    delay: float = DEFAULT_ITEM_DELAY_SEC,
    skip_ratings: bool = False,
    skip_watched: bool = False,
    skip_watchlist: bool = False,
    dry_run: bool = False,
    no_resume: bool = False,
) -> int:
    ratings, watched, watchlist = load_input(input_path)

    rated_urls = {item.url for item in ratings}
    watched = [item for item in watched if item.url not in rated_urls]

    print("\nВходные данные прочитаны:")
    print(json.dumps({
        "ratings": len(ratings),
        "watched_unrated": len(watched),
        "watchlist": len(watchlist),
    }, ensure_ascii=False, indent=2))

    if dry_run:
        print("\nDry run: импорт не выполнялся.")
        return 0

    if not no_resume and not output_dir:
        latest = find_latest_import_dir()
        if latest:
            out_dir = latest
            print(f"Найдено предыдущее состояние: {out_dir}")
        else:
            out_dir = build_output_dir(output_dir, "kinopoisk_import")
    else:
        out_dir = build_output_dir(output_dir, "kinopoisk_import")

    state_path = out_dir / STATE_FILENAME
    report_path = out_dir / REPORT_FILENAME

    state = ImportState() if no_resume else load_state(state_path)

    try:
        driver = create_driver(chromedriver, chrome_binary, profile_dir)
    except WebDriverException as e:
        print("Не удалось запустить Chrome через Selenium.")
        print(f"Причина: {e}")
        return 2

    report: dict = {
        "started_at": datetime.now().isoformat(),
        "input": str(Path(input_path).resolve()),
        "counts": {
            "ratings": len(ratings),
            "watched_unrated": len(watched),
            "watchlist": len(watchlist),
        },
        "results": {"ratings": [], "watched": [], "watchlist": []},
        "notes": [
            "Manual login is required.",
            "Kinopoisk layout changes may require selector updates.",
            "watched import is best-effort and more fragile than ratings/watchlist.",
            "unverified — action was clicked but outcome could not be confirmed; will retry on resume.",
        ],
    }

    active_phases = (
        (not skip_ratings,   "Импорт оценок",                   ratings,   state.ratings_done,   "ratings",   lambda item: set_rating(driver, item.rating)),
        (not skip_watched,   "Импорт просмотренного (best-effort)", watched, state.watched_done,  "watched",   lambda item: mark_watched(driver)),
        (not skip_watchlist, "Импорт 'буду смотреть'",           watchlist, state.watchlist_done, "watchlist", lambda item: toggle_mark(driver, WATCHLIST_SELECTORS, "watchlist")),
    )
    total_steps = 2 + sum(1 for enabled, *_ in active_phases if enabled)

    try:
        prompt_manual_login(driver)
        print(f"\n[2/{total_steps}] Начинаю импорт...")

        step = 3
        for enabled, phase_label, items_list, done_set, result_key, action_fn in active_phases:
            if not enabled:
                continue
            print(f"\n[{step}/{total_steps}] {phase_label}...")
            _run_phase(
                driver=driver,
                items=items_list,
                action_fn=action_fn,
                done_set=done_set,
                label=result_key,
                report_list=report["results"][result_key],
                state_path=state_path,
                state=state,
                delay=delay,
            )
            step += 1

        report["finished_at"] = datetime.now().isoformat()
        report["state_file"] = str(state_path.resolve())
        report["report_file"] = str(report_path.resolve())
        save_state(state_path, state)
        save_report(report_path, report)

        summary = {
            "ratings_ok": len(state.ratings_done),
            "watched_ok": len(state.watched_done),
            "watchlist_ok": len(state.watchlist_done),
        }
        print("\nИмпорт завершён.")
        print(f"Отчёт: {report_path}")
        print(f"Состояние: {state_path}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    except KeyboardInterrupt:
        report["finished_at"] = datetime.now().isoformat()
        report["interrupted"] = True
        save_state(state_path, state)
        save_report(report_path, report)
        print("\nОстановлено пользователем.")
        print(f"Частичный отчёт: {report_path}")
        return 130
    except Exception as exc:
        report["finished_at"] = datetime.now().isoformat()
        report["fatal_error"] = str(exc)
        save_state(state_path, state)
        save_report(report_path, report)
        print(f"\nОшибка: {exc}")
        print(f"Частичный отчёт: {report_path}")
        return 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import ratings / watched / watchlist into a Kinopoisk account.")
    parser.add_argument("--input", required=True, help="Path to kinopoisk_export.json or to a directory with ratings.json/watched.json/watchlist.json.")
    parser.add_argument("--chromedriver", help="Path to chromedriver binary. Optional on modern Selenium.")
    parser.add_argument("--chrome-binary", help="Path to Chrome/Chromium binary if autodetection fails.")
    parser.add_argument("--profile-dir", help="Chrome user-data dir to reuse an existing browser profile.")
    parser.add_argument("--output-dir", help="Where to store the import report/state.")
    parser.add_argument("--delay", type=float, default=DEFAULT_ITEM_DELAY_SEC, help=f"Delay between items in seconds (default: {DEFAULT_ITEM_DELAY_SEC}).")
    parser.add_argument("--skip-ratings", action="store_true", help="Do not import ratings.")
    parser.add_argument("--skip-watched", action="store_true", help="Do not import watched marks.")
    parser.add_argument("--skip-watchlist", action="store_true", help="Do not import watchlist.")
    parser.add_argument("--dry-run", action="store_true", help="Only read input and print counts without clicking anything.")
    parser.add_argument("--no-resume", action="store_true", help="Ignore saved import_state.json and start from scratch.")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay не может быть отрицательным")
    return args


def main() -> int:
    args = parse_args()
    return run_import(
        input_path=args.input,
        chromedriver=args.chromedriver,
        chrome_binary=args.chrome_binary,
        profile_dir=args.profile_dir,
        output_dir=args.output_dir,
        delay=args.delay,
        skip_ratings=args.skip_ratings,
        skip_watched=args.skip_watched,
        skip_watchlist=args.skip_watchlist,
        dry_run=args.dry_run,
        no_resume=args.no_resume,
    )


if __name__ == "__main__":
    sys.exit(main())
