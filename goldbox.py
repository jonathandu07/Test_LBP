#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

TRACKING_QUERY_PREFIXES = (
    "utm_", "ref", "fbclid", "gclid", "msclkid",
    "aff", "affiliate", "affid", "tag", "source",
    "campaign", "cmp", "clkid",
)

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".css", ".js", ".json", ".xml", ".pdf", ".zip",
    ".rar", ".7z", ".mp4", ".mp3", ".woff", ".woff2",
    ".ttf", ".eot", ".ico",
}

NEGATIVE_URL_WORDS = {
    "login", "signin", "register", "account", "cart", "wishlist",
    "privacy", "cookie", "cookies", "help", "support", "contact",
    "terms", "conditions", "faq", "legal", "customer-service",
    "customer_service", "mentions-legales", "mentions_legales",
}

PROMO_CONTEXT_WORDS = {
    "promo", "promos", "promotion", "promotions",
    "offre", "offres", "deal", "deals",
    "soldes", "flash", "coupon", "coupons",
    "reduction", "réduction", "remise",
    "economisez", "économisez", "discount", "save",
    "special offer", "special offers",
    "offre a duree limitee", "offre à durée limitée",
    "limited time", "limited-time",
}

CARD_TAGS = {"div", "li", "article", "section", "aside"}

PERCENT_RE = re.compile(r"(?<!\d)-?\s*(\d{1,2}|[1-8]\d|9[0-5])\s*%")
PRICE_RE = re.compile(
    r"(\d{1,5}(?:[.,]\d{2})?\s?(€|eur|\$|usd|£))|"
    r"((€|eur|\$|usd|£)\s?\d{1,5}(?:[.,]\d{2})?)",
    re.IGNORECASE,
)


@dataclass
class PromoResult:
    url: str
    max_percent: int
    percents: list[int]
    snippet: str
    anchor_text: str
    source_page: str


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


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return normalize_spaces(text)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return session


