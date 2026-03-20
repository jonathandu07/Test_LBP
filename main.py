#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# Mots-clés positifs : indices qu'un lien parle d'une promo
PROMO_KEYWORDS = {
    "promo", "promos", "promotion", "promotions",
    "deal", "deals",
    "offre", "offres",
    "flash", "vente flash", "ventes flash",
    "bon plan", "bons plans",
    "coupon", "coupons",
    "soldes", "remise", "reduction", "réduction",
    "discount", "save", "saving",
    "economisez", "économisez",
    "special offer", "special offers",
    "black friday", "cyber monday",
}

# Mots-clés négatifs : indices qu'un lien n'est pas une promo utile
NEGATIVE_KEYWORDS = {
    "signin", "login", "register", "account",
    "privacy", "cookies", "help", "support",
    "customer service", "cart", "wishlist",
    "footer", "language", "country", "careers",
    "mentions legales", "contact", "terms",
    "conditions", "politique", "confidentialite",
    "confidentialité",
}

# Paramètres de tracking qu'on enlève
TRACKING_QUERY_PREFIXES = (
    "utm_", "ref", "fbclid", "gclid", "msclkid",
    "aff", "affiliate", "affid", "tag", "source",
)

PRICE_PATTERNS = [
    re.compile(r"\b\d{1,5}[.,]\d{2}\s?(€|eur|\$|usd|£)\b", re.I),
    re.compile(r"\b\d{1,5}\s?(€|eur|\$|usd|£)\b", re.I),
]

DISCOUNT_PATTERNS = [
    re.compile(r"-\s?\d{1,3}\s?%", re.I),
    re.compile(r"\b\d{1,3}\s?%\s?(de reduction|de réduction|off)\b", re.I),
    re.compile(r"\beconomisez\b", re.I),
    re.compile(r"\béconomisez\b", re.I),
    re.compile(r"\bremise\b", re.I),
    re.compile(r"\bcoupon\b", re.I),
    re.compile(r"\bcode promo\b", re.I),
]


@dataclass(frozen=True)
class CandidateURL:
    url: str
    score: int
    reason: str
    anchor_text: str


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
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return s


def fetch_html_no_redirect(url: str, timeout: int) -> tuple[requests.Response, str]:
    session = build_session()
    response = session.get(url, timeout=timeout, allow_redirects=False)

    content_type = (response.headers.get("Content-Type") or "").lower()
    html = response.text if "html" in content_type else ""
    return response, html


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
        m = re.search(pattern, html, re.I)
        if m:
            return urljoin(base_url, m.group(1).strip())
    return None


def forbid_redirects(response: requests.Response, html: str) -> None:
    if 300 <= response.status_code < 400:
        raise RuntimeError(
            f"Redirection HTTP détectée : code {response.status_code}, "
            f"Location={response.headers.get('Location')!r}"
        )

    if response.headers.get("Location"):
        raise RuntimeError(
            f"Header Location détecté : {response.headers.get('Location')!r}"
        )

    meta_refresh = detect_meta_refresh(html, response.url) if html else None
    if meta_refresh:
        raise RuntimeError(f"Meta refresh détecté vers : {meta_refresh}")

    js_redirect = detect_js_redirect(html, response.url) if html else None
    if js_redirect:
        raise RuntimeError(f"Redirection JavaScript détectée vers : {js_redirect}")


def canonicalize_url(url: str, base_url: str) -> Optional[str]:
    full = urljoin(base_url, url)
    parsed = urlparse(full)

    if parsed.scheme not in {"http", "https"}:
        return None

    cleaned_query = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith(TRACKING_QUERY_PREFIXES):
            continue
        cleaned_query.append((k, v))

    cleaned = parsed._replace(
        query=urlencode(cleaned_query, doseq=True),
        fragment=""
    )
    return urlunparse(cleaned)


def nearest_container_text(a: Tag, max_len: int = 700) -> str:
    node: Optional[Tag] = a
    for _ in range(5):
        if not isinstance(node, Tag):
            break
        text = normalize_spaces(node.get_text(" ", strip=True))
        if len(text) >= 40:
            return text[:max_len]
        node = node.parent if isinstance(node.parent, Tag) else None
    return normalize_spaces(a.get_text(" ", strip=True))[:max_len]


def count_keyword_hits(text: str, keywords: Iterable[str]) -> int:
    t = fold_text(text)
    hits = 0
    for kw in keywords:
        if fold_text(kw) in t:
            hits += 1
    return hits


def has_price_like_text(text: str) -> bool:
    return any(p.search(text or "") for p in PRICE_PATTERNS)


def has_discount_like_text(text: str) -> bool:
    return any(p.search(text or "") for p in DISCOUNT_PATTERNS)


def looks_like_navigation_or_useless(url: str) -> bool:
    p = urlparse(url)
    path = fold_text(p.path)
    return any(bad in path for bad in NEGATIVE_KEYWORDS)


