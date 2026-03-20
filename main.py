#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Set, List
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


LOG = logging.getLogger("page_watcher")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_driver(headless: bool = False, user_data_dir: Optional[str] = None) -> WebDriver:
    chrome_options = Options()

    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1600,1200")
    chrome_options.add_argument("--lang=fr-FR")

    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(60)
    return driver


def wait_ready(driver: WebDriver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_element(By.TAG_NAME, "body")
    )


def dismiss_cookie_banner(driver: WebDriver, timeout: int = 5) -> bool:
    labels = [
        "accepter", "tout accepter", "accepter tout", "j'accepte", "ok",
        "continuer", "accept", "accept all", "allow all", "agree"
    ]
    end = time.time() + timeout

    while time.time() < end:
        try:
            elems = driver.find_elements(
                By.XPATH,
                "//button | //a | //input[@type='button'] | //input[@type='submit'] | //*[@role='button']"
            )
            for elem in elems:
                try:
                    txt = " ".join(
                        filter(
                            None,
                            [
                                elem.text,
                                elem.get_attribute("value"),
                                elem.get_attribute("aria-label"),
                                elem.get_attribute("title"),
                            ],
                        )
                    ).strip().lower()
                    if txt and any(label in txt for label in labels):
                        elem.click()
                        time.sleep(1)
                        LOG.info("Bandeau cookies fermé.")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)

    return False


def auto_scroll(driver: WebDriver, pause: float = 1.0, max_rounds_without_growth: int = 3) -> None:
    """
    Scrolle progressivement jusqu'à stabilisation de la hauteur.
    """
    last_height = driver.execute_script("return document.body.scrollHeight")
    stagnant_rounds = 0

    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)

        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        last_height = new_height

        if stagnant_rounds >= max_rounds_without_growth:
            break

    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)


def extract_urls(driver: WebDriver, same_domain_only: bool = False) -> List[str]:
    urls: Set[str] = set()
    base_host = urlparse(driver.current_url).netloc.lower()

    elems = driver.find_elements(By.XPATH, "//*[@href]")
    for elem in elems:
        try:
            href = elem.get_attribute("href")
            if not href:
                continue
            if not href.startswith(("http://", "https://")):
                continue

            if same_domain_only:
                host = urlparse(href).netloc.lower()
                if host != base_host:
                    continue

            urls.add(href.strip())
        except Exception:
            continue

    return sorted(urls)


def save_snapshot(
    out_dir: Path,
    html: str,
    urls: List[str],
    cycle_index: int,
    keep_history: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if keep_history:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = out_dir / f"snapshot_{cycle_index:04d}_{stamp}.html"
        urls_path = out_dir / f"urls_{cycle_index:04d}_{stamp}.txt"
    else:
        html_path = out_dir / "latest.html"
        urls_path = out_dir / "latest_urls.txt"

    html_path.write_text(html, encoding="utf-8")
    urls_path.write_text("\n".join(urls), encoding="utf-8")


def append_new_urls_log(out_dir: Path, new_urls: List[str], cycle_index: int) -> None:
    if not new_urls:
        return

    log_path = out_dir / "new_urls.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n===== cycle {cycle_index} | {datetime.now().isoformat()} =====\n")
        for url in new_urls:
            f.write(url + "\n")


def monitor_page(
    url: str,
    refresh_seconds: int,
    headless: bool,
    user_data_dir: Optional[str],
    same_domain_only: bool,
    out_dir: Path,
    keep_history: bool,
) -> None:
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)
    seen_urls: Set[str] = set()
    cycle_index = 0

    try:
        LOG.info("Ouverture : %s", url)
        driver.get(url)
        wait_ready(driver)
        dismiss_cookie_banner(driver)

        while True:
            cycle_index += 1
            LOG.info("Cycle %d", cycle_index)

            wait_ready(driver)
            auto_scroll(driver, pause=1.0, max_rounds_without_growth=3)

            current_html = driver.page_source
            current_urls = extract_urls(driver, same_domain_only=same_domain_only)

            current_set = set(current_urls)
            new_urls = sorted(current_set - seen_urls)

            LOG.info("URLs trouvées : %d", len(current_urls))
            LOG.info("Nouvelles URLs : %d", len(new_urls))

            if new_urls:
                print("\n" + "=" * 80)
                print(f"NOUVELLES URLS - cycle {cycle_index}")
                print("=" * 80)
                for i, item in enumerate(new_urls, start=1):
                    print(f"{i:04d} | {item}")
                print("=" * 80)

            save_snapshot(
                out_dir=out_dir,
                html=current_html,
                urls=current_urls,
                cycle_index=cycle_index,
                keep_history=keep_history,
            )
            append_new_urls_log(out_dir, new_urls, cycle_index)

            seen_urls = current_set

            LOG.info("Rafraîchissement dans %d secondes", refresh_seconds)
            time.sleep(refresh_seconds)
            driver.refresh()
            wait_ready(driver)

    finally:
        driver.quit()


def ask_input_if_missing(value: Optional[str], prompt_text: str, default: Optional[str] = None) -> str:
    if value:
        return value.strip()

    if default is not None:
        typed = input(f"{prompt_text} [{default}] : ").strip()
        return typed if typed else default

    while True:
        typed = input(f"{prompt_text} : ").strip()
        if typed:
            return typed
        print("Valeur obligatoire.")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Surveille une page autorisée : scroll auto, extraction d'URLs, rafraîchissement périodique."
    )
    parser.add_argument("--url", required=False, help="URL à surveiller")
    parser.add_argument("--refresh", type=int, default=60, help="Secondes entre deux rafraîchissements")
    parser.add_argument("--headless", action="store_true", help="Mode sans interface")
    parser.add_argument("--same-domain-only", action="store_true", help="Ne garder que les URLs du même domaine")
    parser.add_argument("--out-dir", default="watch_output", help="Dossier de sortie")
    parser.add_argument("--no-history", action="store_true", help="Écrase toujours latest.html/latest_urls.txt")
    parser.add_argument("--user-data-dir", default=None, help="Profil Chrome existant")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    setup_logging()
    args = parse_args(argv)

    try:
        url = ask_input_if_missing(args.url, "Entre l'URL à surveiller")
        monitor_page(
            url=url,
            refresh_seconds=args.refresh,
            headless=args.headless,
            user_data_dir=args.user_data_dir,
            same_domain_only=args.same_domain_only,
            out_dir=Path(args.out_dir),
            keep_history=not args.no_history,
        )
        return 0
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
        return 0
    except TimeoutException as e:
        LOG.exception("Timeout : %s", e)
        return 1
    except WebDriverException as e:
        LOG.exception("Erreur WebDriver : %s", e)
        return 1
    except Exception as e:
        LOG.exception("Erreur : %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))