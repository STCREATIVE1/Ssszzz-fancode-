import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

def detect_sport_from_url(url: str) -> str:
    u = str(url).lower()
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

async def collect_match_links(page, listing_url: str):
    print(f"🔍 Step 1: Collecting match links from {listing_url}")
    await page.goto(listing_url, wait_until="domcontentloaded", timeout=35000)
    await asyncio.sleep(3)

    # Scroll karke saare upcoming & live matches load karna
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 1000);")
        await asyncio.sleep(0.8)

    links_data = await page.evaluate(r"""() => {
        let found = [];
        let seen = new Set();
        let idRegex = new RegExp('[-/](\\d{6,8})(?:/|$)');

        document.querySelectorAll('a[href*="/matches/"]').forEach(a => {
            let href = a.href ? a.href.split('?')[0] : '';
            if (!href || !idRegex.test(href)) return;

            let card = a.closest('div[role="article"], li, div') || a;
            let cardText = (card.innerText || '').toUpperCase();

            // Completed matches ko yahin skip kar do
            if (cardText.includes('COMPLETED') || cardText.includes('BEAT ') || cardText.includes('WON BY')) {
                return;
            }

            let baseUrl = href.replace(/\/(live-match-info|scorecard|commentary|squad|stats)\/?$/, '');
            let fullMatchUrl = baseUrl + '/live-match-info';

            if (!seen.has(fullMatchUrl)) {
                seen.add(fullMatchUrl);
                found.push({
                    match_url: fullMatchUrl,
                    base_url: baseUrl,
                    is_live_hint: cardText.includes('LIVE')
                });
            }
        });
        return found;
    }""")

    print(f"🟢 Found {len(links_data)} active/upcoming match links on {listing_url}")
    return links_data

async def scrape_single_match_detail(page, link_info: dict, ist_zone, now_ist, cutoff_time):
    match_url = link_info["match_url"]
    base_url = link_info["base_url"]

    id_match = re.search(r'[-/](\d{6,8})(?:/|$)', base_url)
    mid_int = int(id_match.group(1)) if id_match else 0
    category = detect_sport_from_url(base_url)

    print(f"➡️ Visiting Match Page: {match_url}")
    await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2.2)

    detail = await page.evaluate(r"""() => {
        let info = {
            team_1: '',
            team_2: '',
            event_name: '',
            poster: '',
            status_text: '',
            toss: '',
            venue: '',
            time_venue_raw: '',
            iso_start: '',
            is_live: false,
            is_completed: false,
            scores: [],
            raw_title: document.title || ''
        };

        // 1. Official Match Poster (cms-media banner)
        let imgs = Array.from(document.querySelectorAll('img'));
        for (let img of imgs) {
            let s = img.src || img.getAttribute('data-src') || '';
            if (s.includes('cms-media') && !s.includes('flags') && !s.includes('20x20')) {
                info.poster = s.split('?')[0];
                break;
            }
        }

        // 2. Visible DOM Text se exact fields nikalna
        let bodyText = document.body.innerText || '';
        let upperBody = bodyText.toUpperCase();
        if (upperBody.includes('MATCH COMPLETED') || upperBody.includes('WON BY')) {
            info.is_completed = true;
        }
        if (upperBody.includes('WATCH LIVE') || upperBody.includes('LIVE NOW')) {
            info.is_live = true;
        }

        let lines = bodyText.split('\n').map(l => l.trim()).filter(Boolean);
        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];

            if ((line === 'vs' || line === 'VS') && i > 0 && i + 1 < lines.length) {
                info.team_1 = lines[i - 1];
                info.team_2 = lines[i + 1];
            }
            if (line === 'TOURNAMENT' && lines[i + 1]) {
                info.event_name = lines[i + 1];
            }
            if (line === 'TOSS' && lines[i + 1]) {
                info.toss = lines[i + 1];
            }
            if (line === 'MATCH TIME AND VENUE' && lines[i + 1]) {
                info.time_venue_raw = lines[i + 1];
                if (lines[i + 1].includes('@')) {
                    info.venue = lines[i + 1].split('@')[1].trim();
                }
            }
            if (line.includes('trail by') || line.includes('lead by') || line.includes('need ')) {
                info.status_text = line;
                info.is_live = true;
            }
            if (/^\(\d+(\.\d+)?\)\s*\d+\/\d+/.test(line) || /^\d+\/\d+$/.test(line)) {
                info.scores.push({
                    team: lines[i - 1] || '',
                    score: line
                });
                info.is_live = true;
            }
        }

        // 3. Next.js State Backup (Type-safe string extraction)
        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                if (Array.isArray(node.teams) && node.teams.length >= 2) {
                    if (typeof node.teams[0].name === 'string') info.team_1 = node.teams[0].name;
                    if (typeof node.teams[1].name === 'string') info.team_2 = node.teams[1].name;
                }
                if (typeof node.tourName === 'string' && node.tourName) info.event_name = node.tourName;
                if (typeof node.startTime === 'string' && node.startTime) info.iso_start = node.startTime;
                if (typeof node.src === 'string' && node.src.includes('cms-media')) info.poster = node.src.split('?')[0];
                for (let k in node) scan(node[k]);
            };
            if (window.__NEXT_DATA__) scan(window.__NEXT_DATA__);
        } catch(e) {}

        return info;
    }""")

    if detail.get("is_completed"):
        return None

    # Teams fallback from URL slug agar DOM se na mile
    t1 = str(detail.get("team_1") or "").strip()
    t2 = str(detail.get("team_2") or "").strip()
    if not t1 or not t2:
        slug_part = base_url.split("/matches/")[-1]
        slug_clean = re.sub(r'-\d+$', '', slug_part).replace("-", " ").title()
        parts = re.split(r'\s+Vs\s+', slug_clean, flags=re.I)
        t1 = parts[0].strip() if len(parts) > 0 else slug_clean
        t2 = parts[1].strip() if len(parts) > 1 else ""

    # Event name fallback from URL slug
    event_name = str(detail.get("event_name") or "").strip()
    if not event_name and "/tour/" in base_url:
        tour_slug = base_url.split("/tour/")[1].split("/")[0]
        event_name = re.sub(r'-\d+$', '', tour_slug).replace("-", " ").title()
    if not event_name:
        event_name = f"{category} Series, 2026"

    # StartTime aur 5-Day filter check
    is_live = bool(detail.get("is_live") or link_info.get("is_live_hint"))
    formatted_start = now_ist.strftime("%I:%M:%S %p %d-%m-%Y")
    iso_start = str(detail.get("iso_start") or "").strip()

    if iso_start:
        try:
            dt = datetime.fromisoformat(iso_start.replace("Z", "+00:00")).astimezone(ist_zone)
            if not is_live and dt > cutoff_time:
                return None  # 5 din se aage ka match skip karo
            formatted_start = dt.strftime("%I:%M:%S %p %d-%m-%Y")
        except Exception:
            formatted_start = detail.get("time_venue_raw") or formatted_start
    elif detail.get("time_venue_raw"):
        formatted_start = str(detail["time_venue_raw"]).split("@")[0].strip()

    poster = str(detail.get("poster") or "").strip()
    if not poster:
        poster = f"https://www.fancode.com/skillup-uploads/cms-media/{mid_int}_fc-Web.jpg"

    match_name = f"{t1} vs {t2}" if t2 else t1

    return {
        "event_category": category,
        "match_id": mid_int,
        "title": f"{match_name} ({event_name})",
        "match_name": match_name,
        "event_name": event_name,
        "src": poster,
        "team_1": t1,
        "team_2": t2,
        "status": "LIVE" if is_live else "UPCOMING",
        "match_status_text": str(detail.get("status_text") or ""),
        "startTime": formatted_start,
        "live_scorecard": detail.get("scores", []),
        "match_info": {
            "toss": str(detail.get("toss") or ""),
            "venue": str(detail.get("venue") or ""),
            "time_and_venue": str(detail.get("time_venue_raw") or "")
        },
        "links": {
            "match_url": f"{base_url}/live-match-info",
            "full_scorecard": f"{base_url}/scorecard",
            "live_commentary": f"{base_url}/commentary",
            "squad": f"{base_url}/squad"
        }
    }

