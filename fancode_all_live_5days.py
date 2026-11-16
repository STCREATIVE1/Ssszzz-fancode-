import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

def detect_sport_from_url(url: str) -> str:
    u = url.lower()
    if "/football/" in u:
        return "Football"
    if "/kabaddi/" in u:
        return "Kabaddi"
    if "/tennis/" in u or "/lawn-tennis/" in u:
        return "Tennis"
    if "/formula1/" in u or "/motogp/" in u or "/motorsport/" in u:
        return "Motorsport"
    if "/badminton/" in u:
        return "Badminton"
    if "/basketball/" in u:
        return "Basketball"
    if "/golf/" in u:
        return "Golf"
    if "/hockey/" in u:
        return "Hockey"
    return "Cricket"

async def extract_from_page(page, url: str, default_status: str):
    print(f"🟢 Scanning: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=35000)
    await asyncio.sleep(3)

    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 1000);")
        await asyncio.sleep(0.8)

    return await page.evaluate(r"""(defStatus) => {
        let extracted = [];

        // 1. Embedded Next.js / Apollo State Scan
        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                if ((node.matchId || node.id) && (node.teams || node.startTime || node.slug)) {
                    let mid = node.matchId || node.id;
                    let t1 = '', t2 = '';
                    if (Array.isArray(node.teams) && node.teams.length >= 2) {
                        t1 = node.teams[0].name || '';
                        t2 = node.teams[1].name || '';
                    }
                    extracted.push({
                        match_id: mid,
                        team_1: t1,
                        team_2: t2,
                        name: node.name || node.title || '',
                        event_name: node.tourName || node.seriesName || '',
                        src: node.src || node.image || '',
                        startTime: node.startTime || '',
                        status: node.status || defStatus,
                        url: node.url || node.deepLink || ''
                    });
                }
                for (let k in node) scan(node[k]);
            };
            if (window.__NEXT_DATA__) scan(window.__NEXT_DATA__);
            if (window.__INIT_STATE__) scan(window.__INIT_STATE__);
        } catch(e) {}

        // 2. DOM Match Cards + Direct Match Links Scan (Fixed RegExp)
        let idRegex = new RegExp('[-/](\\d{6,8})(?:/|$)');
        let links = document.querySelectorAll('a[href*="/matches/"]');
        links.forEach(a => {
            let href = a.href ? a.href.split('?')[0] : '';
            let idM = href.match(idRegex);
            if (!idM) return;

            let card = a.closest('div[role="article"], li, div') || a;
            let text = card.innerText ? card.innerText.replace(/\n+/g, ' | ').trim() : '';
            let img = card.querySelector('img:not([src*="flags"])');
            let src = img ? (img.src || img.getAttribute('data-src') || '') : '';

            extracted.push({
                match_id: idM[1],
                raw_text: text,
                src: src,
                url: href,
                status: (text.toUpperCase().includes('LIVE') || defStatus === 'LIVE') ? 'LIVE' : 'UPCOMING'
            });
        });

        return extracted;
    }""", default_status)

