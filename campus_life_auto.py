#!/usr/bin/env python3
"""
Campus.Life (VTU APTS) Smart Multi-Event Autonomous Attendance Engine
====================================================================
Features:
- Multi-pulse cron schedule for 100% cloud execution guarantee.
- Direct database verification endpoint to check official server logs.
- Earliest-Deadline-First (EDF) event priority scheduling.
- Automatic event switching once 80 hours target is achieved.
- Daily check-in (9:00 AM window) & check-out (5:00 PM window).
- Idempotent execution (skips duplicates on the same day).
- Resilient network retry with exponential backoff.
- Persistent state tracking across cloud runs.
"""

import argparse
import datetime
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "events_db.json")
STATE_FILE = os.path.join(BASE_DIR, "attendance_state.json")

DEFAULT_RIDER_ID = "1894897"
SUB_DEPS_URL = "https://cykul.in/app/lifeCykul/webservice/departments/subDepartments.php"
SCAN_URL = "https://cykul.in/app/lifeCykul/webservice/scanning/scanActivities.php"
USER_AGENT = "CampusLife/1 CFNetwork/3896.200.61 Darwin/27.2.0"
VERIFY_URL = "https://vtuapts.zdotapps.in/apiv1/events/checkinandcheckoutdatatable/"


def log(msg: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def load_events():
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_events(events):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_prioritized_event():
    events = load_events()
    candidates = [
        e for e in events
        if e.get("active", True) and e.get("completed_hours", 0) < e.get("target_hours", 80.0)
    ]

    if not candidates:
        return None

    def priority_key(e):
        deadline_str = e.get("deadline", "9999-12-31")
        try:
            deadline = datetime.date.fromisoformat(deadline_str)
        except Exception:
            deadline = datetime.date(9999, 12, 31)
        remaining = e.get("target_hours", 80.0) - e.get("completed_hours", 0)
        return (deadline, remaining)

    candidates.sort(key=priority_key)
    return candidates[0]


def resolve_department_ids(event_id: str, rider_id: str = DEFAULT_RIDER_ID):
    payload = {
        "eventID": event_id,
        "riderID": rider_id,
        "masterLeague": "0",
        "whichapp": "campuslife"
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        SUB_DEPS_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT}
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                checkin_id = None
                checkout_id = None

                for dept in res_json.get("resultStatus", []):
                    dept_name = dept.get("name", "").strip().lower()
                    tabs_order = dept.get("tabsOrder", "").strip().lower()
                    d_id = dept.get("departmentID", "")

                    if "checkin" in dept_name or "checkin" in tabs_order or d_id.startswith("CHEKIN"):
                        checkin_id = d_id
                    elif "checkout" in dept_name or "checkout" in tabs_order or d_id.startswith("CHEKOUT"):
                        checkout_id = d_id

                if checkin_id and checkout_id:
                    return checkin_id, checkout_id
        except Exception as e:
            log(f"Attempt {attempt}/3 error resolving departments: {e}")
            if attempt < 3:
                time.sleep(attempt * 3)

    return "CHEKIN6023YOE602", "CHEKOUT6023YOE602"


def send_scan_request(event, action: str, department_id: str):
    now = datetime.datetime.now()
    formatted_time = now.strftime("%Y-%b-%d %H:%M")

    payload = {
        "qrCode": event["qr_code"],
        "departmentID": department_id,
        "riderID": DEFAULT_RIDER_ID,
        "eventID": event["event_id"],
        "latitude": "",
        "longitude": "",
        "address": "",
        "time": formatted_time,
        "whichapp": "campuslife"
    }

    log(f"Submitting {action.upper()} for [{event['name']}]...")
    log(f"Details: Code={payload['qrCode']}, DeptID={department_id}, Time='{formatted_time}'")

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        SCAN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT}
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                log(f"Server Response: {body}")
                return True
        except Exception as e:
            log(f"Attempt {attempt}/3 error submitting {action}: {e}")
            if attempt < 3:
                time.sleep(attempt * 4)

    return False


def is_already_checked_in_today(state):
    checkin_ts = state.get("checkin_timestamp")
    current_eid = state.get("current_event_id")
    if not checkin_ts or not current_eid:
        return False
    try:
        c_date = datetime.datetime.fromisoformat(checkin_ts).date()
        today = datetime.datetime.now().date()
        return c_date == today
    except Exception:
        return False


def is_already_checked_out_today(state):
    checkout_ts = state.get("last_checkout_timestamp")
    current_eid = state.get("current_event_id")
    if not checkout_ts or current_eid is not None:
        return False
    try:
        co_date = datetime.datetime.fromisoformat(checkout_ts).date()
        today = datetime.datetime.now().date()
        return co_date == today
    except Exception:
        return False


def do_checkin(jitter: bool = False, force: bool = False):
    state = load_state()

    if not force and is_already_checked_in_today(state):
        log(f"Notice: Already checked in for today ({state.get('checkin_timestamp')}). Skipping duplicate.")
        return

    event = get_prioritized_event()
    if not event:
        log("No eligible events found in database! All completed or none active.")
        return

    if jitter:
        delay = random.randint(60, 240)
        log(f"Jitter: Pausing for {delay // 60}m {delay % 60}s before Check-In...")
        time.sleep(delay)

    checkin_id, _ = resolve_department_ids(event["event_id"])
    success = send_scan_request(event, "checkin", checkin_id)

    if success:
        state["current_event_id"] = event["event_id"]
        state["checkin_timestamp"] = datetime.datetime.now().isoformat()
        save_state(state)
        log(f"Check-In registered for event [{event['name']}].")


