import asyncio
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

BASE_URL = os.getenv("BASE_URL", "https://www.egybest.co.in").rstrip("/")
DATA_FILE = Path("data/embeds.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
NOISE = {
    "hd", "cam", "webrip", "bluray", "mtrjm", "fylm", "mslsl",
    "مشاهدة", "فيلم", "مسلسل", "مترجم", "مترجمة", "مدبلج", "اون", "أون", "لاين", "اونلاين",
}


def norm(s: str) -> str:
    s = (s or "").translate(AR_DIGITS).lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_tokens(text: str, year: str = "") -> list[str]:
    toks = [w for w in norm(text).split() if w not in NOISE and w != year]
    return [w for w in toks if len(w) > 1 or w.isdigit()]


def make_job_key(title: str, year: str, season: int | None, episode: int | None) -> str:
    base = f"{norm(title)}|{year.strip()}"
    if season is not None or episode is not None:
        return f"{base}|s{season or 1}|e{episode or 1}"
    return f"{base}|movie"


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


async def click_search_icon_and_type(page, title: str) -> bool:
    icon_selectors = [
        "button[aria-label*='بحث']",
        "button[aria-label*='Search' i]",
        "[class*='search'] button",
        "[class*='search-icon']",
        "[class*='searchIcon']",
        "header button",
        "nav button",
    ]
    input_selectors = [
        "input[type='search']",
        "input[placeholder*='بحث']",
        "input[placeholder*='Search' i]",
        "header input",
        "nav input",
        "input[name*='s']",
        "input[class*='search' i]",
        "input[id*='search' i]",
    ]

    async def get_visible_input():
        for sel in input_selectors:
            loc = page.locator(sel)
            cnt = await loc.count()
            for i in range(min(cnt, 5)):
                item = loc.nth(i)
                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    pass
        return None

    box = await get_visible_input()
    if not box:
        for sel in icon_selectors:
            loc = page.locator(sel)
            cnt = await loc.count()
            for i in range(min(cnt, 8)):
                btn = loc.nth(i)
                try:
                    if await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.8)
                        box = await get_visible_input()
                        if box:
                            break
                except Exception:
                    pass
            if box:
                break

    if not box:
        await page.keyboard.press("/")
        await asyncio.sleep(0.8)
        box = await get_visible_input()

    if not box:
        return False

    await box.click()
    await box.fill("")
    await box.type(title, delay=110)
    return True


async def fetch_search_results_via_ui(page, title: str) -> list[dict]:
    captured_results = []

    async def on_response(resp):
        nonlocal captured_results
        try:
            url = resp.url
            if "loader=searchPage" not in url and "/search/" not in url:
                return
            if resp.status != 200:
                return
            txt = await resp.text()
            data = json.loads(txt)
            if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
                captured_results = data["results"]
        except Exception:
            pass

    page.on("response", on_response)

    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(2.5)

    typed = await click_search_icon_and_type(page, title)
    if not typed:
        page.remove_listener("response", on_response)
        return []

    for _ in range(16):
        if captured_results:
            break
        await asyncio.sleep(0.5)

    page.remove_listener("response", on_response)
    return captured_results