async def scrape_fancode_live_and_schedule():
    live_url = "https://www.fancode.com/live-now/all-sports"
    schedule_url = "https://www.fancode.com/cricket/schedule"
    output_json_file = "fancode_matches.json"

    ist_zone = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_zone)
    cutoff_time = now_ist + timedelta(days=5)

    final_payload = {
        "name": "Fancode All-Sports Live & 5-Days Schedule",
        "telegram": "https://t.me/+rQTz5VL8CRpjNTZl",
        "last update time": now_ist.strftime("%I:%M:%S %p %d-%m-%Y"),
        "matches": []
    }

    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata"
        )
        page = await context.new_page()
        await page.route("**/*.{woff,woff2,css}", lambda route: route.abort())

        try:
            live_raw = await extract_from_page(page, live_url, "LIVE")
            upcoming_raw = await extract_from_page(page, schedule_url, "UPCOMING")

            combined_raw = live_raw + upcoming_raw
            seen_ids = {}
            live_list = []
            upcoming_list = []

            for item in combined_raw:
                mid_str = str(item.get("match_id", "")).strip()
                if not mid_str.isdigit():
                    continue
                mid_int = int(mid_str)

                raw_href = item.get("url", "").strip()
                if raw_href and not raw_href.startswith("http"):
                    raw_href = f"https://www.fancode.com{raw_href}"

                if mid_int in seen_ids:
                    existing = seen_ids[mid_int]
                    if raw_href and "/matches/" in raw_href and not existing["match_url"]:
                        base_u = re.sub(r'/(live-match-info|scorecard|commentary|squad)/?$', '', raw_href.rstrip('/'))
                        existing["match_url"] = f"{base_u}/live-match-info"
                        existing["scorecard_url"] = f"{base_u}/scorecard"
                        existing["event_category"] = detect_sport_from_url(base_u)
                    if item.get("src") and "cms-media" in item.get("src") and "Cricket_FC-Mini" in existing["src"]:
                        existing["src"] = item["src"].split("?")[0]
                    continue

                status_raw = str(item.get("status", "")).upper()
                if "COMPLETED" in status_raw or "RESULT" in status_raw:
                    continue

                is_live = "LIVE" in status_raw or "PROGRESS" in status_raw

                t1 = item.get("team_1", "").strip()
                t2 = item.get("team_2", "").strip()
                raw_text = item.get("raw_text") or item.get("name") or ""

                if not t1 or not t2:
                    clean_line = re.sub(r'(Upcoming|Completed|Highlights|Watch Live|Live Now|Match \d+)', '', raw_text, flags=re.I)
                    clean_line = re.sub(r'[-–—|]', ' ', clean_line).strip()
                    parts = re.split(r'\s+vs\s+|\s+VS\s+|\s+v\s+', clean_line, flags=re.I)
                    t1 = parts[0].strip() if len(parts) > 0 else clean_line
                    t2 = parts[1].strip() if len(parts) > 1 else ""

                if not t1 or len(t1) < 2:
                    continue

                match_name = f"{t1} vs {t2}" if t2 else t1
                category = detect_sport_from_url(raw_href) if raw_href else "Cricket"
                event_name = item.get("event_name") or f"{category} Series, 2026"
                raw_start = item.get("startTime", "")
                formatted_start = now_ist.strftime("%I:%M:%S %p %d-%m-%Y")

                if raw_start:
                    try:
                        dt = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00")).astimezone(ist_zone)
                        if not is_live and dt < (now_ist - timedelta(hours=6)):
                            continue
                        if dt > cutoff_time:
                            continue
                        formatted_start = dt.strftime("%I:%M:%S %p %d-%m-%Y")
                    except Exception:
                        formatted_start = str(raw_start)

                poster = item.get("src", "")
                if poster and "?" in poster:
                    poster = poster.split("?")[0]
                if not poster or "flags" in poster:
                    poster = f"https://www.fancode.com/skillup-uploads/cms-media/{mid_int}_fc-Web.jpg"

                match_page_url = ""
                scorecard_page_url = ""
                if raw_href and "/matches/" in raw_href:
                    base_u = re.sub(r'/(live-match-info|scorecard|commentary|squad)/?$', '', raw_href.rstrip('/'))
                    match_page_url = f"{base_u}/live-match-info"
                    scorecard_page_url = f"{base_u}/scorecard"

                match_obj = {
                    "event_category": category,
                    "match_id": mid_int,
                    "title": f"{match_name} ({event_name})",
                    "match_name": match_name,
                    "event_name": event_name,
                    "src": poster,
                    "team_1": t1,
                    "team_2": t2,
                    "status": "LIVE" if is_live else "UPCOMING",
                    "startTime": formatted_start,
                    "match_url": match_page_url,
                    "scorecard_url": scorecard_page_url
                }

                seen_ids[mid_int] = match_obj
                if is_live:
                    live_list.append(match_obj)
                else:
                    upcoming_list.append(match_obj)

            final_payload["matches"] = live_list + upcoming_list

        finally:
            await browser.close()

    with open(output_json_file, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, indent=4, ensure_ascii=False)

    print(f"🟢 Done! Saved {len(live_list)} LIVE (All-Sports) + {len(upcoming_list)} UPCOMING (5 Days) matches with links to {output_json_file}")

if __name__ == "__main__":
    asyncio.run(scrape_fancode_live_and_schedule())
                    
