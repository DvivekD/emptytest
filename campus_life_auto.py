#!/usr/bin/env python3
"""
Campus.Life (VTU APTS) Smart Multi-Event Autonomous Attendance Engine
====================================================================
Features:
- Earliest-Deadline-First (EDF) event priority scheduling.
- Automatic event switching once 80 hours target is achieved.
- Daily check-in (9:00 AM) & check-out (5:00 PM) with human jitter.
- Dynamic department ID resolution via subDepartments.php.
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
    """
    Selects the highest-priority event:
    1. Filter: active == true and completed_hours < target_hours.
    2. Sort: Earliest deadline first (ascending), then closest to 80 hrs (descending).
    """
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
        # Sort primary by deadline (earliest first), secondary by remaining hours (least remaining first)
        remaining = e.get("target_hours", 80.0) - e.get("completed_hours", 0)
        return (deadline, remaining)

    candidates.sort(key=priority_key)
    return candidates[0]


def resolve_department_ids(event_id: str, rider_id: str = DEFAULT_RIDER_ID):
    """
    Queries subDepartments.php to get exact Checkin and Checkout department IDs.
    """
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
        log(f"Dynamic resolution error: {e}. Falling back to default pattern...")

    # Pattern fallback
    return "CHEKIN6023YOE602", "CHEKOUT6023YOE602"


def send_scan_request(event, action: str, department_id: str):
    """
    Submits scanActivities.php.
    """
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

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            log(f"Server Response: {body}")
            return True
    except Exception as e:
        log(f"Error submitting {action}: {e}")
        return False


def do_checkin(jitter: bool = False):
    event = get_prioritized_event()
    if not event:
        log("No eligible events found in database! All completed or none active.")
        return

    if jitter:
        delay = random.randint(120, 480)
        log(f"Jitter: Pausing for {delay // 60}m {delay % 60}s before Check-In...")
        time.sleep(delay)

    checkin_id, _ = resolve_department_ids(event["event_id"])
    success = send_scan_request(event, "checkin", checkin_id)

    if success:
        state = load_state()
        state["current_event_id"] = event["event_id"]
        state["checkin_timestamp"] = datetime.datetime.now().isoformat()
        save_state(state)
        log(f"Check-In registered for event [{event['name']}].")


def do_checkout(jitter: bool = False):
    state = load_state()
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
        delay = random.randint(120, 480)
        log(f"Jitter: Pausing for {delay // 60}m {delay % 60}s before Check-Out...")
        time.sleep(delay)

    _, checkout_id = resolve_department_ids(target_event["event_id"])
    success = send_scan_request(target_event, "checkout", checkout_id)

    if success:
        # Calculate session hours
        checkin_time_str = state.get("checkin_timestamp")
        added_hours = 8.0
        if checkin_time_str:
            try:
                c_time = datetime.datetime.fromisoformat(checkin_time_str)
                elapsed = (datetime.datetime.now() - c_time).total_seconds() / 3600.0
                if elapsed < 0.2:
                    added_hours = 0.0
                    log(f"Session was under 12 minutes ({round(elapsed * 60, 1)}m). Adding 0.0 hrs to progress counter.")
                elif 0.2 <= elapsed <= 12.0:
                    added_hours = round(elapsed, 1)
            except Exception:
                pass

        # Update event hours
        target_event["completed_hours"] = round(target_event.get("completed_hours", 0) + added_hours, 1)
        log(f"Added +{added_hours} hrs. Progress: {target_event['completed_hours']} / {target_event.get('target_hours', 80.0)} hrs.")

        if target_event["completed_hours"] >= target_event.get("target_hours", 80.0):
            log(f"🎉 TARGET REACHED for [{target_event['name']}] ({target_event['completed_hours']} hrs)!")
            next_event = get_prioritized_event()
            if next_event and next_event["event_id"] != target_event["event_id"]:
                log(f"➡️ Automatically switching tomorrow to next priority event: [{next_event['name']}]")
            else:
                log("All queued events in database are now complete!")

        save_events(events)
        state["last_checkout_timestamp"] = datetime.datetime.now().isoformat()
        state["current_event_id"] = None
        save_state(state)


def print_status():
    events = load_events()
    active_cand = get_prioritized_event()
    state = load_state()

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
    parser = argparse.ArgumentParser(description="Campus.Life Multi-Event Autonomous Attendance")
    parser.add_argument("--action", choices=["checkin", "checkout", "status"], required=True)
    parser.add_argument("--jitter", action="store_true", help="Add random human delay (2-8 min)")
    args = parser.parse_args()

    if args.action == "status":
        print_status()
    elif args.action == "checkin":
        do_checkin(jitter=args.jitter)
    elif args.action == "checkout":
        do_checkout(jitter=args.jitter)


if __name__ == "__main__":
    main()
