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

NAV_JUNK_SLUGS = {
    "formula1", "formula-1", "lawn-tennis", "tennis", "motogp",
    "golf", "racing", "badminton", "football", "cricket", "kabaddi", "hockey"
}

def slugify(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', str(text).lower()).strip('-')

def extract_real_match_id(url: str) -> int:
    m = re.search(r'/matches/[^/]+?-(\d+)(?:/|$)', str(url))
    if m:
        return int(m.group(1))
    m2 = re.search(r'-(\d{5,8})(?:/|$)', str(url))
    return int(m2.group(1)) if m2 else 0

def parse_any_timestamp_to_ist(raw_val, ist_zone, default_year=2026):
    if not raw_val:
        return None

    # 1. Epoch ms / sec
    if isinstance(raw_val, (int, float)) or (isinstance(raw_val, str) and raw_val.strip().isdigit()):
        num = float(raw_val)
        if num > 1e11:
            num = num / 1000.0
        if num > 1e9:
            return datetime.fromtimestamp(num, tz=timezone.utc).astimezone(ist_zone)

    s = str(raw_val).strip()

    # 2. ISO 8601 UTC string
    if "T" in s and ("Z" in s or "+" in s or "-" in s[10:]):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ist_zone)
        except Exception:
            pass

    date_part = s.split("@")[0].strip()

    # 3. With Year: "16 Nov, 2026, 2:30 PM"
    m_with_year = re.search(r'(\d{1,2}\s+[A-Za-z]{3}),?\s+(\d{4}),?\s+(\d{1,2}:\d{2}\s*[AP]M)', date_part, re.I)
    if m_with_year:
        clean_dt_str = f"{m_with_year.group(1)} {m_with_year.group(2)} {m_with_year.group(3).upper()}"
        try:
            dt_naive = datetime.strptime(clean_dt_str, "%d %b %Y %I:%M %p")
            return dt_naive.replace(tzinfo=ist_zone)
        except Exception:
            pass

    # 4. Without Year (F1 / MotoGP banner format): "20 Nov, 9:30 AM"
    m_no_year = re.search(r'(\d{1,2}\s+[A-Za-z]{3}),?\s+(\d{1,2}:\d{2}\s*[AP]M)', date_part, re.I)
    if m_no_year:
        clean_dt_str = f"{m_no_year.group(1)} {default_year} {m_no_year.group(2).upper()}"
        try:
            dt_naive = datetime.strptime(clean_dt_str, "%d %b %Y %I:%M %p")
            return dt_naive.replace(tzinfo=ist_zone)
        except Exception:
            pass

    return None