def score_anchor(a: Tag, base_url: str, same_domain_only: bool) -> Optional[CandidateURL]:
    href = a.get("href")
    if not href:
        return None

    canonical = canonicalize_url(href, base_url)
    if not canonical:
        return None

    base_host = urlparse(base_url).netloc.lower()
    host = urlparse(canonical).netloc.lower()

    if same_domain_only and host != base_host:
        return None

    if looks_like_navigation_or_useless(canonical):
        return None

    anchor_text = normalize_spaces(a.get_text(" ", strip=True))
    title_text = normalize_spaces(a.get("title", ""))
    aria_text = normalize_spaces(a.get("aria-label", ""))
    context_text = nearest_container_text(a)

    href_low = fold_text(canonical)
    anchor_low = fold_text(anchor_text)
    title_low = fold_text(title_text)
    aria_low = fold_text(aria_text)
    context_low = fold_text(context_text)

    score = 0
    reasons: list[str] = []

    promo_hits = (
        count_keyword_hits(href_low, PROMO_KEYWORDS)
        + count_keyword_hits(anchor_low, PROMO_KEYWORDS)
        + count_keyword_hits(title_low, PROMO_KEYWORDS)
        + count_keyword_hits(aria_low, PROMO_KEYWORDS)
        + count_keyword_hits(context_low, PROMO_KEYWORDS)
    )
    if promo_hits:
        score += promo_hits * 8
        reasons.append(f"keywords={promo_hits}")

    neg_hits = (
        count_keyword_hits(anchor_low, NEGATIVE_KEYWORDS)
        + count_keyword_hits(context_low, NEGATIVE_KEYWORDS)
    )
    if neg_hits:
        score -= neg_hits * 8
        reasons.append(f"negative={neg_hits}")

    if has_discount_like_text(anchor_text):
        score += 22
        reasons.append("discount-anchor")

    if has_discount_like_text(context_text):
        score += 18
        reasons.append("discount-context")

    if has_price_like_text(context_text):
        score += 10
        reasons.append("price-context")

    if any(token in href_low for token in ["/deal", "/deals", "/promo", "/promotions", "/coupon", "/soldes", "/offre"]):
        score += 20
        reasons.append("promo-href")

    if any(x in anchor_low for x in ["voir l'offre", "voir loffre", "voir l offre", "profiter", "coupon", "offre", "promo"]):
        score += 16
        reasons.append("cta-anchor")

    deep_path = len([p for p in urlparse(canonical).path.split("/") if p]) >= 2
    if deep_path and (promo_hits > 0 or has_discount_like_text(context_text)):
        score += 8
        reasons.append("deep-path")

    if score < 18:
        return None

    return CandidateURL(
        url=canonical,
        score=score,
        reason=", ".join(reasons) if reasons else "heuristic",
        anchor_text=anchor_text,
    )


def extract_promo_urls(html: str, base_url: str, same_domain_only: bool, min_score: int) -> list[CandidateURL]:
    soup = BeautifulSoup(html, "lxml")
    best_by_url: dict[str, CandidateURL] = {}

    for a in soup.find_all("a", href=True):
        cand = score_anchor(a, base_url=base_url, same_domain_only=same_domain_only)
        if not cand:
            continue
        if cand.score < min_score:
            continue

        prev = best_by_url.get(cand.url)
        if prev is None or cand.score > prev.score:
            best_by_url[cand.url] = cand

    return sorted(best_by_url.values(), key=lambda c: (-c.score, c.url))


def save_urls_only(path: Path, candidates: list[CandidateURL], with_scores: bool) -> None:
    lines = []
    for c in candidates:
        if with_scores:
            lines.append(f"{c.score:03d} | {c.url} | {c.reason} | {c.anchor_text}")
        else:
            lines.append(c.url)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse le HTML d'une page en mémoire et extrait les URLs qui ressemblent à des promos."
    )
    parser.add_argument("--url", required=False, help="URL à analyser")
    parser.add_argument("--timeout", type=int, default=20, help="Timeout HTTP")
    parser.add_argument("--output", default="urls_promos.txt", help="Fichier de sortie")
    parser.add_argument("--min-score", type=int, default=18, help="Score minimum")
    parser.add_argument("--same-domain-only", action="store_true", help="Ne garder que les URLs du même domaine")
    parser.add_argument("--with-scores", action="store_true", help="Écrire score et raison")
    args = parser.parse_args(argv)

    try:
        url = ask_input_if_missing(args.url, "Entre l'URL à analyser")
        if not urlparse(url).scheme:
            url = "https://" + url.strip()

        response, html = fetch_html_no_redirect(url=url, timeout=args.timeout)
        forbid_redirects(response, html)

        if response.status_code != 200:
            raise RuntimeError(f"Réponse HTTP non exploitable : {response.status_code}")

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            raise RuntimeError(f"Contenu non HTML : {content_type or 'inconnu'}")

        candidates = extract_promo_urls(
            html=html,
            base_url=response.url,
            same_domain_only=args.same_domain_only,
            min_score=args.min_score,
        )

        save_urls_only(Path(args.output), candidates, with_scores=args.with_scores)

        print("\n" + "=" * 90)
        print("ANALYSE HTML")
        print("=" * 90)
        print(f"URL analysée       : {response.url}")
        print(f"Code HTTP          : {response.status_code}")
        print(f"URLs retenues      : {len(candidates)}")
        print(f"Fichier de sortie  : {Path(args.output).resolve()}")
        print("=" * 90)

        for i, c in enumerate(candidates[:50], start=1):
            if args.with_scores:
                print(f"{i:03d} | {c.score:03d} | {c.url} | {c.reason}")
            else:
                print(f"{i:03d} | {c.url}")

        if len(candidates) > 50:
            print(f"... {len(candidates) - 50} autres URLs dans le fichier.")

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