"""Read-only: sort a week's Brain blocks into untouched / moved / deleted."""
import sys
import tomllib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from slots import KEY, RULES, SCOPES


def load_blocks(cal, write_id, monday, tz):
    """Every block this code placed for the week, including ones you deleted."""
    tag = f"brain_week={monday.isoformat()}"
    items = cal.events().list(
        calendarId=write_id, privateExtendedProperty=tag,
        showDeleted=True, singleEvents=True, maxResults=250,
    ).execute().get("items", [])

    blocks = []
    for e in items:
        p = e.get("extendedProperties", {}).get("private", {})
        if "brain_start" not in p:
            status, planned, actual = "untagged", None, None
        else:
            planned = (datetime.fromisoformat(p["brain_start"]),
                       datetime.fromisoformat(p["brain_end"]))
            if e.get("status") == "cancelled":
                status, actual = "deleted", None
            else:
                actual = (datetime.fromisoformat(e["start"]["dateTime"]),
                          datetime.fromisoformat(e["end"]["dateTime"]))
                status = "untouched" if actual == planned else "moved"
        blocks.append({"id": e["id"], "kind": p.get("brain_kind"), "status": status, "props": p,
                       "planned": planned, "actual": actual,
                       "title": e.get("summary")})
    return blocks


def main():
    with open(RULES, "rb") as f:
        rules = tomllib.load(f)
    tz = ZoneInfo(rules["timezone"])
    day0 = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else rules["dev"]["start_date"])
    monday = day0 - timedelta(days=day0.weekday())

    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

    for b in sorted(load_blocks(cal, rules["calendars"]["write"], monday, tz),
                    key=lambda b: b["planned"][0] if b["planned"] else datetime.max.replace(tzinfo=tz)):
        ps = b["planned"][0].astimezone(tz) if b["planned"] else None
        line = f"{ps:%a %m/%d %H:%M}  {b['kind']:5}  {b['status']:9}" if ps else f"untagged  {b['title']}"
        if b["status"] == "moved":
            line += f"  now {b['actual'][0].astimezone(tz):%a %H:%M}-{b['actual'][1].astimezone(tz):%H:%M}"
        print(line)


if __name__ == "__main__":
    main()