async def main():
    output_json_file = "fancode_matches.json"
    ist_zone = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_zone)
    cutoff_time = now_ist + timedelta(days=5)

    final_payload = {
        "name": "Fancode Live & 5-Days Schedule Data",
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
            # Step 1: Dono pages se saare match links ikattha karo
            live_links = await collect_match_links(page, "https://www.fancode.com/live-now/all-sports")
            sched_links = await collect_match_links(page, "https://www.fancode.com/cricket/schedule")

            # Duplicate links merge karo
            all_links_map = {}
            for item in (live_links + sched_links):
                u = item["match_url"]
                if u not in all_links_map:
                    all_links_map[u] = item
                elif item["is_live_hint"]:
                    all_links_map[u]["is_live_hint"] = True

            unique_links = list(all_links_map.values())
            print(f"🟢 Total unique match links to visit: {len(unique_links)}")

            live_matches = []
            upcoming_matches = []

            # Step 2: Ek-ek karke har match link ke andar ja kar exact poster & data nikalo
            for link_info in unique_links[:35]:  # Max 35 matches taaki workflow fast complete ho
                try:
                    match_data = await scrape_single_match_detail(page, link_info, ist_zone, now_ist, cutoff_time)
                    if match_data:
                        if match_data["status"] == "LIVE":
                            live_matches.append(match_data)
                        else:
                            upcoming_matches.append(match_data)
                except Exception as e:
                    print(f"⚠️ Skipped {link_info['match_url']} due to error: {str(e)}")

            final_payload["matches"] = live_matches + upcoming_matches

        finally:
            await browser.close()

    with open(output_json_file, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, indent=4, ensure_ascii=False)

    print(f"✅ Completed! Saved {len(final_payload['matches'])} matches to {output_json_file}")

if __name__ == "__main__":
    asyncio.run(main())
    