async def collect_schedule_links(page, sport_name: str, schedule_url: str):
    print(f"🔍 Scanning [{sport_name}] Schedule: {schedule_url}")
    try:
        await page.goto(schedule_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2.5)
    except Exception as e:
        print(f"⚠️ Load warning for {schedule_url}: {e}")
        return []

    for _ in range(7):
        await page.evaluate("window.scrollBy(0, 1000);")
        await asyncio.sleep(0.6)

    raw_items = await page.evaluate(r"""(args) => {
        let results = [];
        let seen = new Set();
        let sportSlug = args.sportSlug;
        let sportName = args.sportName;

        document.querySelectorAll('a[href*="/tour/"][href*="/matches/"]').forEach(a => {
            let href = a.href ? a.href.split('?')[0] : '';
            if (!href) return;

            let card = a.closest('div[role="article"], li, div') || a;
            let text = (card.innerText || '').toUpperCase();
            if (text.includes('COMPLETED') || text.includes('BEAT ') || text.includes('WON BY') || text.includes('HIGHLIGHTS')) return;

            let isLive = text.includes('WATCH LIVE') && !text.includes('WATCH LIVE IN') && !text.includes('WATCH LIVE AT');

            let baseUrl = href.replace(/\/(live-match-info|scorecard|commentary|squad|stats)\/?$/, '');
            let fullUrl = baseUrl + '/live-match-info';
            if (!seen.has(fullUrl)) {
                seen.add(fullUrl);
                results.push({
                    sport_name: sportName,
                    match_url: fullUrl,
                    base_url: baseUrl,
                    is_live_hint: isLive,
                    raw_start: ''
                });
            }
        });

        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                
                let mid = node.matchId || node.id;
                let tourObj = node.tour || node.series || {};
                let tourId = node.tourId || tourObj.id || '';
                let tourName = node.tourName || tourObj.name || node.seriesName || '';
                let status = String(node.status || '').toUpperCase();

                let hasMatchProps = Boolean(tourId || tourObj.id || (Array.isArray(node.teams) && node.teams.length >= 2) || node.startTime);

                if (mid && String(mid).match(/^\d{5,8}$/) && hasMatchProps && (node.teams || node.slug || node.name)) {
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
                                raw_start: node.startTime || node.startTimestamp || ''
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

    clean_items = []
    for item in raw_items:
        slug_part = item["base_url"].split("/matches/")[-1]
        slug_base = re.sub(r'-\d+$', '', slug_part).lower()
        if slug_base in NAV_JUNK_SLUGS:
            continue
        clean_items.append(item)

    print(f"🟢 [{sport_name}] Collected {len(clean_items)} valid event URLs")
    return clean_items

async def scrape_event_detail(page, link_info: dict, ist_zone, now_ist, cutoff_time):
    sport_name = link_info["sport_name"]
    match_url = link_info["match_url"]
    base_url = link_info["base_url"]

    print(f"➡️ Scraping [{sport_name}]: {match_url}")
    await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(1.5)

    await page.evaluate("window.scrollBy(0, 600);")
    await asyncio.sleep(1.0)

    final_page_url = page.url.split('?')[0]
    if "/matches/" in final_page_url:
        base_url = re.sub(r'/(live-match-info|scorecard|commentary|squad|stats)/?$', '', final_page_url.rstrip('/'))

    mid_int = extract_real_match_id(base_url)

    detail = await page.evaluate(r"""(midStr) => {
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
            event_starts_on: '',
            state_start: '',
            state_status: '',
            countdown_sec: 0,
            is_live: false,
            is_completed: false,
            scores: []
        };

        // 1. Official Banner Poster
        let imgs = Array.from(document.querySelectorAll('img'));
        let fallbackBanner = '';
        for (let img of imgs) {
            let s = (img.src || img.getAttribute('data-src') || '').split('?')[0];
            if (s.includes('cms-media') && !s.includes('flags') && !s.includes('20x20') && !s.includes('Mini')) {
                if (midStr && s.includes(midStr)) {
                    info.poster = s;
                    break;
                }
                if (!fallbackBanner && (s.includes('_fc-Web') || s.includes('_fc-web') || s.includes('WebTVBG') || s.includes('WEBTVBG') || s.includes('Round-') || img.width > 250)) {
                    fallbackBanner = s;
                }
            }
        }
        if (!info.poster && fallbackBanner) {
            info.poster = fallbackBanner;
        }

        // 2. Visible Page Text Parsing
        let bodyText = document.body.innerText || '';
        let upperBody = bodyText.toUpperCase();
        if (upperBody.includes('MATCH COMPLETED') || upperBody.includes('WON BY') || upperBody.includes('SESSION ENDED')) {
            info.is_completed = true;
        }

        // Countdown timer check e.g. "02h : 28m : 53s"
        let cdMatch = bodyText.match(/(\d{1,2})h\s*:\s*(\d{1,2})m\s*:\s*(\d{1,2})s/i);
        if (cdMatch) {
            info.countdown_sec = (parseInt(cdMatch[1]) * 3600) + (parseInt(cdMatch[2]) * 60) + parseInt(cdMatch[3]);
            info.is_live = false;
        }

        let lines = bodyText.split('\n').map(l => l.trim()).filter(Boolean);
        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];

            // F1 / MotoGP Banner: "EVENT STARTS ON" -> next line "20 Nov, 9:30 AM"
            if (line.toUpperCase() === 'EVENT STARTS ON' && lines[i + 1]) {
                info.event_starts_on = lines[i + 1];
                info.is_live = false;
            }
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
                let combinedVenue = lines[i + 1];
                if (lines[i + 2] && !['OFFICIALS', 'TOSS', 'SQUAD', 'MORE LIVE ACTION'].includes(lines[i + 2].toUpperCase())) {
                    combinedVenue += ' ' + lines[i + 2];
                }
                info.time_venue_raw = combinedVenue;
                if (combinedVenue.includes('@')) {
                    info.venue = combinedVenue.split('@')[1].trim();
                }
            }
            // Circuit / Track venue for F1 / MotoGP (e.g., "Las Vegas Strip Circuit")
            if (!info.venue && (line.includes('Circuit') || line.includes('International Speedway') || line.includes('Autodromo') || line.includes('Track'))) {
                info.venue = line;
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

        // Check Watch Live only if neither countdown nor "EVENT STARTS ON" is present
        if (info.countdown_sec === 0 && !info.event_starts_on && (upperBody.includes('WATCH LIVE') || upperBody.includes('LIVE COMMENTARY'))) {
            info.is_live = true;
        }

        // 3. Next.js State Backup
        try {
            let scan = (node) => {
                if (!node || typeof node !== 'object') return;
                let nodeMid = String(node.matchId || node.id || '');
                if (!midStr || nodeMid === midStr) {
                    if (Array.isArray(node.teams) && node.teams.length >= 2) {
                        if (typeof node.teams[0].name === 'string') info.team_1 = node.teams[0].name;
                        if (typeof node.teams[1].name === 'string') info.team_2 = node.teams[1].name;
                    }
                    if (typeof node.name === 'string' && node.name && nodeMid) {
                        info.session_name = node.name;
                    }
                    if (typeof node.tourName === 'string' && node.tourName) info.event_name = node.tourName;
                    if (node.startTime) info.state_start = node.startTime;
                    if (typeof node.status === 'string') info.state_status = node.status;
                }
                if (typeof node.src === 'string' && node.src.includes('cms-media') && midStr && node.src.includes(midStr)) {
                    info.poster = node.src.split('?')[0];
                }
                for (let k in node) scan(node[k]);
            };
            if (window.__NEXT_DATA__) scan(window.__NEXT_DATA__);
        } catch(e) {}

        return info;
    }""", str(mid_int))

    if detail.get("is_completed"):
        return None

    event_name = str(detail.get("event_name") or "").strip()
    if not event_name and "/tour/" in base_url:
        tour_slug = base_url.split("/tour/")[1].split("/")[0]
        event_name = re.sub(r'-\d+$', '', tour_slug).replace("-", " ").title()
    if not event_name:
        event_name = f"{sport_name} Series, 2026"

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
            session_title = str(detail.get("session_name") or slug_clean).strip()
            t1 = session_title
            t2 = event_name

    # Exact IST Time & Status Resolution
    state_st = str(detail.get("state_status") or "").upper()
    has_countdown = int(detail.get("countdown_sec", 0)) > 0
    has_future_banner = bool(detail.get("event_starts_on"))

    is_live = bool(
        detail.get("is_live")
        or link_info.get("is_live_hint")
        or state_st in {"LIVE", "IN_PROGRESS", "STARTED"}
    )
    if has_countdown or has_future_banner:
        is_live = False

    parsed_dt = None
    for candidate in [
        detail.get("state_start"),
        link_info.get("raw_start"),
        detail.get("time_venue_raw"),
        detail.get("event_starts_on"),
    ]:
        parsed_dt = parse_any_timestamp_to_ist(candidate, ist_zone, now_ist.year)
        if parsed_dt:
            break

    if not parsed_dt and has_countdown:
        parsed_dt = now_ist + timedelta(seconds=int(detail["countdown_sec"]))
        rem = parsed_dt.minute % 5
        if rem >= 3:
            parsed_dt = (parsed_dt + timedelta(minutes=(5 - rem))).replace(second=0)
        elif rem <= 2:
            parsed_dt = (parsed_dt - timedelta(minutes=rem)).replace(second=0)

    if parsed_dt:
        if not has_countdown and not has_future_banner and parsed_dt <= now_ist:
            is_live = True
        elif parsed_dt > now_ist:
            is_live = False

        if not is_live and parsed_dt < (now_ist - timedelta(hours=12)):
            return None
        if parsed_dt > cutoff_time:
            return None

        formatted_start = parsed_dt.strftime("%I:%M:%S %p %d-%m-%Y")
        if not detail.get("time_venue_raw"):
            v_suffix = f" @ {detail['venue']}" if detail.get("venue") else ""
            detail["time_venue_raw"] = parsed_dt.strftime("%d %b, %Y, %I:%M %p") + v_suffix
    else:
        formatted_start = now_ist.strftime("%I:%M:%S %p %d-%m-%Y")

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
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": 28.6139, "longitude": 77.2090},
            permissions=["geolocation"]
        )
        await context.add_init_script("""
            const origResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
            Intl.DateTimeFormat.prototype.resolvedOptions = function() {
                const res = origResolvedOptions.apply(this, arguments);
                res.timeZone = 'Asia/Kolkata';
                return res;
            };
        """)

        page = await context.new_page()
        await page.route("**/*.{woff,woff2,css}", lambda route: route.abort())

        try:
            all_collected = []
            for sport_name, sched_url in SPORT_SCHEDULES:
                sport_items = await collect_schedule_links(page, sport_name, sched_url)
                all_collected.extend(sport_items)

            unique_by_id = {}
            for item in all_collected:
                real_mid = extract_real_match_id(item["base_url"])
                if not real_mid:
                    continue
                if real_mid not in unique_by_id:
                    unique_by_id[real_mid] = item
                elif "/tour/" in item["base_url"] and "/tour/" not in unique_by_id[real_mid]["base_url"]:
                    unique_by_id[real_mid] = item

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
