#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


LOG = logging.getLogger("amazon_deals_capture")


# ============================================================
# LOGGING
# ============================================================

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ============================================================
# TEXTE
# ============================================================

def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


# ============================================================
# DRIVER
# ============================================================

def build_driver(headless: bool = False, user_data_dir: Optional[str] = None) -> WebDriver:
    chrome_options = Options()

    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--lang=fr-FR")
    chrome_options.add_argument("--window-size=1600,1200")

    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.set_page_load_timeout(60)

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    Object.defineProperty(navigator, 'language', {
                        get: () => 'fr-FR'
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['fr-FR', 'fr', 'en-US', 'en']
                    });
                """
            },
        )
    except Exception:
        pass

    return driver


# ============================================================
# ATTENTES
# ============================================================

def wait_dom_ready(driver: WebDriver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )


def wait_body_present(driver: WebDriver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_element(By.TAG_NAME, "body")
    )


def stabilize_page(driver: WebDriver, timeout: int = 15) -> None:
    end = time.time() + timeout
    last_len = -1
    stable_rounds = 0

    while time.time() < end:
        try:
            html = driver.page_source or ""
            current_len = len(html)
            state = driver.execute_script("return document.readyState")

            if state in ("interactive", "complete"):
                if current_len == last_len:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                last_len = current_len

                if stable_rounds >= 2:
                    time.sleep(0.8)
                    return
        except Exception:
            pass

        time.sleep(1.0)


# ============================================================
# COOKIES
# ============================================================

def dismiss_cookie_banner(driver: WebDriver, timeout: int = 5) -> bool:
    labels = [
        "accepter",
        "tout accepter",
        "accepter tout",
        "j'accepte",
        "ok",
        "continuer",
        "allow all",
        "accept all",
        "accept",
        "agree",
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
                    text = normalize_text(
                        " ".join(
                            filter(
                                None,
                                [
                                    elem.text,
                                    elem.get_attribute("value"),
                                    elem.get_attribute("aria-label"),
                                    elem.get_attribute("title"),
                                ],
                            )
                        )
                    )
                    if text and any(label in text for label in labels):
                        elem.click()
                        time.sleep(1.0)
                        LOG.info("Bandeau cookies fermé.")
                        return True
                except Exception:
                    continue
        except Exception:
            pass

        time.sleep(0.5)

    return False


# ============================================================
# CIBLE AMAZON
# ============================================================

def is_amazon_fr(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return "amazon.fr" in host


def compute_navigation_url(start_url: str, target_text: str) -> Tuple[str, bool]:
    """
    Si l'URL est Amazon FR et que la cible ressemble à "Ventes flash",
    on évite le clic et on va directement sur la page deals.
    """
    target = normalize_text(target_text)

    direct_tokens = [
        "ventes flash",
        "vente flash",
        "deals",
        "bons plans",
        "bon plan",
        "promotions",
        "promo",
    ]

    if is_amazon_fr(start_url) and any(tok in target for tok in direct_tokens):
        return "https://www.amazon.fr/deals?ref_=nav_cs_gb", True

    return start_url, False


# ============================================================
# EXTRACTION DES URLS
# ============================================================

def extract_all_urls(driver: WebDriver) -> List[str]:
    urls: Set[str] = set()

    try:
        elements = driver.find_elements(By.XPATH, "//*[@href]")
        for elem in elements:
            try:
                href = elem.get_attribute("href")
                if href and href.startswith(("http://", "https://")):
                    urls.add(href.strip())
            except Exception:
                continue
    except Exception:
        pass

    return sorted(urls)


def canonicalize_amazon_product_url(url: str) -> Optional[str]:
    m = re.search(r"/dp/([A-Z0-9]{10})", url, re.IGNORECASE)
    if m:
        asin = m.group(1).upper()
        return f"https://www.amazon.fr/dp/{asin}"

    m = re.search(r"/gp/product/([A-Z0-9]{10})", url, re.IGNORECASE)
    if m:
        asin = m.group(1).upper()
        return f"https://www.amazon.fr/dp/{asin}"

    return None


def filter_amazon_urls(urls: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """
    Retourne :
    - pages promos utiles
    - produits dédupliqués
    - ensemble filtré total
    """
    promo_pages: Set[str] = set()
    product_pages: Set[str] = set()

    for url in urls:
        u = url.lower()

        if "amazon.fr" not in u:
            continue

        if any(x in u for x in [
            "/deals",
            "/events/deals",
            "/coupons",
            "/promotions-bons-plans",
        ]):
            promo_pages.add(url)

        product_url = canonicalize_amazon_product_url(url)
        if product_url:
            product_pages.add(product_url)

    all_filtered = sorted(promo_pages | product_pages)
    return sorted(promo_pages), sorted(product_pages), all_filtered


# ============================================================
# SAUVEGARDE
# ============================================================

def save_text_file(path: Path, lines: List[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def save_html(driver: WebDriver, path: Path) -> None:
    path.write_text(driver.page_source, encoding="utf-8")


def save_screenshot(driver: WebDriver, path: Path) -> None:
    driver.save_screenshot(str(path))


# ============================================================
# INPUT
# ============================================================

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


# ============================================================
# RUN
# ============================================================

def run(
    start_url: str,
    target_text: str,
    output_html: Path,
    output_png: Path,
    output_all_urls: Path,
    output_filtered_urls: Path,
    headless: bool,
    timeout: int,
    user_data_dir: Optional[str],
) -> None:
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)

    try:
        target_url, direct_mode = compute_navigation_url(start_url, target_text)

        LOG.info("URL de départ demandée : %s", start_url)
        if direct_mode:
            LOG.info("Mode direct activé. Navigation vers : %s", target_url)
        else:
            LOG.info("Mode normal. Navigation vers : %s", target_url)

        driver.get(target_url)
        wait_dom_ready(driver, timeout)
        wait_body_present(driver, timeout)

        dismiss_cookie_banner(driver)
        stabilize_page(driver, timeout=15)

        final_url = driver.current_url
        all_urls = extract_all_urls(driver)
        promo_urls, product_urls, filtered_urls = filter_amazon_urls(all_urls)

        save_html(driver, output_html)
        save_screenshot(driver, output_png)
        save_text_file(output_all_urls, all_urls)
        save_text_file(output_filtered_urls, filtered_urls)

        print("\n" + "=" * 90)
        print(f"URL finale : {final_url}")
        print(f"Nombre total d'URLs trouvées : {len(all_urls)}")
        print(f"Nombre d'URLs filtrées utiles : {len(filtered_urls)}")
        print("=" * 90)

        print("\nPAGES PROMOS TROUVÉES")
        print("-" * 90)
        for i, u in enumerate(promo_urls, start=1):
            print(f"{i:03d} | {u}")

        print("\nPRODUITS TROUVÉS")
        print("-" * 90)
        for i, u in enumerate(product_urls, start=1):
            print(f"{i:03d} | {u}")

        print("\nFICHIERS GÉNÉRÉS")
        print("-" * 90)
        print(f"HTML                : {output_html.resolve()}")
        print(f"Capture             : {output_png.resolve()}")
        print(f"Toutes les URLs     : {output_all_urls.resolve()}")
        print(f"URLs filtrées utiles: {output_filtered_urls.resolve()}")
        print("=" * 90)

    finally:
        driver.quit()


# ============================================================
# CLI
# ============================================================

def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Va sur une URL, ou directement sur Amazon Deals si la cible est 'Ventes flash', puis extrait les URLs."
    )
    parser.add_argument("--url", required=False, help="URL de départ")
    parser.add_argument("--target", required=False, help="Texte cible, ex: Ventes flash")
    parser.add_argument("--html", default="page_apres_navigation.html", help="Fichier HTML de sortie")
    parser.add_argument("--png", default="page_apres_navigation.png", help="Capture de sortie")
    parser.add_argument("--all-urls", default="toutes_les_urls.txt", help="Fichier de toutes les URLs")
    parser.add_argument("--filtered-urls", default="urls_filtrees_utiles.txt", help="Fichier des URLs filtrées utiles")
    parser.add_argument("--headless", action="store_true", help="Lancer Chrome sans interface graphique")
    parser.add_argument("--timeout", type=int, default=25, help="Timeout général")
    parser.add_argument("--user-data-dir", default=None, help="Profil Chrome si besoin")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    setup_logging()
    args = parse_args(argv)

    try:
        url = ask_input_if_missing(args.url, "Entre l'URL", "https://www.amazon.fr/")
        target = ask_input_if_missing(args.target, "Texte cible", "Ventes flash")

        run(
            start_url=url,
            target_text=target,
            output_html=Path(args.html),
            output_png=Path(args.png),
            output_all_urls=Path(args.all_urls),
            output_filtered_urls=Path(args.filtered_urls),
            headless=args.headless,
            timeout=args.timeout,
            user_data_dir=args.user_data_dir,
        )
        return 0

    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 1
    except TimeoutException as e:
        LOG.exception("Timeout : %s", e)
        return 1
    except Exception as e:
        LOG.exception("Échec : %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))