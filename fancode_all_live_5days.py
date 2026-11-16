import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

SPORT_SCHEDULES = [
    ("Cricket", "https://www.fancode.com/cricket/schedule"),
    ("Football", "https://www.fancode.com/football/schedule"),
    ("Tennis", "https://www.fancode.com/tennis/schedule"),
    ("Formula 1", "https://www.fancode.com/formula1/schedule"),
    ("MotoGP", "https://www.fancode.com/motogp/schedule"),
    ("Golf", "https://www.fancode.com/golf/schedule"),
    ("Badminton", "https://www.fancode.com/badminton/schedule"),
]

def slugify(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', str(text).lower()).strip('-')

async def collect_schedule_links(page, sport_name: str, schedule_url: str):
    print(f"🔍 Scanning [{sport_name}] Schedule: {schedule_url}")
    try:
        await page.goto(schedule_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2.5)
    except Exception as e:
        print(f"⚠️ Load warning for {schedule_url}: {e}")
        return []

    # 5 days ke fixtures load karne ke liye scrolling
    for _ in range(7):
        await page.evaluate("window.scrollBy(0, 1000);")
        await asyncio.sleep(0.6)

    raw_items = await page.evaluate(r"""(args) => {
        let results = [];
        let seen = new Set();
        let sportSlug = args.sportSlug;
        let sportName = args.sportName;

        // 1. Direct DOM links check
        let idRegex = new RegExp('[-/](\\d{6,8})(?:/|$)');
        document.querySelectorAll('a[href*="/matches/"]').forEach(a => {
            let href = a.href ? a.href.split('?')[0] : '';
            if (!href || !idRegex.test(href)) return;

            let card = a.closest('div[role="article"], li, div') || a;
            let text = (card.innerText || '').toUpperCase();
            if (text.includes('COMPLETED') || text.includes('BEAT ') || text.includes('WON BY') || text.includes('HIGHLIGHTS')) return;

            let baseUrl = href.replace(/\/(live-match-info|scorecard|commentary|squad|stats)\/?$/, '');
            let fullUrl = baseUrl + '/live-match-info';
            if (!seen.has(fullUrl)) {
                seen.add(fullUrl);
                results.push({
                    sport_name: sportName,
                    match_url: fullUrl,
                    base_url: baseUrl,
                    is_live_hint: text.includes('LIVE')
                });
            }
        });

        // 2. Embedded __NEXT_DATA__ & Apollo State Scan
        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                
                let mid = node.matchId || node.id;
                let tourObj = node.tour || node.series || {};
                let tourId = node.tourId || tourObj.id || '';
                let tourName = node.tourName || tourObj.name || node.seriesName || '';
                let status = String(node.status || '').toUpperCase();

                if (mid && String(mid).match(/^\d{6,8}$/) && (node.teams || node.slug || node.name)) {
                    if (status === 'COMPLETED' || status === 'ABANDONED' || status === 'RESULT') return;

                    let mSlug = typeof node.slug === 'string' ? node.slug : '';
                    if (!mSlug && Array.isArray(node.teams) && node.teams.length >= 2) {
                        let t1 = (node.teams[0].name || 'team1').toLowerCase().replace(/[^a-z0-9]+/g, '-');
                        let t2 = (node.teams[1].name || 'team2').toLowerCase().replace(/[^a-z0-9]+/g, '-');
                        mSlug = `${t1}-vs-${t2}-${mid}`;
                    } else if (!mSlug && typeof node.name === 'string') {
                        mSlug = node.name.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '-' + mid;
                    } else if (mSlug && !mSlug.endsWith(String(mid))) {
                        mSlug = `${mSlug}-${mid}`;
                    }

                    let tSlug = typeof tourObj.slug === 'string' ? tourObj.slug : '';
                    if (!tSlug && tourName && tourId) {
                        tSlug = tourName.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '-' + tourId;
                    }

                    if (mSlug) {
                        let baseUrl = (tSlug && tSlug.match(/-\d+$/))
                            ? `https://www.fancode.com/${sportSlug}/tour/${tSlug}/matches/${mSlug}`
                            : `https://www.fancode.com/${sportSlug}/matches/${mSlug}`;
                        let fullUrl = baseUrl + '/live-match-info';

                        if (!seen.has(fullUrl)) {
                            seen.add(fullUrl);
                            results.push({
                                sport_name: sportName,
                                match_url: fullUrl,
                                base_url: baseUrl,
                                is_live_hint: status === 'LIVE' || status === 'IN_PROGRESS',
                                iso_start: typeof node.startTime === 'string' ? node.startTime : ''
                            });
                        }
                    }
                }
                for (let k in node) scan(node[k]);
            };
            if (window.__NEXT_DATA__) scan(window.__NEXT_DATA__);
            if (window.__INIT_STATE__) scan(window.__INIT_STATE__);
        } catch(e) {}

        return results;
    }""", {"sportSlug": slugify(sport_name), "sportName": sport_name})

    print(f"🟢 [{sport_name}] Collected {len(raw_items)} scheduled URLs")
    return raw_items

async def scrape_event_detail(page, link_info: dict, ist_zone, now_ist, cutoff_time):
    sport_name = link_info["sport_name"]
    match_url = link_info["match_url"]
    base_url = link_info["base_url"]

    id_match = re.search(r'[-/](\d{6,8})(?:/|$)', base_url)
    mid_int = int(id_match.group(1)) if id_match else 0

    print(f"➡️ Scraping [{sport_name}]: {match_url}")
    await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2.0)

    final_page_url = page.url.split('?')[0]
    if "/matches/" in final_page_url:
        base_url = re.sub(r'/(live-match-info|scorecard|commentary|squad|stats)/?$', '', final_page_url.rstrip('/'))

    detail = await page.evaluate(r"""() => {
        let info = {
            team_1: '',
            team_2: '',
            session_name: '',
            event_name: '',
            poster: '',
            status_text: '',
            toss: '',
            venue: '',
            time_venue_raw: '',
            iso_start: '',
            is_live: false,
            is_completed: false,
            scores: []
        };

        // 1. Official 16:9 Banner Poster
        let imgs = Array.from(document.querySelectorAll('img'));
        for (let img of imgs) {
            let s = img.src || img.getAttribute('data-src') || '';
            if (s.includes('cms-media') && !s.includes('flags') && !s.includes('20x20')) {
                info.poster = s.split('?')[0];
                break;
            }
        }

        // 2. Visible Page Text
        let bodyText = document.body.innerText || '';
        let upperBody = bodyText.toUpperCase();
        if (upperBody.includes('MATCH COMPLETED') || upperBody.includes('WON BY') || upperBody.includes('SESSION ENDED')) {
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

        // 3. Next.js State Backup
        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                if (Array.isArray(node.teams) && node.teams.length >= 2) {
                    if (typeof node.teams[0].name === 'string') info.team_1 = node.teams[0].name;
                    if (typeof node.teams[1].name === 'string') info.team_2 = node.teams[1].name;
                }
                if (typeof node.name === 'string' && node.name && (node.matchId || node.id)) {
                    info.session_name = node.name;
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

    # Event / Tournament Name
    event_name = str(detail.get("event_name") or "").strip()
    if not event_name and "/tour/" in base_url:
        tour_slug = base_url.split("/tour/")[1].split("/")[0]
        event_name = re.sub(r'-\d+$', '', tour_slug).replace("-", " ").title()
    if not event_name:
        event_name = f"{sport_name} Series, 2026"

    # Teams ya Non-Team Session (Formula 1 / MotoGP / Golf) handle karna
    t1 = str(detail.get("team_1") or "").strip()
    t2 = str(detail.get("team_2") or "").strip()

    if not t1 or not t2:
        slug_part = base_url.split("/matches/")[-1]
        slug_clean = re.sub(r'-\d+$', '', slug_part).replace("-", " ").title()
        if re.search(r'\s+vs\s+', slug_clean, flags=re.I):
            parts = re.split(r'\s+Vs\s+', slug_clean, flags=re.I)
            t1 = parts[0].strip()
            t2 = parts[1].strip() if len(parts) > 1 else ""
        else:
            # Formula 1, MotoGP, Golf sessions ke liye
            session_title = str(detail.get("session_name") or slug_clean).strip()
            t1 = session_title
            t2 = event_name

    # StartTime aur 5-Day filter check
    is_live = bool(detail.get("is_live") or link_info.get("is_live_hint"))
    formatted_start = now_ist.strftime("%I:%M:%S %p %d-%m-%Y")
    iso_start = str(detail.get("iso_start") or link_info.get("iso_start") or "").strip()

    if iso_start:
        try:
            dt = datetime.fromisoformat(iso_start.replace("Z", "+00:00")).astimezone(ist_zone)
            if not is_live and dt < (now_ist - timedelta(hours=6)):
                return None
            if dt > cutoff_time:
                return None  # 5 din se aage ka event skip karo
            formatted_start = dt.strftime("%I:%M:%S %p %d-%m-%Y")
        except Exception:
            formatted_start = detail.get("time_venue_raw") or formatted_start
    elif detail.get("time_venue_raw"):
        formatted_start = str(detail["time_venue_raw"]).split("@")[0].strip()

    poster = str(detail.get("poster") or "").strip()
    if not poster:
        poster = f"https://www.fancode.com/skillup-uploads/cms-media/{mid_int}_fc-Web.jpg"

    match_name = f"{t1} vs {t2}" if (t2 and t2 != event_name) else t1

    links_obj = {
        "match_url": f"{base_url}/live-match-info"
    }
    if sport_name == "Cricket":
        links_obj["full_scorecard"] = f"{base_url}/scorecard"
        links_obj["live_commentary"] = f"{base_url}/commentary"
        links_obj["squad"] = f"{base_url}/squad"

    return {
        "event_category": sport_name,
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
        "links": links_obj
    }

async def main():
    output_json_file = "fancode_matches.json"
    ist_zone = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_zone)
    cutoff_time = now_ist + timedelta(days=5)

    final_payload = {
        "name": "Fancode 7-Sports 5-Days Schedule Data",
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
            all_collected = []
            for sport_name, sched_url in SPORT_SCHEDULES:
                sport_items = await collect_schedule_links(page, sport_name, sched_url)
                all_collected.extend(sport_items)

            # Deduplicate by match_id
            unique_by_id = {}
            for item in all_collected:
                id_m = re.search(r'[-/](\d{6,8})(?:/|$)', item["base_url"])
                if not id_m:
                    continue
                mid = id_m.group(1)
                if mid not in unique_by_id:
                    unique_by_id[mid] = item

            unique_links = list(unique_by_id.values())
            print(f"🟢 Total unique scheduled events across 7 sports: {len(unique_links)}")

            live_matches = []
            upcoming_matches = []

            for link_info in unique_links[:60]:
                try:
                    match_data = await scrape_event_detail(page, link_info, ist_zone, now_ist, cutoff_time)
                    if match_data:
                        if match_data["status"] == "LIVE":
                            live_matches.append(match_data)
                        else:
                            upcoming_matches.append(match_data)
                except Exception as e:
                    print(f"⚠️ Skipped {link_info['match_url']}: {str(e)}")

            final_payload["matches"] = live_matches + upcoming_matches

        finally:
            await browser.close()

    with open(output_json_file, "w", encoding="utf-8") as f:
        json.dump(final_payload, f, indent=4, ensure_ascii=False)

    print(f"✅ Done! Saved {len(live_matches)} LIVE + {len(upcoming_matches)} UPCOMING (5 Days) events to {output_json_file}")

if __name__ == "__main__":
    asyncio.run(main())
    
