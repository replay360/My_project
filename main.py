import os
import re
import time
import unicodedata
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

BASE_URL = os.getenv("BASE_URL", "https://www.egybest.co.in").rstrip("/")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Referer": BASE_URL + "/",
}

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
NOISE = {
    "hd", "cam", "webrip", "bluray", "mtrjm", "fylm", "mslsl",
    "مشاهدة", "فيلم", "مسلسل", "مترجم", "مترجمة", "مدبلج", "اون", "أون", "لاين", "اونلاين",
}

CACHE_TTL_SECONDS = 6 * 3600
_CACHE: dict[str, tuple[float, dict]] = {}

app = FastAPI(title="Embed Extractor API (No Browser)", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def norm(s: str) -> str:
    s = (s or "").translate(AR_DIGITS).lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_tokens(text: str, year: str = "") -> list[str]:
    toks = [w for w in norm(text).split() if w not in NOISE and w != year]
    return [w for w in toks if len(w) > 1 or w.isdigit()]


def _make_cache_key(title: str, year: str, season: int | None, episode: int | None) -> str:
    return f"{norm(title)}|{year.strip()}|{season or ''}|{episode or ''}"


def _get_cached(key: str) -> dict | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if time.monotonic() > expires_at:
        _CACHE.pop(key, None)
        return None
    out = dict(payload)
    out["cached"] = True
    return out


def _set_cached(key: str, payload: dict) -> None:
    if payload.get("ok") and payload.get("embed_url"):
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, dict(payload))


def pick_best_result(results: list[dict], title: str, year: str = "", prefer_series: bool = False) -> dict | None:
    if not results:
        return None

    target_norm = norm(title)
    target_tokens = clean_tokens(title, year)
    target_year = int(year) if year.isdigit() else None

    best_item = None
    best_score = -10**9

    for item in results:
        is_series = bool(item.get("is_series")) or (
            str(item.get("type") or "").lower() in {"series", "tv", "show"}
        )

        if prefer_series and not is_series:
            continue
        if not prefer_series and is_series:
            continue

        orig = norm(item.get("original_title") or "")
        name = norm(item.get("name") or "")
        combined = f"{orig} {name}".strip()
        item_year = item.get("year")

        hits = sum(1 for w in target_tokens if f" {w} " in f" {combined} ")
        if target_tokens and hits == 0:
            continue

        score = hits * 20

        if target_norm and orig == target_norm:
            score += 60
        elif target_norm and target_norm in orig:
            score += 35
        elif target_norm and target_norm in name:
            score += 25

        extra_penalty_words = {"making", "behind", "scenes", "documentary", "وثائقي", "كواليس"}
        if any(f" {pw} " in f" {orig} " and f" {pw} " not in f" {target_norm} " for pw in extra_penalty_words):
            score -= 35

        if target_year is not None and item_year is not None:
            try:
                if int(item_year) == target_year:
                    score += 40
                else:
                    score -= 60
            except ValueError:
                pass

        if item.get("fully_synced"):
            score += 5

        if score > best_score:
            best_score = score
            best_item = item

    return best_item


def build_slug(item: dict, is_series: bool = False) -> str:
    name = (item.get("name") or "").strip().lower()
    orig = (item.get("original_title") or "").strip().lower()
    year = str(item.get("year") or "").strip()

    replacements = {
        "فيلم": "fylm",
        "مسلسل": "mslsl",
        "مترجم": "mtrjm",
        "مترجمة": "mtrjm",
        "مدبلج": "mdblj",
    }
    temp = name
    for ar, en in replacements.items():
        temp = re.sub(rf"\b{re.escape(ar)}\b", en, temp)

    slug = re.sub(r"[^a-z0-9]+", "-", temp).strip("-")
    if not slug and orig:
        slug = re.sub(r"[^a-z0-9]+", "-", orig).strip("-")
        if year and year not in slug:
            slug = f"{slug}-{year}"
        if is_series and not slug.endswith("-mslsl"):
            slug = f"{slug}-mslsl"
    return slug


def _parse_results_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_hrefs = set()

    for a in soup.select("a[href*='/titles/']"):
        href = (a.get("href") or "").strip()
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        m = re.search(r"/titles/(\d+)(?:/([^/?#]+))?", href)
        if not m:
            continue

        item_id = m.group(1)
        slug = m.group(2) or ""
        text = " ".join(a.get_text(" ", strip=True).split())
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", f"{text} {slug}")
        is_series = bool(re.search(r"mslsl|series|مسلسل", f"{text} {slug}", re.I))

        items.append({
            "id": item_id,
            "name": text or slug,
            "original_title": text or slug,
            "year": int(year_match.group(1)) if year_match else None,
            "is_series": is_series,
            "type": "series" if is_series else "movie",
            "url": urljoin(BASE_URL + "/", href),
        })

    return items