def normalize_input_url(url: str) -> str:
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def canonicalize_url(url: str, base_url: str) -> Optional[str]:
    if not url:
        return None

    raw = url.strip()
    if raw.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None

    full = urljoin(base_url, raw)
    parsed = urlparse(full)

    if parsed.scheme not in {"http", "https"}:
        return None

    path_lower = parsed.path.lower()
    for ext in SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return None

    kept_params = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if any(kl.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        kept_params.append((k, v))

    cleaned = parsed._replace(
        query=urlencode(kept_params, doseq=True),
        fragment=""
    )
    return urlunparse(cleaned)


def url_is_negative(url: str) -> bool:
    folded = fold_text(urlparse(url).path + " " + urlparse(url).query)
    return any(word in folded for word in NEGATIVE_URL_WORDS)


def same_domain(url_a: str, url_b: str) -> bool:
    return urlparse(url_a).netloc.lower() == urlparse(url_b).netloc.lower()


def detect_meta_refresh(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if not meta:
        return None

    content = meta.get("content", "") or ""
    match = re.search(r"url\s*=\s*(.+)$", content, re.I)
    if not match:
        return None

    target = match.group(1).strip().strip("'\"")
    return urljoin(base_url, target)


def detect_js_redirect(html: str, base_url: str) -> Optional[str]:
    patterns = [
        r"""location\.href\s*=\s*['"]([^'"]+)['"]""",
        r"""location\.replace\(\s*['"]([^'"]+)['"]\s*\)""",
        r"""window\.location\s*=\s*['"]([^'"]+)['"]""",
        r"""window\.location\.href\s*=\s*['"]([^'"]+)['"]""",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return urljoin(base_url, match.group(1).strip())
    return None


def fetch_html_no_redirect(session: requests.Session, url: str, timeout: int) -> tuple[requests.Response, str]:
    response = session.get(url, timeout=timeout, allow_redirects=False)

    if 300 <= response.status_code < 400:
        raise RuntimeError(
            f"Redirection HTTP détectée : {response.status_code} -> {response.headers.get('Location')}"
        )

    if response.headers.get("Location"):
        raise RuntimeError(
            f"Header Location détecté : {response.headers.get('Location')}"
        )

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type:
        raise RuntimeError(f"Contenu non HTML : {content_type or 'inconnu'}")

    html = response.text

    meta_refresh = detect_meta_refresh(html, response.url)
    if meta_refresh:
        raise RuntimeError(f"Meta refresh détecté vers : {meta_refresh}")

    js_redirect = detect_js_redirect(html, response.url)
    if js_redirect:
        raise RuntimeError(f"Redirection JavaScript détectée vers : {js_redirect}")

    return response, html


def tag_text(tag: Tag, max_len: int = 1200) -> str:
    return normalize_spaces(tag.get_text(" ", strip=True))[:max_len]


def has_promo_context(text: str) -> bool:
    folded = fold_text(text)
    return any(fold_text(word) in folded for word in PROMO_CONTEXT_WORDS)


def extract_percents(text: str) -> list[int]:
    found = []
    for m in PERCENT_RE.finditer(text or ""):
        try:
            value = abs(int(m.group(1)))
        except ValueError:
            continue
        if 1 <= value <= 95:
            found.append(value)
    return sorted(set(found), reverse=True)


def price_count(text: str) -> int:
    return len(PRICE_RE.findall(text or ""))


def make_discount_snippet(text: str) -> str:
    normalized = normalize_spaces(text)
    m = PERCENT_RE.search(normalized)
    if not m:
        return normalized[:180]

    start = max(0, m.start() - 70)
    end = min(len(normalized), m.end() + 90)
    return normalized[start:end]


def anchor_display_text(a: Tag) -> str:
    parts = [
        a.get_text(" ", strip=True),
        a.get("title", ""),
        a.get("aria-label", ""),
    ]
    for img in a.find_all("img", alt=True):
        parts.append(img.get("alt", ""))
    return normalize_spaces(" ".join(filter(None, parts)))


def anchor_score(a: Tag, href: str, base_url: str, same_domain_only: bool) -> int:
    if same_domain_only and not same_domain(href, base_url):
        return -999

    if url_is_negative(href):
        return -999

    score = 0
    text = anchor_display_text(a)
    folded = fold_text(text)

    if 8 <= len(text) <= 220:
        score += 20
    elif len(text) > 220:
        score += 5

    if a.find("img"):
        score += 10

    path_parts = [p for p in urlparse(href).path.split("/") if p]
    if len(path_parts) >= 2:
        score += 8

    if text in {"+", "-", ""}:
        score -= 30

    if any(x in folded for x in ["add to cart", "ajouter", "wishlist", "compare"]):
        score -= 25

    return score


def analyze_card(tag: Tag) -> dict:
    text = tag_text(tag, max_len=1500)
    folded = fold_text(text)
    percents = extract_percents(text)
    prices = price_count(text)
    anchors = tag.find_all("a", href=True)
    images = tag.find_all("img")
    has_context = has_promo_context(text)
    negative_context = any(word in folded for word in NEGATIVE_URL_WORDS)

    score = 0

    if percents:
        score += 40
    if has_context:
        score += 22
    if prices > 0:
        score += 18
    if 1 <= len(anchors) <= 6:
        score += 12
    elif len(anchors) > 8:
        score -= min(30, len(anchors) * 3)
    if len(images) >= 1:
        score += 8
    if 25 <= len(text) <= 600:
        score += 12
    elif len(text) > 900:
        score -= 25
    if len(percents) > 3:
        score -= 15
    if negative_context:
        score -= 20

    return {
        "score": score,
        "text": text,
        "percents": percents,
        "prices": prices,
        "has_context": has_context,
        "snippet": make_discount_snippet(text),
        "anchor_count": len(anchors),
    }


def nearest_qualifying_card(
    a: Tag,
    min_discount: int,
    card_threshold: int,
    cache: dict[int, dict],
) -> tuple[Optional[Tag], Optional[dict]]:
    current: Optional[Tag] = a
    for _ in range(6):
        if current is None:
            break
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        current = parent

        if current.name not in CARD_TAGS:
            continue

        key = id(current)
        if key not in cache:
            cache[key] = analyze_card(current)
        info = cache[key]

        if (
            info["score"] >= card_threshold
            and info["percents"]
            and max(info["percents"]) >= min_discount
            and (info["has_context"] or info["prices"] > 0)
        ):
            return current, info

    return None, None


def choose_best_product_anchor(
    card: Tag,
    base_url: str,
    same_domain_only: bool,
) -> tuple[Optional[str], str]:
    best_url: Optional[str] = None
    best_text = ""
    best_score = -999

    for a in card.find_all("a", href=True):
        href = canonicalize_url(a.get("href"), base_url)
        if not href:
            continue

        score = anchor_score(a, href, base_url, same_domain_only=same_domain_only)
        if score <= best_score:
            continue

        best_score = score
        best_url = href
        best_text = anchor_display_text(a)

    return best_url, best_text


def detect_promo_urls_in_page(
    html: str,
    page_url: str,
    same_domain_only: bool,
    min_discount: int,
    card_threshold: int,
) -> dict[str, PromoResult]:
    soup = BeautifulSoup(html, "lxml")
    cache: dict[int, dict] = {}
    results: dict[str, PromoResult] = {}

    for a in soup.find_all("a", href=True):
        card, card_info = nearest_qualifying_card(
            a=a,
            min_discount=min_discount,
            card_threshold=card_threshold,
            cache=cache,
        )
        if card is None or card_info is None:
            continue

        best_url, best_text = choose_best_product_anchor(
            card=card,
            base_url=page_url,
            same_domain_only=same_domain_only,
        )
        if not best_url:
            continue

        if url_is_negative(best_url):
            continue

        existing = results.get(best_url)
        new_max = max(card_info["percents"]) if card_info["percents"] else 0

        if existing is None or new_max > existing.max_percent:
            results[best_url] = PromoResult(
                url=best_url,
                max_percent=new_max,
                percents=card_info["percents"],
                snippet=card_info["snippet"],
                anchor_text=best_text,
                source_page=page_url,
            )

    return results


def extract_child_links(
    html: str,
    base_url: str,
    same_domain_only: bool,
    max_links_per_page: int,
) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []

    for a in soup.find_all("a", href=True):
        href = canonicalize_url(a.get("href"), base_url)
        if not href:
            continue
        if url_is_negative(href):
            continue
        if same_domain_only and not same_domain(href, base_url):
            continue
        links.append(href)

    uniq = sorted(set(links))

    def priority(url: str) -> tuple[int, str]:
        folded = fold_text(url)
        promo_hint = any(fold_text(w) in folded for w in PROMO_CONTEXT_WORDS)
        return (0 if promo_hint else 1, url)

    uniq.sort(key=priority)
    return uniq[:max_links_per_page]


def crawl_for_promos(
    start_url: str,
    timeout: int,
    max_pages: int,
    max_depth: int,
    same_domain_only: bool,
    min_discount: int,
    card_threshold: int,
    max_links_per_page: int,
    verbose: bool,
) -> dict[str, PromoResult]:
    session = build_session()
    start_url = normalize_input_url(start_url)

    queue = deque([(start_url, 0)])
    queued = {start_url}
    visited = set()
    found: dict[str, PromoResult] = {}

    while queue and len(visited) < max_pages:
        current_url, depth = queue.popleft()
        if current_url in visited:
            continue
        visited.add(current_url)

        if verbose:
            print(f"\n[PAGE {len(visited)}/{max_pages}] profondeur={depth} -> {current_url}")

        try:
            response, html = fetch_html_no_redirect(session, current_url, timeout)
        except Exception as e:
            if verbose:
                print(f"  ignorée : {e}")
            continue

        page_results = detect_promo_urls_in_page(
            html=html,
            page_url=response.url,
            same_domain_only=same_domain_only,
            min_discount=min_discount,
            card_threshold=card_threshold,
        )

        for url, result in page_results.items():
            previous = found.get(url)
            if previous is None or result.max_percent > previous.max_percent:
                found[url] = result

        if verbose and page_results:
            print(f"  promos détectées : {len(page_results)}")

        if depth < max_depth:
            children = extract_child_links(
                html=html,
                base_url=response.url,
                same_domain_only=same_domain_only,
                max_links_per_page=max_links_per_page,
            )
            for child in children:
                if child not in visited and child not in queued:
                    queue.append((child, depth + 1))
                    queued.add(child)

    return found


def save_urls_only(path: Path, results: dict[str, PromoResult]) -> None:
    urls = sorted(results.keys())
    path.write_text("\n".join(urls), encoding="utf-8")


def print_summary(results: dict[str, PromoResult], limit: int = 50) -> None:
    ordered = sorted(results.values(), key=lambda r: (-r.max_percent, r.url))

    print("\n" + "=" * 100)
    print("RÉSULTATS")
    print("=" * 100)
    print(f"URLs avec promo locale détectée : {len(ordered)}")
    print("=" * 100)

    for i, r in enumerate(ordered[:limit], start=1):
        print(f"{i:03d} | max={r.max_percent:02d}% | {r.url}")
        print(f"      source : {r.source_page}")
        print(f"      extrait: {r.snippet[:180]}")

    if len(ordered) > limit:
        print(f"... {len(ordered) - limit} autres URLs dans le fichier.")

    print("=" * 100)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyse un HTML page par page, repère localement les cartes avec % de promo, "
            "et extrait uniquement les URLs associées à ces cartes."
        )
    )
    parser.add_argument("--url", required=False, help="URL de départ")
    parser.add_argument("--timeout", type=int, default=20, help="Timeout HTTP")
    parser.add_argument("--max-pages", type=int, default=20, help="Nombre max de pages à analyser")
    parser.add_argument("--max-depth", type=int, default=1, help="Profondeur max de crawl")
    parser.add_argument("--min-discount", type=int, default=1, help="Pourcentage minimum à retenir")
    parser.add_argument("--card-threshold", type=int, default=55, help="Seuil minimum pour considérer un bloc DOM comme une carte promo")
    parser.add_argument("--same-domain-only", action="store_true", help="Limiter au même domaine")
    parser.add_argument("--max-links-per-page", type=int, default=120, help="Nombre max de liens explorés par page")
    parser.add_argument("--output", default="urls_promos.txt", help="Fichier de sortie")
    parser.add_argument("--verbose", action="store_true", help="Affichage détaillé")
    args = parser.parse_args(argv)

    try:
        url = ask_input_if_missing(args.url, "Entre l'URL à analyser")

        results = crawl_for_promos(
            start_url=url,
            timeout=args.timeout,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            same_domain_only=args.same_domain_only,
            min_discount=args.min_discount,
            card_threshold=args.card_threshold,
            max_links_per_page=args.max_links_per_page,
            verbose=args.verbose,
        )

        save_urls_only(Path(args.output), results)
        print_summary(results)
        print(f"\nFichier de sortie : {Path(args.output).resolve()}")
        return 0

    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 1
    except requests.RequestException as e:
        print(f"\nErreur HTTP : {e}")
        return 1
    except Exception as e:
        print(f"\nErreur : {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))