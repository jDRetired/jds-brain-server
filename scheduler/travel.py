"""Travel time from home to an appointment's location (Routes API, Essentials tier)."""
import json
import math
import sys
import tomllib
import urllib.error
import urllib.request

from datetime import datetime, timedelta
from blocks import load_blocks

PLACES = "/etc/brain-scheduler/places.toml"
MAPS_KEY = "/etc/brain-scheduler/maps-key"
RULES = "/opt/brain-server/scheduler/rules.toml"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def load_places():
    with open(PLACES, "rb") as f:
        return tomllib.load(f)


def is_online(location, rules):
    """True for Teams/Zoom/links: no travel needed."""
    loc = location.lower()
    return any(m.lower() in loc for m in rules["travel"].get("online_markers", []))


def resolve(location, places):
    """Alias name -> full address; anything else passes through as typed."""
    aliases = {k.lower(): v for k, v in places.get("aliases", {}).items()}
    return aliases.get(location.strip().lower(), location)


def drive_minutes(origin, destination):
    """One-way drive time, rounded UP to the next 15 min. None if Maps can't route it."""
    with open(MAPS_KEY) as f:
        key = f.read().strip()            # the file ends in a newline; Google won't
    body = json.dumps({
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",   # this is what keeps it Essentials, not Pro
    }).encode()
    req = urllib.request.Request(ROUTES_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration",    # ask for one field, nothing else
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"Maps lookup failed: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Maps lookup failed: {e}", file=sys.stderr)
        return None
    routes = data.get("routes")
    if not routes:
        return None                       # Maps couldn't make sense of the place
    seconds = int(routes[0]["duration"].rstrip("s"))   # Google sends e.g. "1325s"
    return math.ceil(seconds / 900) * 15


def sync_travel(cal, rules, monday, week_start, week_end, now, tz, apply):
    """Make this week's travel blocks match located appointments. Returns (spans, n_changes)."""
    read_id, write_id = rules["calendars"]["read"], rules["calendars"]["write"]
    places = load_places()
    title = rules["travel"].get("title", "Travel")
    both = rules["travel"].get("before_and_after", True)
    fallback = rules["travel"]["fallback_minutes"]

    existing, refused = {}, set()          # keyed by (appointment id, "before"/"after")
    for b in load_blocks(cal, write_id, monday, tz):
        if b["kind"] != "travel":
            continue
        key = (b["props"].get("brain_event"), b["props"].get("brain_side"))
        if b["status"] == "deleted":
            refused.add(key)               # you deleted it: don't bring it back
        else:
            existing[key] = b

    events = cal.events().list(
        calendarId=read_id, singleEvents=True, maxResults=250,
        timeMin=week_start.isoformat(), timeMax=week_end.isoformat(),
    ).execute().get("items", [])

    wanted = {}
    for e in events:
        loc = (e.get("location") or "").strip()
        if not loc or "dateTime" not in e.get("start", {}) or is_online(loc, rules):
            continue
        ev_s = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(tz)
        ev_e = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(tz)
        sides = ["before", "after"] if both else ["before"]
        live = [s for s in sides if (e["id"], s) not in refused]
        if ev_e <= now or not live:
            continue                       # over, or you turned its travel off: no Maps call
        prev = existing.get((e["id"], "before")) or existing.get((e["id"], "after"))
        if prev and prev["props"].get("brain_loc") == loc:
            minutes = int(prev["props"]["brain_minutes"])     # cache hit: no Maps call
        else:
            minutes = drive_minutes(places["home"], resolve(loc, places)) or fallback
        d = timedelta(minutes=minutes)
        spans = {"before": (ev_s - d, ev_s), "after": (ev_e, ev_e + d)}
        for side in live:
            wanted[(e["id"], side)] = (*spans[side], loc, minutes)

    changes = 0
    for (ev_id, side), (s, t, loc, minutes) in wanted.items():
        b = existing.pop((ev_id, side), None)
        if b and b["actual"] == (s, t) and b["props"].get("brain_loc") == loc and b["title"] == title:
            continue                       # already right
        changes += 1
        print(f"{'retime' if b else 'create':8} travel {s:%a %m/%d %H:%M}-{t:%H:%M}")
        if not apply:
            continue
        body = {
            "summary": title,
            "start": {"dateTime": s.isoformat(), "timeZone": rules["timezone"]},
            "end": {"dateTime": t.isoformat(), "timeZone": rules["timezone"]},
            "extendedProperties": {"private": {
                "brain_kind": "travel", "brain_week": monday.isoformat(),
                "brain_start": s.isoformat(), "brain_end": t.isoformat(),
                "brain_event": ev_id, "brain_side": side,
                "brain_loc": loc, "brain_minutes": str(minutes)}},
        }
        if b:
            cal.events().patch(calendarId=write_id, eventId=b["id"], body=body).execute()
        else:
            cal.events().insert(calendarId=write_id, body=body).execute()

    for b in existing.values():            # leftovers: appointment gone or lost its location
        if b["actual"][1] <= now:
            continue                       # never touch the past
        changes += 1
        s, t = b["actual"]
        print(f"delete   travel {s.astimezone(tz):%a %m/%d %H:%M}-{t.astimezone(tz):%H:%M}")
        if apply:
            props = {**b["props"], "brain_week": "retired"}
            cal.events().patch(calendarId=write_id, eventId=b["id"],
                               body={"extendedProperties": {"private": props}}).execute()
            cal.events().delete(calendarId=write_id, eventId=b["id"]).execute()

    return [(s, t) for s, t, _, _ in wanted.values()], changes

if __name__ == "__main__":
    with open(RULES, "rb") as f:
        rules = tomllib.load(f)
    places = load_places()
    loc = sys.argv[1]
    if is_online(loc, rules):
        print("online location: no travel")
        sys.exit()
    dest = resolve(loc, places)
    print("alias matched:", dest != loc)
    print("one-way minutes:", drive_minutes(places["home"], dest))