async def open_season_and_episode(page, title_base_url: str, season: int, episode: int):
    season_url = f"{title_base_url}/season/{season}"
    await page.goto(season_url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(2)

    body_text = await page.inner_text("body")
    if "404" in body_text:
        season_btn = page.locator("a, button, div").filter(has_text=re.compile(rf"^\s*{season}\s*$")).first
        if await season_btn.count() > 0:
            await season_btn.click()
            await asyncio.sleep(2)

    ep_url = f"{title_base_url}/season/{season}/episode/{episode}"
    await page.goto(ep_url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(2)

    ep_body = await page.inner_text("body")
    if "404" in ep_body:
        ep_pad = f"{episode:02d}"
        ep_loc = page.locator("a, div, li").filter(
            has_text=re.compile(rf"(الحلقة\s*0?{episode}\b|episode\s*0?{episode}\b|\b{ep_pad}\b)", re.I)
        ).first
        if await ep_loc.count() > 0:
            await ep_loc.scroll_into_view_if_needed()
            await ep_loc.click()
            await asyncio.sleep(2)


async def go_to_watch_page(page) -> bool:
    if "/watch/" in page.url:
        return True

    watch_locators = [
        page.locator("a, button").filter(has_text=re.compile(r"شاهد\s*الآن|مشاهدة|watch", re.I)),
        page.locator("a[href*='/watch/']"),
    ]

    for loc in watch_locators:
        cnt = await loc.count()
        for i in range(cnt):
            el = loc.nth(i)
            try:
                if await el.is_visible():
                    href = await el.get_attribute("href")
                    if href and "/watch/" in href:
                        watch_url = urljoin(page.url, href)
                        await page.goto(watch_url, wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(2.5)
                        return True

                    await el.scroll_into_view_if_needed()
                    await el.click()
                    for _ in range(12):
                        if "/watch/" in page.url:
                            return True
                        await asyncio.sleep(0.5)
            except Exception:
                pass

    return "/watch/" in page.url


async def extract_single_embed(page, wait_time: int = 8) -> str | None:
    found, seen = [], set()
    site_host = urlparse(BASE_URL).netloc

    def add(u):
        if not u or not u.startswith("http"):
            return
        if u not in seen:
            seen.add(u)
            found.append(u)

    page.on("framenavigated", lambda fr: add(fr.url) if fr != page.main_frame else None)
    await asyncio.sleep(wait_time)

    dom = await page.evaluate(
        """() => {
            const out = [];
            for (const el of document.querySelectorAll("iframe, video source, [data-embed], [data-src], [data-url]")) {
                for (const attr of ["src", "data-src", "data-lazy-src", "data-embed", "data-url"]) {
                    const v = el.getAttribute(attr);
                    if (v && /^(https?:)?\\/\\//.test(v)) out.push(v);
                }
            }
            return out;
        }"""
    )
    for u in dom:
        add(urljoin(page.url, u))

    for fr in page.frames:
        if fr != page.main_frame:
            add(fr.url)

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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()

        try:
            results = await fetch_search_results_via_ui(page, title)
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
            title_url = f"{BASE_URL}/titles/{item_id}/{slug}" if slug else f"{BASE_URL}/titles/{item_id}"

            await page.goto(title_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2)

            body_text = await page.inner_text("body")
            if "404" in body_text or "/titles/" not in page.url:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)
                await click_search_icon_and_type(page, title)
                await asyncio.sleep(2.5)
                cand = page.locator("a, div[role='button'], li").filter(
                    has_text=chosen.get("original_title") or title
                ).first
                if await cand.count() > 0:
                    await cand.click()
                    await asyncio.sleep(2.5)
                title_url = page.url.rstrip("/")

            if is_series:
                s_num = season if season is not None else 1
                e_num = episode if episode is not None else 1
                await open_season_and_episode(page, title_url.rstrip("/"), s_num, e_num)

            await go_to_watch_page(page)
            embed_url = await extract_single_embed(page, wait_time=8)

            if not embed_url:
                return {
                    "ok": False,
                    "error": "embed_not_found",
                    "watch_url": page.url,
                    "embed_url": None,
                }

            return {
                "ok": True,
                "title": chosen.get("original_title") or chosen.get("name") or title,
                "year": chosen.get("year"),
                "season": season if is_series else None,
                "episode": episode if is_series else None,
                "watch_url": page.url,
                "embed_url": embed_url,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as exc:
            return {"ok": False, "error": str(exc), "embed_url": None}
        finally:
            await browser.close()


def save_result_to_json(job_key: str, result: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        try:
            store = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            store = {"items": {}}
    else:
        store = {"items": {}}

    if not isinstance(store.get("items"), dict):
        store["items"] = {}

    store["items"][job_key] = result
    store["latest_key"] = job_key
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


async def main():
    title = os.getenv("TITLE", "").strip()
    year = os.getenv("YEAR", "").strip()
    season_raw = os.getenv("SEASON", "").strip()
    episode_raw = os.getenv("EPISODE", "").strip()

    season = int(season_raw) if season_raw.isdigit() and int(season_raw) >= 1 else None
    episode = int(episode_raw) if episode_raw.isdigit() and int(episode_raw) >= 1 else None

    if not title:
        print(json.dumps({"ok": False, "error": "missing_title"}, ensure_ascii=False))
        raise SystemExit(1)

    job_key = make_job_key(title, year, season, episode)
    result = await extract_embed_job(title=title, year=year, season=season, episode=episode)
    result["job_key"] = job_key

    if result.get("ok") and result.get("embed_url"):
        save_result_to_json(job_key, result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
