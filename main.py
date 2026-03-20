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
from typing import List, Optional, Set

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver, WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


LOG = logging.getLogger("selenium_html_capture")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def xpath_literal(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


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
    driver.implicitly_wait(0)

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


def wait_dom_ready(driver: WebDriver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )


def wait_body_present(driver: WebDriver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )


def wait_post_click_stabilization(driver: WebDriver, old_url: str, timeout: int = 20) -> None:
    end = time.time() + timeout
    last_html_len = -1
    stable_rounds = 0

    while time.time() < end:
        try:
            current_url = driver.current_url
            state = driver.execute_script("return document.readyState")
            html_len = len(driver.page_source or "")

            if current_url != old_url and state in ("interactive", "complete"):
                time.sleep(1.0)
                return

            if state in ("interactive", "complete"):
                if html_len == last_html_len:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                last_html_len = html_len

                if stable_rounds >= 2:
                    time.sleep(0.8)
                    return
        except Exception:
            pass

        time.sleep(1.0)


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

    deadline = time.time() + timeout

    while time.time() < deadline:
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
                        LOG.info("Bandeau cookies détecté, fermeture.")
                        robust_click(driver, elem)
                        time.sleep(1.0)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return False


def build_target_xpaths(target_text: str) -> List[str]:
    x = xpath_literal(target_text)
    return [
        f"//a[contains(normalize-space(.), {x})]",
        f"//button[contains(normalize-space(.), {x})]",
        f"//*[@role='button' and contains(normalize-space(.), {x})]",
        f"//*[@aria-label and contains(@aria-label, {x})]",
        f"//*[@title and contains(@title, {x})]",
        f"//*[contains(normalize-space(.), {x}) and (self::a or self::button or @role='button')]",
    ]


def find_clickable_target(driver: WebDriver, target_text: str, timeout: int = 20) -> WebElement:
    xpaths = build_target_xpaths(target_text)

    for xp in xpaths:
        try:
            LOG.info("Recherche XPath : %s", xp)
            elem = WebDriverWait(driver, max(2, timeout // 2)).until(
                EC.presence_of_element_located((By.XPATH, xp))
            )
            if elem:
                return elem
        except TimeoutException:
            continue

    LOG.info("Fallback recherche globale.")
    candidates = driver.find_elements(
        By.XPATH,
        "//a | //button | //*[@role='button'] | //span | //div | //li",
    )

    target_norm = normalize_text(target_text)
    best_elem = None
    best_score = -1

    for elem in candidates:
        try:
            raw = " ".join(
                filter(
                    None,
                    [
                        elem.text,
                        elem.get_attribute("aria-label"),
                        elem.get_attribute("title"),
                    ],
                )
            )
            norm = normalize_text(raw)
            if not norm:
                continue

            score = 0
            if norm == target_norm:
                score += 100
            if target_norm in norm:
                score += 50
            if elem.tag_name in ("a", "button"):
                score += 20
            if elem.is_displayed():
                score += 20
            if elem.is_enabled():
                score += 10

            if score > best_score:
                best_score = score
                best_elem = elem

        except StaleElementReferenceException:
            continue
        except Exception:
            continue

    if best_elem is None:
        raise NoSuchElementException(f"Aucun élément contenant '{target_text}' n'a été trouvé.")

    return best_elem


def robust_click(driver: WebDriver, element: WebElement, timeout: int = 10) -> None:
    last_error = None

    for method in ("selenium_click", "actionchains_click", "js_click"):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                element,
            )
            time.sleep(0.5)

            if method == "selenium_click":
                WebDriverWait(driver, timeout).until(lambda d: element.is_displayed())
                element.click()
                return

            if method == "actionchains_click":
                ActionChains(driver).move_to_element(element).pause(0.2).click().perform()
                return

            if method == "js_click":
                driver.execute_script("arguments[0].click();", element)
                return

        except (
            ElementClickInterceptedException,
            StaleElementReferenceException,
            WebDriverException,
        ) as e:
            last_error = e
            time.sleep(0.8)

    raise RuntimeError(f"Impossible de cliquer sur l'élément. Dernière erreur : {last_error}")


def switch_to_new_tab_if_any(driver: WebDriver, before_handles: List[str], timeout: int = 5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        handles = driver.window_handles
        if len(handles) > len(before_handles):
            new_handle = [h for h in handles if h not in before_handles][0]
            driver.switch_to.window(new_handle)
            LOG.info("Nouvel onglet détecté.")
            return True
        time.sleep(0.5)
    return False


def save_html(driver: WebDriver, output_html: Path) -> None:
    output_html.write_text(driver.page_source, encoding="utf-8")
    LOG.info("HTML sauvegardé : %s", output_html.resolve())


def save_screenshot(driver: WebDriver, output_png: Path) -> None:
    driver.save_screenshot(str(output_png))
    LOG.info("Capture sauvegardée : %s", output_png.resolve())


def extract_urls(driver: WebDriver) -> List[str]:
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


def save_urls(urls: List[str], output_txt: Path) -> None:
    output_txt.write_text("\n".join(urls), encoding="utf-8")


def ask_input_if_missing(value: Optional[str], prompt_text: str, default: Optional[str] = None) -> str:
    if value:
        return value.strip()

    if default:
        entered = input(f"{prompt_text} [{default}] : ").strip()
        return entered if entered else default

    while True:
        entered = input(f"{prompt_text} : ").strip()
        if entered:
            return entered
        print("Valeur obligatoire.")


def run(
    url: str,
    target_text: str,
    output_html: Path,
    output_png: Optional[Path],
    output_urls: Path,
    headless: bool,
    timeout: int,
    user_data_dir: Optional[str],
) -> None:
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)

    try:
        LOG.info("Ouverture URL : %s", url)
        driver.get(url)
        wait_dom_ready(driver, timeout)
        wait_body_present(driver, timeout)

        dismiss_cookie_banner(driver)

        before_handles = driver.window_handles[:]
        old_url = driver.current_url

        LOG.info("Recherche de l'élément cible : %s", target_text)
        target = find_clickable_target(driver, target_text=target_text, timeout=timeout)

        robust_click(driver, target)
        switch_to_new_tab_if_any(driver, before_handles, timeout=5)

        wait_post_click_stabilization(driver, old_url=old_url, timeout=timeout)
        wait_dom_ready(driver, timeout=timeout)

        save_html(driver, output_html)

        if output_png:
            save_screenshot(driver, output_png)

        urls = extract_urls(driver)
        save_urls(urls, output_urls)

        print("\n" + "=" * 80)
        print(f"URL finale : {driver.current_url}")
        print(f"Nombre d'URLs trouvées : {len(urls)}")
        print("=" * 80)

        for i, found_url in enumerate(urls, start=1):
            print(f"{i:04d} | {found_url}")

        print("=" * 80)
        print(f"HTML sauvegardé     : {output_html.resolve()}")
        print(f"Capture sauvegardée : {output_png.resolve() if output_png else 'Non demandée'}")
        print(f"URLs sauvegardées   : {output_urls.resolve()}")
        print("=" * 80)

    finally:
        driver.quit()


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ouvre une URL, clique sur un texte donné, puis récupère le HTML et les URLs trouvées."
    )
    parser.add_argument("--url", required=False, help="URL de départ")
    parser.add_argument("--target", default=None, help="Texte à chercher pour cliquer")
    parser.add_argument("--html", default="page_apres_click.html", help="Fichier HTML de sortie")
    parser.add_argument("--png", default="page_apres_click.png", help="Capture écran de sortie")
    parser.add_argument("--urls", default="urls_trouvees.txt", help="Fichier TXT des URLs trouvées")
    parser.add_argument("--headless", action="store_true", help="Lancer Chrome sans interface graphique")
    parser.add_argument("--timeout", type=int, default=25, help="Timeout général en secondes")
    parser.add_argument("--user-data-dir", default=None, help="Profil Chrome existant si besoin")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    setup_logging()
    args = parse_args(argv)

    try:
        url = ask_input_if_missing(args.url, "Entre l'URL")
        target = ask_input_if_missing(args.target, "Texte du bouton/lien à cliquer", "Ventes flash")

        run(
            url=url,
            target_text=target,
            output_html=Path(args.html),
            output_png=Path(args.png) if args.png else None,
            output_urls=Path(args.urls),
            headless=args.headless,
            timeout=args.timeout,
            user_data_dir=args.user_data_dir,
        )
        return 0

    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 1
    except Exception as e:
        LOG.exception("Échec : %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))