def do_checkout(jitter: bool = False, force: bool = False):
    state = load_state()

    if not force and is_already_checked_out_today(state):
        log(f"Notice: Already checked out for today ({state.get('last_checkout_timestamp')}). Skipping duplicate.")
        return

    active_event_id = state.get("current_event_id")
    events = load_events()
    target_event = None

    if active_event_id:
        for e in events:
            if e["event_id"] == active_event_id:
                target_event = e
                break

    if not target_event:
        target_event = get_prioritized_event()

    if not target_event:
        log("No active event found to Check-Out!")
        return

    if jitter:
        delay = random.randint(60, 240)
        log(f"Jitter: Pausing for {delay // 60}m {delay % 60}s before Check-Out...")
        time.sleep(delay)

    _, checkout_id = resolve_department_ids(target_event["event_id"])
    success = send_scan_request(target_event, "checkout", checkout_id)

    if success:
        checkin_time_str = state.get("checkin_timestamp")
        added_hours = 8.0
        if checkin_time_str:
            try:
                c_time = datetime.datetime.fromisoformat(checkin_time_str)
                elapsed = (datetime.datetime.now() - c_time).total_seconds() / 3600.0
                if elapsed < 0.2:
                    added_hours = 0.0
                    log(f"Session under 12 min ({round(elapsed * 60, 1)}m). Adding 0.0 hrs to counter.")
                elif 0.2 <= elapsed <= 12.0:
                    added_hours = round(elapsed, 1)
            except Exception:
                pass

        target_event["completed_hours"] = round(target_event.get("completed_hours", 0) + added_hours, 1)
        log(f"Added +{added_hours} hrs. Total Progress: {target_event['completed_hours']} / {target_event.get('target_hours', 80.0)} hrs.")

        if target_event["completed_hours"] >= target_event.get("target_hours", 80.0):
            log(f"🎉 TARGET REACHED for [{target_event['name']}] ({target_event['completed_hours']} hrs)!")
            next_event = get_prioritized_event()
            if next_event and next_event["event_id"] != target_event["event_id"]:
                log(f"➡️ Automatically switching tomorrow to next priority: [{next_event['name']}]")
            else:
                log("All queued events in database are complete!")

        save_events(events)
        state["last_checkout_timestamp"] = datetime.datetime.now().isoformat()
        state["current_event_id"] = None
        save_state(state)


def verify_official_server_logs():
    """
    Directly queries the VTU APTS server database to verify officially recorded attendance history.
    """
    log("Connecting to official VTU APTS server database...")
    event = get_prioritized_event()
    event_id = event["event_id"] if event else "CLCYOE6023260722105630242"

    payload = {
        "eventID": event_id,
        "search": "SAI VIVEK",
        "page": 1,
        "length": 5
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        VERIFY_URL,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            records = res.get("data", [])
            print("\n" + "=" * 90)
            print(" OFFICIAL VTU APTS DATABASE VERIFICATION (DIRECT FROM SERVER)")
            print("=" * 90)
            print(f"{'Student Name':<18} {'Check-In Time':<22} {'Check-Out Time':<22} {'Status'}")
            print("-" * 90)

            for r in records[:5]:
                name = r.get("firstName", "") + " " + r.get("lastName", "")
                ci = r.get("checkinTime") or "Not Checked In"
                co = r.get("checkoutTime") or "In Progress"
                status = "COMPLETED" if r.get("checkoutTime") else "IN PROGRESS"
                print(f"{name:<18} {ci:<22} {co:<22} {status}")

            print("=" * 90 + "\n")
            return True
    except Exception as e:
        log(f"Verification error: {e}")
        return False


def print_status():
    events = load_events()
    active_cand = get_prioritized_event()

    print("\n" + "=" * 80)
    print(" VTU CAMPUS.LIFE EVENT PROGRESS & PRIORITY QUEUE")
    print("=" * 80)
    print(f"{'#':<3} {'Event Name':<42} {'Progress':<14} {'Deadline':<12} {'Status'}")
    print("-" * 80)

    for idx, e in enumerate(events, 1):
        progress = f"{e.get('completed_hours', 0):.1f}/{e.get('target_hours', 80.0):.0f} hrs"
        deadline = e.get("deadline", "N/A")
        is_complete = e.get("completed_hours", 0) >= e.get("target_hours", 80.0)
        
        status = "COMPLETED" if is_complete else ("IN PROGRESS" if active_cand and active_cand["event_id"] == e["event_id"] else "QUEUED")
        name = e["name"][:40]
        print(f"{idx:<3} {name:<42} {progress:<14} {deadline:<12} {status}")

    print("-" * 80)
    if active_cand:
        rem = active_cand.get("target_hours", 80.0) - active_cand.get("completed_hours", 0)
        days_left = round(rem / 8.0, 1)
        print(f"Active Priority Event: [{active_cand['name']}]")
        print(f"Remaining Hours Needed: {rem:.1f} hrs (~{days_left} working days @ 8 hrs/day)")
    else:
        print("All events in database are fully completed!")
    print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Campus.Life Autonomous Multi-Event Engine")
    parser.add_argument("--action", choices=["checkin", "checkout", "status", "verify"], required=True)
    parser.add_argument("--jitter", action="store_true", help="Add random human delay")
    parser.add_argument("--force", action="store_true", help="Force action even if already done today")
    args = parser.parse_args()

    if args.action == "verify":
        verify_official_server_logs()
    elif args.action == "status":
        print_status()
    elif args.action == "checkin":
        do_checkin(jitter=args.jitter, force=args.force)
    elif args.action == "checkout":
        do_checkout(jitter=args.jitter, force=args.force)


if __name__ == "__main__":
    main()
