"""Read-only check: can the scheduler see JD's calendars?"""
import tomllib
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

RULES = "/opt/brain-server/scheduler/rules.toml"
KEY = "/etc/brain-scheduler/service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

with open(RULES, "rb") as f:
    rules = tomllib.load(f)

creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

now = datetime.now(timezone.utc).isoformat()

for label, cal_id in [("primary", rules["calendars"]["read"]),
                      ("brain blocks", rules["calendars"]["write"])]:
    result = cal.events().list(
        calendarId=cal_id, timeMin=now, maxResults=5,
        singleEvents=True, orderBy="startTime",
    ).execute()
    events = result.get("items", [])
    print(f"{label}: {len(events)} upcoming event(s) visible")
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        print(f"  {start}  location: {'yes' if e.get('location') else 'no'}")
