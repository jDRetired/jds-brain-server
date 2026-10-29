"""Plan one week of dev + study blocks. Dry run unless --apply is given."""
import sys
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from slots import KEY, RULES, at, fetch_busy, subtract, windows_for

SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.readonly"]


def round15(td):
    return timedelta(minutes=15 * round(td.total_seconds() / 900))


def fetch_days_off(cal, cal_id, monday, rules, tz):
    """Holidays from the rules file + days fully covered by Out-of-office events."""
    off = {d for d in rules.get("days_off", []) if monday <= d < monday + timedelta(days=7)}
    result = cal.events().list(
        calendarId=cal_id, eventTypes="outOfOffice", singleEvents=True,
        timeMin=at(monday, "00:00", tz).isoformat(),
        timeMax=at(monday + timedelta(days=7), "00:00", tz).isoformat(),
    ).execute()
    for e in result.get("items", []):
        s = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(tz)
        end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(tz)
        for i in range(7):
            d = monday + timedelta(days=i)
            if s <= at(d, "00:00", tz) and end >= at(d + timedelta(days=1), "00:00", tz):
                off.add(d)   # only whole days count; partial OOO is just busy time
    return off


def week_days(kind, monday, rules, days_off, tz):
    """(normal days for this block type, those days minus days off)."""
    start_date = date.fromisoformat(rules[kind].get("start_date", "2000-01-01"))
    week = [monday + timedelta(days=i) for i in range(7)]
    normal = [d for d in week if d >= start_date and windows_for(d, rules, tz)[kind]]
    return normal, [d for d in normal if d not in days_off]


def week_target(kind, monday, rules, days_off, tz):
    """The week's ideal hours, shrunk in proportion to days off."""
    normal, days = week_days(kind, monday, rules, days_off, tz)
    if not days:
        return timedelta(0)
    return timedelta(hours=rules[kind]["ideal_hours_week"]) * len(days) / len(normal)


def place(kind, monday, rules, busy, days_off, tz, target=None):
    """Spread `target` (default: the week's goal) evenly across eligible days."""
    r = rules[kind]
    min_b = timedelta(minutes=r["min_block_minutes"])
    max_b = timedelta(minutes=r["max_block_minutes"])
    brk = timedelta(minutes=rules["break_minutes"])
    _, days = week_days(kind, monday, rules, days_off, tz)
    if not days:
        return []

    remaining = week_target(kind, monday, rules, days_off, tz) if target is None else target
    blocks = []
    for i, day in enumerate(days):
        quota = remaining / (len(days) - i)
        placed = timedelta(0)
        for w in windows_for(day, rules, tz)[kind]:
            for g_start, g_end in subtract(w, busy):
                cursor = g_start
                while quota - placed >= timedelta(minutes=15):
                    want = round15(min(max_b, max(min_b, quota - placed)))
                    if cursor + want > g_end:
                        want = g_end - cursor
                        if want < min_b:
                            break
                    blocks.append((cursor, cursor + want))
                    placed += want
                    cursor += want + brk
        remaining -= placed
    return blocks


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv

    with open(RULES, "rb") as f:
        rules = tomllib.load(f)
    tz = ZoneInfo(rules["timezone"])
    day0 = date.fromisoformat(args[0] if args else rules["dev"]["start_date"])
    monday = day0 - timedelta(days=day0.weekday())
    week_start, week_end = at(monday, "00:00", tz), at(monday + timedelta(days=7), "00:00", tz)

    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    read_id, write_id = rules["calendars"]["read"], rules["calendars"]["write"]
    busy = fetch_busy(cal, read_id, week_start, week_end, tz)
    days_off = fetch_days_off(cal, read_id, monday, rules, tz)
    if days_off:
        print("Days off: " + ", ".join(f"{d:%a %m/%d}" for d in sorted(days_off)))

    plan = [(k, s, e) for k in ("dev", "study") for s, e in place(k, monday, rules, busy, days_off, tz)]
    for kind, s, e in sorted(plan, key=lambda p: p[1]):
        print(f"{s:%a %m/%d}  {kind:5}  {s:%H:%M}-{e:%H:%M}")
    for kind in ("dev", "study"):
        hrs = sum((e - s for k, s, e in plan if k == kind), timedelta()).total_seconds() / 3600
        print(f"{kind}: {hrs:.1f} h planned")

    if not apply:
        print("\nDry run: nothing written. Add --apply to write these blocks.")
        return

    tag = f"brain_week={monday.isoformat()}"
    existing = cal.events().list(calendarId=write_id, privateExtendedProperty=tag).execute()
    if existing.get("items"):
        print(f"\nRefusing: this week already has {len(existing['items'])} Brain blocks.")
        return

    for kind, s, e in plan:
        cal.events().insert(calendarId=write_id, body={
            "summary": rules[kind]["title"],
            "start": {"dateTime": s.isoformat(), "timeZone": rules["timezone"]},
            "end": {"dateTime": e.isoformat(), "timeZone": rules["timezone"]},
            "extendedProperties": {"private": {
                "brain_kind": kind,
                "brain_week": monday.isoformat(),
                "brain_start": s.isoformat(),   # where the code put it...
                "brain_end": e.isoformat(),     # ...so a mismatch later = you moved it
            }},
        }).execute()
    print(f"\nWrote {len(plan)} blocks to Brain Blocks.")


if __name__ == "__main__":
    main()
