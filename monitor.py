"""Alta Via 1 rifugio booking monitor.

Runs on GitHub Actions. Checks each hut for the trip night and sends a
Telegram alert ONLY when something actionable changes:
  - dorm beds for 2 appear on your night (engine huts)
  - rooms appear on your night (Scotoni / Bukly - verify dorm)
  - a hut page starts saying 2027 bookings are open (page-watch huts)
  - a check has been broken for several runs in a row (so it never dies silently)

Never books, submits a booking, or pays. Read-only.
Usage: python monitor.py [--test-alert]
"""
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent
CFG = json.loads((ROOT / "huts.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state" / "state.json"
HISTORY_PATH = ROOT / "state" / "history.jsonl"
STATUS_PATH = ROOT / "STATUS.md"

GUESTS = CFG["trip"]["guests"]
ALERTS = CFG["alerts"]
ROME, KL = ZoneInfo("Europe/Rome"), ZoneInfo("Asia/Kuala_Lumpur")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9,it;q=0.8",
}
TIMEOUT = 25
PAUSE = 3  # seconds between requests - be polite to small hut servers

# Status values
AVAILABLE = "AVAILABLE"            # dorm beds for your group on your night
ROOMS = "ROOMS - VERIFY DORM"      # Bukly shows rooms; may be private only
LIVE_FULL = "LIVE - your night full"
NOT_LIVE = "NOT LIVE YET"
PAGE_OPEN = "PAGE SAYS OPEN - verify"
WATCHING = "WATCHING PAGE"
NO_URL = "NO URL SET"
ERROR = "ERROR"
ALERT_STATUSES = {AVAILABLE, ROOMS, PAGE_OPEN}

NO_ROOM_RE = re.compile(
    r"do not have any room available|no rooms? available|"
    r"non (abbiamo|ci sono) (nessuna|alcuna|camere|posti)|nessuna disponibilit", re.I)
BEDS_SPLIT_RE = re.compile(r"Number of beds|Numero (?:di )?(?:letti|posti)", re.I)
DORM_RE = re.compile(r"dormitor|bunk|camerat|letti a castello|cuccett|lager", re.I)
NUMS_RE = re.compile(r"^\s*:?\s*((?:\d+\s+){0,80}\d+)")


# ---------------------------------------------------------------- helpers
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def d(s):
    return dt.date.fromisoformat(s)


def ddmmyyyy(day):
    return day.strftime("%d-%m-%Y")


def norm_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def fmt_night(s):
    return d(s).strftime("%a %d %b %Y")


# ---------------------------------------------------------------- engine huts
BOTCHECK_RE = re.compile(r"sgcaptcha|One moment, please|captcha|cf-chl|Just a moment", re.I)


def parse_engine(html):
    """Return {'kind': 'none'|'rooms'|'blocked'|'unrecognized', 'dorm': n, 'private': n}."""
    if BOTCHECK_RE.search(html[:4000]):
        return {"kind": "blocked", "dorm": 0, "private": 0}
    text = norm_text(html)
    if NO_ROOM_RE.search(text):
        return {"kind": "none", "dorm": 0, "private": 0}
    parts = BEDS_SPLIT_RE.split(text)
    if len(parts) < 2:
        return {"kind": "unrecognized", "dorm": 0, "private": 0}
    dorm = private = 0
    for i in range(len(parts) - 1):
        label = parts[i]
        if i > 0:  # drop the previous room's bed-count numbers
            label = NUMS_RE.sub("", label, count=1)
        label = label[-400:]
        m = NUMS_RE.match(parts[i + 1])
        if not m:
            continue
        mx = max(int(x) for x in m.group(1).split())
        if DORM_RE.search(label):
            dorm = max(dorm, mx)
        else:
            private = max(private, mx)
    return {"kind": "rooms", "dorm": dorm, "private": private}


DEBUG_DIR = ROOT / "state" / "debug"


def save_debug(name, html):
    """Keep a copy of a page the monitor can't read, so the parser can be fixed."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{name}.html").write_text(html, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def post_engine(session, url, night, debug_name=None):
    arr = d(night) if isinstance(night, str) else night
    data = {"arrivo": ddmmyyyy(arr), "partenza": ddmmyyyy(arr + dt.timedelta(days=1)),
            "persone": str(GUESTS)}
    r = session.post(url, data=data, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    time.sleep(PAUSE)
    res = parse_engine(r.text)
    if res["kind"] == "unrecognized" and debug_name:
        save_debug(debug_name, r.text)
    return res


def check_engine(hut, prev):
    """Light-touch check: 1 request for your night, plus at most 1 control night
    (rotating), and none once the portal is known to be live."""
    s = requests.Session()
    endpoints = list(hut["endpoints"])
    if prev.get("endpoint") in endpoints:  # try last known-good first
        endpoints.remove(prev["endpoint"])
        endpoints.insert(0, prev["endpoint"])
    last_err = None
    for url in endpoints:
        dbg = f"{hut['id']}-post-{url.split('//')[-1].split('/')[0]}"
        try:
            res = post_engine(s, url, hut["night"], debug_name=dbg)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"[:200]
            continue
        if res["kind"] == "blocked":
            return {"status": ERROR, "detail": "site showed a bot check (\"One moment, please\") - will retry",
                    "link": url, "blocked": True}
        if res["kind"] == "unrecognized":
            last_err = "page not recognised (layout changed?)"
            continue
        out = {"endpoint": url, "link": url}
        if res["dorm"] >= GUESTS:
            return {**out, "status": AVAILABLE,
                    "detail": f"{res['dorm']} dorm beds shown for your night"}
        note = ""
        if res["private"]:
            note = " (private rooms only)"
        elif res["kind"] == "rooms" and res["dorm"]:
            note = f" (only {res['dorm']} dorm bed)"
        if prev.get("status") == LIVE_FULL:  # a live portal doesn't go back - skip controls
            return {**out, "status": LIVE_FULL, "detail": "portal live, your night full" + note}
        controls = CFG["control_nights"]
        c = controls[now_utc().hour % len(controls)]
        try:
            cr = post_engine(s, url, c)
        except Exception:  # noqa: BLE001
            cr = {"kind": "error"}
        if cr["kind"] == "rooms" and (cr["dorm"] or cr["private"]):
            return {**out, "status": LIVE_FULL, "detail": f"portal live (control {c} has space){note}"}
        if prev.get("status") == NOT_LIVE or cr["kind"] == "none":
            return {**out, "status": NOT_LIVE, "detail": f"no space on your night or control {c}{note}"}
        return {**out, "status": prev.get("status") or NOT_LIVE, "detail": f"control {c} unreadable{note}"}
    return {"status": ERROR, "detail": last_err or "all endpoints failed", "link": hut["endpoints"][0]}


# ---------------------------------------------------------------- Bukly (Scotoni)
MONTHS_IT = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
             "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12}


def bukly_get(hut, night):
    """Returns (url, on_day, any_open, rooms_text). on_day None = page not recognised.
    Ported from the old monitor: either a room list (night bookable) or a ~16-day
    grid of div.s-open / div.s-closed cells, read by the column for the trip night."""
    a = d(night)
    url = f"{hut['base']}/{a.isoformat()}/{(a + dt.timedelta(days=1)).isoformat()}/"
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    time.sleep(PAUSE)
    soup = BeautifulSoup(r.text, "html.parser")
    table = next((t for t in soup.find_all("table") if t.find("span", class_="day")), None)
    if table is None:
        rooms = soup.select(".hotel-room__info")
        if rooms:
            return url, len(rooms), True, " | ".join(
                re.sub(r"\s+", " ", x.get_text(" ")).strip()[:60] for x in rooms[:4])
        return url, None, None, ""
    heads = []
    for th in table.find_all("th"):
        day, mon = th.find("span", class_="day"), th.find("span", class_="month")
        if day and mon:
            heads.append((MONTHS_IT.get(mon.get_text(strip=True)[:3].lower()), int(day.get_text(strip=True))))
    col = heads.index((a.month, a.day)) if (a.month, a.day) in heads else None
    any_open, on_day = False, 0
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")[1:]
        if any(c.find("div", class_="s-open") for c in cells):
            any_open = True
        if col is not None and col < len(cells) and cells[col].find("div", class_="s-open"):
            on_day += 1
    return url, on_day, any_open, ""


def check_bukly(hut, prev):
    bases = list(hut.get("bases") or [hut["base"]])
    if prev.get("base") in bases:  # try last known-good address first
        bases.remove(prev["base"])
        bases.insert(0, prev["base"])
    last = "no address worked"
    for base in bases:
        h = {**hut, "base": base}
        try:
            url, on_day, any_open, names = bukly_get(h, hut["night"])
        except Exception as e:  # noqa: BLE001
            last = f"{base.split('//')[-1].split('/')[0]}: {type(e).__name__}"
            continue
        if on_day is None:
            last = f"{base.split('//')[-1].split('/')[0]}: calendar page not recognised"
            continue
        out = {"link": url, "base": base}
        if on_day:
            dorm = "dorm-type room listed" if DORM_RE.search(names) else "check if any are dorm"
            return {**out, "status": ROOMS, "detail": f"{on_day} room type(s) open: {dorm}. {names}".strip()}
        if any_open:
            return {**out, "status": LIVE_FULL, "detail": "2027 calendar live, your night closed"}
        for c in ["2027-07-15", "2027-08-15", "2027-09-14"]:
            try:
                _, _, ao, _ = bukly_get(h, c)
            except Exception:  # noqa: BLE001
                continue
            if ao:
                return {**out, "status": LIVE_FULL, "detail": f"calendar live (control {c})"}
        return {**out, "status": NOT_LIVE, "detail": "2027 calendar blank"}
    return {"status": ERROR, "detail": last[:200], "link": bases[0]}


# ---------------------------------------------------------------- page watch
KEY_RE = re.compile(r"2027|prenotazion|booking|reservation|buchung|apertur|opening|stagione|season", re.I)
OPEN_RE = re.compile(
    r"prenotazioni\s+(?:\S+\s+){0,6}aperte|aperte le prenotazioni|"
    r"booking[s]?\s+(?:\S+\s+){0,6}open|open for booking|now open|"
    r"buchungen\s+(?:\S+\s+){0,6}(?:offen|geöffnet)|prenotabil", re.I)
NEG_RE = re.compile(r"\bnot\b|\bnon\b|\bnicht\b|\bnoch nicht\b|\bnot yet\b|closed|chius", re.I)


NOISE_RE = re.compile(r"privacy|personal data|cookie|gdpr|credit card|legal|all rights|"
                      r"copyright|©|facebook|instagram|newsletter", re.I)


def page_sentences(html):
    text = norm_text(html)
    sents = re.split(r"(?<=[.!?])\s+|\s{2,}|\s\|\s", text)
    out = set()
    for x in sents:
        x = x.strip()
        if not KEY_RE.search(x[:300]) and (k := KEY_RE.search(x)):
            # keyword sits after a long menu - keep the text around it
            x = x[max(0, k.start() - 100):k.start() + 200]
        x = x[:300]
        if len(x) < 15 or not KEY_RE.search(x) or NOISE_RE.search(x):
            continue
        if len(re.findall(r"\d", x)) > len(x) * 0.4:  # calendar/number noise
            continue
        out.add(x)
    return sorted(out)


def fetch_page(url):
    """GET a page. If a site blocks GitHub's servers (e.g. Cloudflare), retry
    with a browser-like TLS fingerprint, then via the r.jina.ai reader proxy."""
    errs = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code < 400:
            return r.text, "direct"
        errs.append(f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(type(e).__name__)
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=TIMEOUT)
        if r.status_code < 400:
            return r.text, "browser-mode"
        errs.append(f"browser-mode HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"browser-mode {type(e).__name__}")
    try:
        r = requests.get("https://r.jina.ai/" + url, timeout=45)
        if r.status_code < 400 and len(r.text) > 200:
            return r.text, "reader-proxy"
        errs.append(f"proxy HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"proxy {type(e).__name__}")
    raise RuntimeError(", ".join(errs))


def check_page(hut, prev):
    if not hut.get("pages"):
        return {"status": NO_URL, "detail": hut.get("todo", "add a URL in huts.json"), "link": ""}
    sents, errors, vias = set(), [], set()
    for url in hut["pages"]:
        try:
            html, via = fetch_page(url)
            sents.update(page_sentences(html))
            vias.add(via)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url.split('//')[-1][:40]}: {e}")
        time.sleep(PAUSE)
    if errors and not sents:
        return {"status": ERROR, "detail": "; ".join(errors)[:200], "link": hut["pages"][0]}
    fp = hashlib.sha1("\n".join(sorted(sents)).encode()).hexdigest()[:12]
    old = set(prev.get("sentences", []))
    added = sorted(sents - old) if old else []
    removed = sorted(old - sents) if old else []
    opens = [s for s in sents if "2027" in s and OPEN_RE.search(s) and not NEG_RE.search(s)]
    out = {"link": hut["pages"][0], "sentences": sorted(sents), "fingerprint": fp, "via": sorted(vias),
           "added": added[:5], "removed": removed[:5]}
    if opens:
        return {**out, "status": PAGE_OPEN, "detail": opens[0][:200]}
    changed = bool(added or removed)
    return {**out, "status": WATCHING,
            "detail": ("page text changed: " + (added[0] if added else "text removed"))[:200]
            if changed else "no booking-text change" + ("" if vias == {"direct"} else f" (via {', '.join(sorted(vias))})"),
            "changed": changed}


MANUAL = "CHECK MANUALLY"


def check_manual(hut, prev):
    return {"status": MANUAL, "detail": hut.get("manual_note", "site can't be checked automatically"),
            "link": hut.get("book_url", "")}


CHECKS = {"engine": check_engine, "bukly": check_bukly, "page": check_page, "manual": check_manual}


# ---------------------------------------------------------------- telegram
def telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram not configured]\n" + text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
            "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        ok = r.ok and r.json().get("ok")
        if not ok:
            print("Telegram error:", r.text[:300])
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        print("Telegram exception:", e)
        return False


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def alert_text(hut, res, reminder=False):
    icon = {AVAILABLE: "🟢", ROOMS: "🟢", PAGE_OPEN: "🟡"}.get(res["status"], "⚠️")
    head = "Still available" if reminder else res["status"]
    lines = [f"{icon} <b>{esc(hut['name'])}</b> ({hut['role']}) - {esc(head)}",
             f"Night: {fmt_night(hut['night'])} · {GUESTS} guests · dorm · half board",
             esc(res.get("detail", ""))]
    if res.get("link"):
        lines.append(esc(res["link"]))
    lines.append("Book by hand - check deposit terms before paying.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def write_status(results, state, ts):
    rome, kl = ts.astimezone(ROME), ts.astimezone(KL)
    lines = ["# Alta Via 1 booking monitor", "",
             f"Last check: **{kl:%d %b %Y %H:%M} Kuala Lumpur** · {rome:%H:%M} Italy", "",
             f"Trip: {GUESTS} guests · dorm only · half board", "",
             "| Night | Hut | Role | Status | Detail |", "|---|---|---|---|---|"]
    for hut in CFG["huts"]:
        r = results.get(hut["id"]) or state.get(hut["id"], {})
        name = f"[{hut['name']}]({r['link']})" if r.get("link") else hut["name"]
        detail = str(r.get("detail", "")).replace("|", "/")[:140]
        lines.append(f"| {d(hut['night']):%d %b} | {name} | {hut['role']} | **{r.get('status', '-')}** | {detail} |")
    STATUS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def send_reminders(state, ts):
    """One-off dated Telegram reminders from reminders.json (sent once, first run after 'at')."""
    path = ROOT / "reminders.json"
    done = list(state.get("_reminders", []))
    if not path.exists():
        return done
    cfg = json.loads(path.read_text(encoding="utf-8"))
    tz = ZoneInfo(cfg.get("timezone", "Asia/Kuala_Lumpur"))
    for r in cfg.get("reminders", []):
        if r["id"] in done:
            continue
        due = dt.datetime.fromisoformat(r["at"]).replace(tzinfo=tz)
        if ts >= due and telegram(r["text"]):
            done.append(r["id"])
            print("Reminder sent:", r["id"])
    return done


def main():
    if "--test-alert" in sys.argv:
        ok = telegram("✅ Alta Via 1 monitor: test alert. Telegram is set up correctly.")
        print("test alert sent" if ok else "test alert FAILED")
        sys.exit(0 if ok else 1)

    ts = now_utc()
    if ts.astimezone(ROME).date() > d(CFG["trip"]["stop_after"]):
        print("Trip dates have passed - nothing to do. Disable the workflow in GitHub.")
        return

    state = load_state()
    reminders_done = send_reminders(state, ts)
    for hut in CFG["huts"]:
        for i, u in enumerate(hut.get("debug_urls", [])):
            try:
                save_debug(f"{hut['id']}-page{i}", requests.get(u, headers=HEADERS, timeout=TIMEOUT).text)
            except Exception as e:  # noqa: BLE001
                save_debug(f"{hut['id']}-page{i}", f"ERROR {e}")
    results, sent = {}, []
    for hut in CFG["huts"]:
        prev = state.get(hut["id"], {})
        try:
            res = CHECKS[hut["check"]](hut, prev)
        except Exception as e:  # noqa: BLE001 - never crash the whole run
            res = {"status": ERROR, "detail": f"{type(e).__name__}: {e}"[:200]}
        if hut.get("book_url"):
            res["probe_url"] = res.get("link", "")
            res["link"] = hut["book_url"]
        status, pstatus = res["status"], prev.get("status")
        res["checked"] = ts.isoformat()
        res["since"] = prev.get("since") if status == pstatus else ts.isoformat()
        res["errors"] = prev.get("errors", 0) + 1 if status == ERROR else 0
        res["alerted"] = prev.get("alerted") if status == pstatus else None
        if status == ERROR and pstatus and pstatus != ERROR:
            res["last_good"] = pstatus  # keep last real result visible
        elif status == ERROR:
            res["last_good"] = prev.get("last_good")

        send = None
        if status in ALERT_STATUSES and status != pstatus:
            send = alert_text(hut, res)
        elif status in ALERT_STATUSES and res["alerted"] and ALERTS["remind_hours"]:
            last = dt.datetime.fromisoformat(res["alerted"])
            if ts - last >= dt.timedelta(hours=ALERTS["remind_hours"]):
                send = alert_text(hut, res, reminder=True)
        elif status == LIVE_FULL and pstatus == NOT_LIVE and ALERTS["portal_live"]:
            send = f"🔵 <b>{esc(hut['name'])}</b>: 2027 portal is live, but your night is full."
        elif status == WATCHING and res.get("changed") and ALERTS["page_changes"]:
            send = f"📝 <b>{esc(hut['name'])}</b> booking text changed:\n{esc(res['detail'])}\n{esc(res['link'])}"
        elif status == ERROR and res["errors"] == ALERTS["error_after_runs"]:
            send = (f"⚠️ <b>{esc(hut['name'])}</b> check has failed {res['errors']} runs in a row.\n"
                    f"{esc(res['detail'])}\n"
                    + ("The site is blocking automated checks - check it by hand for now."
                       if res.get("blocked") else "The monitor may need fixing for this hut."))
        if send and telegram(send):
            res["alerted"] = ts.isoformat()
            sent.append(hut["id"])

        results[hut["id"]] = res
        print(f"{hut['night']}  {hut['name']:<18} {status:<24} {str(res.get('detail',''))[:80]}")

    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps({**results, "_reminders": reminders_done},
                                     indent=1, ensure_ascii=False), encoding="utf-8")
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": ts.isoformat(), "alerts": sent,
                            "status": {k: v["status"] for k, v in results.items()}}) + "\n")
    write_status(results, state, ts)
    print(f"Alerts sent: {sent or 'none'}")


if __name__ == "__main__":
    main()
