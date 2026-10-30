"""Dry run: show open slots for dev and study in one week. Writes nothing."""
import sys
import tomllib
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

RULES = "/opt/brain-server/scheduler/rules.toml"
KEY = "/etc/brain-scheduler/service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def at(day, hhmm, tz):
    """Turn a date + "HH:MM" into a time-zone-aware datetime."""
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=tz)


def windows_for(day, rules, tz):
    """Where each block type is allowed to go on this day."""
    fence, dev, study = rules["work_fence"], rules["dev"], rules["study"]
    out = {"dev": [], "study": []}
    if DAY_NAMES[day.weekday()] in fence["days"]:
        out["dev"].append((at(day, dev["window"]["start"], tz), at(day, dev["window"]["end"], tz)))
        w = study["weekday_window"]
    else:
        w = study["weekend_window"]
    out["study"].append((at(day, w["start"], tz), at(day, w["end"], tz)))
    return out


def subtract(window, busy):
    """Cut busy intervals out of one window; return what's left."""
    start, end = window
    gaps = []
    for b_start, b_end in sorted(busy):
        if b_end <= start or b_start >= end:
            continue                      # doesn't overlap this window
        if b_start > start:
            gaps.append((start, b_start))  # free time before the busy block
        start = max(start, b_end)          # jump past the busy block
    if start < end:
        gaps.append((start, end))
    return gaps


def fetch_busy(cal, cal_id, start, end, tz):
    """Ask Google for busy intervals only (no titles, no details)."""
    body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "items": [{"id": cal_id}]}
    info = cal.freebusy().query(body=body).execute()["calendars"][cal_id]
    if info.get("errors"):
        raise RuntimeError(info["errors"])
    return [(datetime.fromisoformat(b["start"]).astimezone(tz),
             datetime.fromisoformat(b["end"]).astimezone(tz))
            for b in info["busy"]]


def fetch_work_buffers(cal, cal_id, start, end, tz, rules):
    """No-study zones right after events titled like 'Work Meeting'."""
    prefix = rules.get("work_title_prefix", "")
    minutes = rules["study"].get("buffer_after_work_minutes", 0)
    if not prefix or not minutes:
        return []
    pattern = re.compile(rf"{re.escape(prefix)}\b", re.IGNORECASE)   # 'Work' yes, 'Workout' no
    items = cal.events().list(
        calendarId=cal_id, singleEvents=True, maxResults=250,
        timeMin=start.isoformat(), timeMax=end.isoformat(),
    ).execute().get("items", [])
    out = []
    for e in items:
        if "dateTime" not in e.get("end", {}):      # all-day events have no end time to buffer
            continue
        if pattern.match(e.get("summary", "").strip()):
            ev_end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(tz)
            out.append((ev_end, ev_end + timedelta(minutes=minutes)))
    return out


def main():
    with open(RULES, "rb") as f:
        rules = tomllib.load(f)
    tz = ZoneInfo(rules["timezone"])

    # Week to plan: a date given on the command line, else the dev start date
    day0 = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else rules["dev"]["start_date"])
    monday = day0 - timedelta(days=day0.weekday())
    week_start = at(monday, "00:00", tz)
    week_end = at(monday + timedelta(days=7), "00:00", tz)

    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    busy = fetch_busy(cal, rules["calendars"]["read"], week_start, week_end, tz)

    totals = {"dev": 0.0, "study": 0.0}
    for i in range(7):
        day = monday + timedelta(days=i)
        for kind, wins in windows_for(day, rules, tz).items():
            min_len = timedelta(minutes=rules[kind]["min_block_minutes"])
            for w in wins:
                for g_start, g_end in subtract(w, busy):
                    if g_end - g_start >= min_len:
                        hrs = (g_end - g_start).total_seconds() / 3600
                        totals[kind] += hrs
                        print(f"{day:%a %m/%d}  {kind:5}  {g_start:%H:%M}-{g_end:%H:%M}  ({hrs:.1f} h)")

    for kind, hrs in totals.items():
        r = rules[kind]
        print(f"{kind}: {hrs:.1f} h open (goal {r['min_hours_week']}-{r['ideal_hours_week']} h)")


if __name__ == "__main__":
    main()