async def search_titles_http(client: httpx.AsyncClient, title: str) -> list[dict]:
    encoded = quote(title)
    candidate_urls = [
        f"{BASE_URL}/?loader=searchPage&q={encoded}",
        f"{BASE_URL}/search/{encoded}",
        f"{BASE_URL}/?s={encoded}",
    ]

    for url in candidate_urls:
        try:
            resp = await client.get(url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"})
            if resp.status_code != 200:
                continue

            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype or resp.text.strip().startswith("{"):
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
                    return data["results"]

            parsed = _parse_results_from_html(resp.text)
            if parsed:
                return parsed
        except Exception:
            continue

    return []


def _extract_watch_url(html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        txt = a.get_text(" ", strip=True)
        if "/watch/" in href or re.search(r"شاهد\s*الآن|مشاهدة|watch", txt, re.I):
            full = urljoin(current_url, href)
            if "/watch/" in full:
                return full

    m = re.search(r"""["']([^"']*?/watch/[^"']*)["']""", html)
    if m:
        return urljoin(current_url, m.group(1))

    return None


def _extract_best_embed_from_html(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    site_host = urlparse(BASE_URL).netloc
    found: list[str] = []
    seen: set[str] = set()

    def add(u: str | None):
        if not u:
            return
        u = u.strip()
        if u.startswith("//"):
            u = "https:" + u
        full = urljoin(page_url, u)
        if not full.startswith("http"):
            return
        if full not in seen:
            seen.add(full)
            found.append(full)

    for el in soup.select("iframe, video, source, [data-embed], [data-src], [data-url], [data-lazy-src]"):
        for attr in ("src", "data-src", "data-lazy-src", "data-embed", "data-url"):
            val = el.get(attr)
            if val and not val.startswith("data:") and not val.startswith("javascript:"):
                add(val)

    for m in re.finditer(r"""https?://[^\s"'<>\\]+""", html):
        candidate = m.group(0).rstrip(").,;\"'")
        if any(k in candidate.lower() for k in ("embed", "player", "stream", "vid", "m3u8", "mp4")):
            if urlparse(candidate).netloc != site_host:
                add(candidate)

    if not found:
        return None

    found.sort(key=lambda u: urlparse(u).netloc == site_host)
    return found[0]


async def extract_embed_job(
    title: str,
    year: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> dict:
    prefer_series = season is not None or episode is not None

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(25.0, connect=12.0),
        headers=HEADERS,
    ) as client:
        try:
            results = await search_titles_http(client, title)
            if not results:
                return {"ok": False, "error": "no_search_results", "embed_url": None}

            chosen = pick_best_result(results, title, year, prefer_series=prefer_series)
            if not chosen or not chosen.get("id"):
                return {"ok": False, "error": "no_matching_title", "embed_url": None}

            item_id = chosen["id"]
            is_series = (
                bool(chosen.get("is_series"))
                or (str(chosen.get("type") or "").lower() in {"series", "tv", "show"})
                or prefer_series
            )
            slug = build_slug(chosen, is_series=is_series)
            title_url = (
                chosen.get("url")
                or (f"{BASE_URL}/titles/{item_id}/{slug}" if slug else f"{BASE_URL}/titles/{item_id}")
            ).rstrip("/")

            target_url = title_url
            if is_series:
                s_num = season if season is not None else 1
                e_num = episode if episode is not None else 1
                target_url = f"{title_url}/season/{s_num}/episode/{e_num}"

            resp = await client.get(target_url)
            if resp.status_code >= 400 and is_series:
                s_num = season if season is not None else 1
                e_num = episode if episode is not None else 1
                season_resp = await client.get(f"{title_url}/season/{s_num}")
                if season_resp.status_code == 200:
                    soup = BeautifulSoup(season_resp.text, "html.parser")
                    ep_link = soup.find(
                        "a",
                        href=re.compile(rf"/season/{s_num}/episode/0?{e_num}\b", re.I),
                    )
                    if ep_link and ep_link.get("href"):
                        target_url = urljoin(str(season_resp.url), ep_link["href"])
                        resp = await client.get(target_url)

            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"page_http_{resp.status_code}",
                    "watch_url": str(resp.url),
                    "embed_url": None,
                }

            current_html = resp.text
            watch_url = str(resp.url)

            if "/watch/" not in watch_url:
                found_watch = _extract_watch_url(current_html, watch_url)
                if found_watch:
                    w_resp = await client.get(found_watch)
                    if w_resp.status_code == 200:
                        current_html = w_resp.text
                        watch_url = str(w_resp.url)

            embed_url = _extract_best_embed_from_html(current_html, watch_url)
            if not embed_url:
                return {
                    "ok": False,
                    "error": "embed_not_found_without_browser",
                    "watch_url": watch_url,
                    "embed_url": None,
                }

            return {
                "ok": True,
                "title": chosen.get("original_title") or chosen.get("name") or title,
                "year": chosen.get("year"),
                "season": season if is_series else None,
                "episode": episode if is_series else None,
                "watch_url": watch_url,
                "embed_url": embed_url,
                "cached": False,
            }

        except Exception as exc:
            return {"ok": False, "error": str(exc), "embed_url": None}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/extract")
async def extract(
    title: str = Query(..., min_length=1),
    year: str = Query(""),
    season: int | None = Query(None, ge=1),
    episode: int | None = Query(None, ge=1),
):
    key = _make_cache_key(title, year, season, episode)
    cached = _get_cached(key)
    if cached:
        return cached

    result = await extract_embed_job(title=title, year=year, season=season, episode=episode)
    _set_cached(key, result)
    return result
