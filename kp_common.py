#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


PAGE_LOAD_TIMEOUT_SEC = 20
KINOPOISK_BASE = "https://www.kinopoisk.ru"
LOGIN_URL = "https://www.kinopoisk.ru/#login"


@dataclass
class RatedMovie:
    url: str
    rating: str


@dataclass
class SimpleMovie:
    url: str


def normalize_movie_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("/"):
        url = KINOPOISK_BASE + url
    parts = urlsplit(url)
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    if "kinopoisk.ru" not in parts.netloc:
        return ""
    return normalized


def build_output_dir(base_dir: Optional[str], prefix: str) -> Path:
    if base_dir:
        out_dir = Path(base_dir)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path.cwd() / f"{prefix}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def create_driver(
    chromedriver_path: Optional[str],
    chrome_binary: Optional[str],
    profile_dir: Optional[str],
    page_load_timeout: int = PAGE_LOAD_TIMEOUT_SEC,
) -> webdriver.Chrome:
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    if chrome_binary:
        options.binary_location = chrome_binary
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")

    if chromedriver_path:
        service = Service(chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(page_load_timeout)
    return driver
