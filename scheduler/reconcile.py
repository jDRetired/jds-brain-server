"""Reconcile Brain blocks with the calendar. Dry run unless --apply is given."""
import sys
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from travel import sync_travel

from google.oauth2 import service_account
from googleapiclient.discovery import build

from blocks import load_blocks
from plan import SCOPES, fetch_days_off, place, week_target
from slots import KEY, RULES, at, fetch_busy, fetch_work_buffers

KINDS = ("dev", "study")


def overlaps(span, intervals):
    s, e = span
    return any(bs < e and be > s for bs, be in intervals)


def reconcile_week(cal, rules, monday, now, tz, apply):
    read_id, write_id = rules["calendars"]["read"], rules["calendars"]["write"]
    week_start = at(monday, "00:00", tz)
    week_end = at(monday + timedelta(days=7), "00:00", tz)
    busy = fetch_busy(cal, read_id, week_start, week_end, tz)
    days_off = fetch_days_off(cal, read_id, monday, rules, tz)
    work_buffers = fetch_work_buffers(cal, read_id, week_start, week_end, tz, rules)
    travel_spans, travel_changes = sync_travel(cal, rules, monday, week_start, week_end, now, tz, apply)
    busy = busy + travel_spans              # drive time is busy time for dev and study
    off_spans = [(at(d, "00:00", tz), at(d + timedelta(days=1), "00:00", tz)) for d in days_off]

    keep, actions = [], []
    blocked = list(busy)                       # where a refill may NOT go
    for b in load_blocks(cal, write_id, monday, tz):
        if b["status"] == "untagged" or b["kind"] not in KINDS:
            continue
        if b["status"] == "deleted":
            blocked.append(b["planned"])       # you said no to that slot
            continue
        span = b["actual"]
        if span[1] <= now:                     # in the past: never touched
            keep.append((b["kind"], span))
            continue
        extra = work_buffers if b["kind"] == "study" else []
        if overlaps(span, busy) or overlaps(span, off_spans) or overlaps(span, extra):
            actions.append(("delete", b, span))    # appointment / day off wins
            continue
        keep.append((b["kind"], span))
        if b["title"] != rules[b["kind"]]["title"]:
            actions.append(("retitle", b, span))

    blocked += [span for _, span in keep]
    up = now.replace(second=0, microsecond=0) + timedelta(minutes=-now.minute % 15)
    blocked.append((week_start, up))     # never refill into the past (snap to :00/:15/:30/:45)

    for kind in KINDS:
        have = sum((e - s for k, (s, e) in keep if k == kind), timedelta())
        short = week_target(kind, monday, rules, days_off, tz) - have
        if short < timedelta(minutes=rules[kind]["min_block_minutes"]):
            continue                           # close enough: don't chase crumbs
        avoid = blocked + (work_buffers if kind == "study" else [])
        for span in place(kind, monday, rules, avoid, days_off, tz, target=short):
            actions.append(("create", kind, span))

    for act, obj, (s, e) in actions:
        kind = obj if act == "create" else obj["kind"]
        print(f"{act:8} {kind:5} {s.astimezone(tz):%a %m/%d %H:%M}-{e.astimezone(tz):%H:%M}")
        if not apply:
            continue
        if act == "delete":
            # Retire the tag first, so this code-made delete isn't
            # mistaken for one of YOUR deletes on the next run.
            props = {**obj["props"], "brain_week": "retired"}
            cal.events().patch(calendarId=write_id, eventId=obj["id"],
                               body={"extendedProperties": {"private": props}}).execute()
            cal.events().delete(calendarId=write_id, eventId=obj["id"]).execute()
        elif act == "retitle":
            cal.events().patch(calendarId=write_id, eventId=obj["id"],
                               body={"summary": rules[kind]["title"]}).execute()
        else:
            cal.events().insert(calendarId=write_id, body={
                "summary": rules[kind]["title"],
                "start": {"dateTime": s.isoformat(), "timeZone": rules["timezone"]},
                "end": {"dateTime": e.isoformat(), "timeZone": rules["timezone"]},
                "extendedProperties": {"private": {
                    "brain_kind": kind, "brain_week": monday.isoformat(),
                    "brain_start": s.isoformat(), "brain_end": e.isoformat()}},
            }).execute()
    return len(actions) + travel_changes


def main():
    apply = "--apply" in sys.argv
    with open(RULES, "rb") as f:
        rules = tomllib.load(f)
    tz = ZoneInfo(rules["timezone"])
    now = datetime.now(tz)
    this_monday = now.date() - timedelta(days=now.weekday())

    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

    total = 0
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if args:
        d = date.fromisoformat(args[0])
        weeks = [d - timedelta(days=d.weekday())]
    else:
        weeks = [this_monday, this_monday + timedelta(days=7)]
    for monday in weeks:
        print(f"--- week of {monday:%m/%d}")
        total += reconcile_week(cal, rules, monday, now, tz, apply)
    if total == 0:
        print("No changes.")
    elif not apply:
        print("\nDry run: nothing written. Add --apply to make these changes.")


if __name__ == "__main__":
    main()
