import json
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, Response

from db import get_db, init_db, row_to_student, GROUP_SIZE_MAX
from scheduler import generate_schedule_for_week, DAY_NAMES, time_to_min

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "student-scheduler-local-dev")

# Initialize the database at import time so this also works when run by a
# production WSGI server (e.g. on PythonAnywhere), not just via `python app.py`.
init_db()

# --------------------------------------------------------------- basic auth --
# Since this app can hold real student names/schools, it's protected with a
# simple login whenever it's reachable over the internet. Set APP_USERNAME /
# APP_PASSWORD as environment variables on your host; these defaults are only
# for local use on your own machine.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")


def _check_auth(username, password):
    return username == APP_USERNAME and password == APP_PASSWORD


@app.before_request
def _require_login():
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return Response(
            "Login required.", 401,
            {"WWW-Authenticate": 'Basic realm="Student Scheduler"'},
        )


DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri"]


# ---------------------------------------------------------------- helpers --

def monday_of(d):
    return d - timedelta(days=d.weekday())


def parse_week_start(value):
    if value:
        try:
            return monday_of(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError:
            pass
    return monday_of(date.today())


def get_schools():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT school FROM students WHERE school != '' "
        "UNION SELECT DISTINCT school FROM work_blocks WHERE school != '' ORDER BY school"
    ).fetchall()
    conn.close()
    return [r["school"] for r in rows]


# --------------------------------------------------------------- students --

@app.route("/")
def index():
    return redirect(url_for("students"))


@app.route("/students")
def students():
    conn = get_db()
    school_filter = request.args.get("school", "").strip()
    min_priority = request.args.get("min_priority", "").strip()
    sort = request.args.get("sort", "priority_desc")

    query = "SELECT * FROM students WHERE active=1"
    params = []
    if school_filter:
        query += " AND school = ?"
        params.append(school_filter)
    if min_priority:
        query += " AND priority >= ?"
        params.append(int(min_priority))

    sort_map = {
        "priority_desc": "priority DESC, minutes_seen ASC",
        "priority_asc": "priority ASC, minutes_seen ASC",
        "name": "name COLLATE NOCASE ASC",
        "school": "school COLLATE NOCASE ASC, name COLLATE NOCASE ASC",
        "minutes_seen": "minutes_seen ASC",
        "minutes_seen_desc": "minutes_seen DESC",
    }
    query += " ORDER BY " + sort_map.get(sort, sort_map["priority_desc"])

    rows = conn.execute(query, params).fetchall()
    student_list = [row_to_student(r) for r in rows]
    schools = get_schools()
    conn.close()

    return render_template(
        "students.html",
        students=student_list,
        schools=schools,
        school_filter=school_filter,
        min_priority=min_priority,
        sort=sort,
        group_size_max=GROUP_SIZE_MAX,
    )


@app.route("/students/add", methods=["POST"])
def add_student():
    name = request.form.get("name", "").strip()
    priority = int(request.form.get("priority", 5))
    school = request.form.get("school", "").strip()
    session_length = int(request.form.get("session_length", 30))
    groupable = 1 if request.form.get("groupable") == "on" else 0
    starts = request.form.getlist("avail_start")
    ends = request.form.getlist("avail_end")

    availability = []
    for s, e in zip(starts, ends):
        if s and e:
            availability.append({"start": s, "end": e})

    if not name or not school:
        flash("Name and school are required.", "error")
        return redirect(url_for("students"))

    priority = max(1, min(10, priority))

    conn = get_db()
    conn.execute(
        """INSERT INTO students (name, priority, school, session_length, minutes_seen, availability, groupable, active)
           VALUES (?, ?, ?, ?, 0, ?, ?, 1)""",
        (name, priority, school, session_length, json.dumps(availability), groupable),
    )
    conn.commit()
    conn.close()
    flash(f"Added {name}.", "success")
    return redirect(url_for("students"))


@app.route("/students/<int:student_id>/edit", methods=["POST"])
def edit_student(student_id):
    name = request.form.get("name", "").strip()
    priority = max(1, min(10, int(request.form.get("priority", 5))))
    school = request.form.get("school", "").strip()
    session_length = int(request.form.get("session_length", 30))
    minutes_seen = int(request.form.get("minutes_seen", 0))
    groupable = 1 if request.form.get("groupable") == "on" else 0
    starts = request.form.getlist("avail_start")
    ends = request.form.getlist("avail_end")

    availability = []
    for s, e in zip(starts, ends):
        if s and e:
            availability.append({"start": s, "end": e})

    conn = get_db()
    conn.execute(
        """UPDATE students SET name=?, priority=?, school=?, session_length=?,
           minutes_seen=?, availability=?, groupable=? WHERE id=?""",
        (name, priority, school, session_length, minutes_seen, json.dumps(availability), groupable, student_id),
    )
    conn.commit()
    conn.close()
    flash(f"Updated {name}.", "success")
    return redirect(url_for("students"))


@app.route("/students/<int:student_id>/delete", methods=["POST"])
def delete_student(student_id):
    conn = get_db()
    conn.execute("UPDATE students SET active=0 WHERE id=?", (student_id,))
    conn.commit()
    conn.close()
    flash("Student removed.", "success")
    return redirect(url_for("students"))


# ------------------------------------------------------------- work blocks --

@app.route("/work-blocks")
def work_blocks():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM work_blocks ORDER BY day_of_week, start_time"
    ).fetchall()
    conn.close()

    by_day = {i: [] for i in range(5)}
    for r in rows:
        by_day[r["day_of_week"]].append(dict(r))

    schools = get_schools()
    return render_template(
        "work_blocks.html", by_day=by_day, day_names=DAY_NAMES, schools=schools
    )


@app.route("/work-blocks/add", methods=["POST"])
def add_work_block():
    day_of_week = int(request.form.get("day_of_week"))
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    school = request.form.get("school", "").strip()

    if not (start_time and end_time and school) or start_time >= end_time:
        flash("Enter a valid time range and school.", "error")
        return redirect(url_for("work_blocks"))

    conn = get_db()
    conn.execute(
        "INSERT INTO work_blocks (day_of_week, start_time, end_time, school) VALUES (?, ?, ?, ?)",
        (day_of_week, start_time, end_time, school),
    )
    conn.commit()
    conn.close()
    flash("Work block added.", "success")
    return redirect(url_for("work_blocks"))


@app.route("/work-blocks/<int:block_id>/delete", methods=["POST"])
def delete_work_block(block_id):
    conn = get_db()
    conn.execute("DELETE FROM work_blocks WHERE id=?", (block_id,))
    conn.commit()
    conn.close()
    flash("Work block removed.", "success")
    return redirect(url_for("work_blocks"))


# ---------------------------------------------------------------- schedule --

def _load_entry_students(conn, entry_id):
    rows = conn.execute(
        """SELECT es.id AS es_id, es.student_id, es.seen, es.minutes_credited,
                  st.name, st.priority, st.minutes_seen, st.session_length, st.school
           FROM entry_students es JOIN students st ON st.id = es.student_id
           WHERE es.entry_id=?
           ORDER BY st.priority DESC, st.name COLLATE NOCASE""",
        (entry_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/schedule")
def schedule():
    week_start = parse_week_start(request.args.get("week"))
    week_start_str = week_start.isoformat()
    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    conn = get_db()
    entries = conn.execute(
        """SELECT * FROM schedule_entries WHERE week_start = ?
           ORDER BY day_of_week, start_time""",
        (week_start_str,),
    ).fetchall()

    all_students = conn.execute(
        "SELECT id, name, school FROM students WHERE active=1 ORDER BY name COLLATE NOCASE"
    ).fetchall()

    by_day = {i: [] for i in range(5)}
    for e in entries:
        entry = dict(e)
        entry["students"] = _load_entry_students(conn, entry["id"])
        by_day[e["day_of_week"]].append(entry)
    conn.close()

    return render_template(
        "schedule.html",
        by_day=by_day,
        day_names=DAY_NAMES,
        week_start=week_start_str,
        prev_week=prev_week,
        next_week=next_week,
        all_students=all_students,
        has_entries=len(entries) > 0,
        group_size_max=GROUP_SIZE_MAX,
    )


@app.route("/schedule/generate", methods=["POST"])
def generate_schedule():
    week_start = request.form.get("week_start")
    generate_schedule_for_week(week_start)
    flash("Schedule generated.", "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/<int:entry_id>/override", methods=["POST"])
def override_entry(entry_id):
    week_start = request.form.get("week_start")
    selected_ids = {int(x) for x in request.form.getlist("student_ids") if x}

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM entry_students WHERE entry_id=?", (entry_id,)
    ).fetchall()
    seen_ids = {r["student_id"] for r in existing if r["seen"]}
    non_seen_ids = {r["student_id"] for r in existing if not r["seen"]}

    room_left = GROUP_SIZE_MAX - len(seen_ids)
    new_selection = list(selected_ids - seen_ids)
    if len(new_selection) > room_left:
        flash(
            f"A session can hold at most {GROUP_SIZE_MAX} students — kept the first "
            f"{room_left} plus anyone already marked seen.",
            "error",
        )
        new_selection = new_selection[:room_left]

    final_non_seen = set(new_selection)
    to_remove = non_seen_ids - final_non_seen
    to_add = final_non_seen - non_seen_ids

    for sid in to_remove:
        conn.execute(
            "DELETE FROM entry_students WHERE entry_id=? AND student_id=?", (entry_id, sid)
        )
    for sid in to_add:
        conn.execute(
            "INSERT INTO entry_students (entry_id, student_id, seen) VALUES (?, ?, 0)",
            (entry_id, sid),
        )

    total = len(seen_ids) + len(final_non_seen)
    conn.execute(
        "UPDATE schedule_entries SET locked=1, is_group=? WHERE id=?",
        (1 if total > 1 else 0, entry_id),
    )
    conn.commit()
    conn.close()
    flash("Slot updated.", "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/<int:entry_id>/attendance", methods=["POST"])
def mark_attendance(entry_id):
    week_start = request.form.get("week_start")

    conn = get_db()
    entry = conn.execute("SELECT * FROM schedule_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        conn.close()
        return redirect(url_for("schedule", week=week_start))

    default_minutes = time_to_min(entry["end_time"]) - time_to_min(entry["start_time"])
    rows = conn.execute(
        "SELECT * FROM entry_students WHERE entry_id=?", (entry_id,)
    ).fetchall()

    for r in rows:
        sid = r["student_id"]
        was_seen = bool(r["seen"])
        prev_minutes = r["minutes_credited"] or 0
        seen = request.form.get(f"seen_{sid}") == "on"
        minutes_str = request.form.get(f"minutes_{sid}", "").strip()
        new_minutes = (int(minutes_str) if minutes_str else default_minutes) if seen else 0

        if was_seen and not seen:
            conn.execute(
                "UPDATE students SET minutes_seen = MAX(0, minutes_seen - ?) WHERE id=?",
                (prev_minutes, sid),
            )
        elif not was_seen and seen:
            conn.execute(
                "UPDATE students SET minutes_seen = minutes_seen + ? WHERE id=?",
                (new_minutes, sid),
            )
        elif was_seen and seen and new_minutes != prev_minutes:
            delta = new_minutes - prev_minutes
            conn.execute(
                "UPDATE students SET minutes_seen = MAX(0, minutes_seen + ?) WHERE id=?",
                (delta, sid),
            )

        conn.execute(
            "UPDATE entry_students SET seen=?, minutes_credited=? WHERE id=?",
            (1 if seen else 0, new_minutes if seen else None, r["id"]),
        )

    conn.execute("UPDATE schedule_entries SET locked=1 WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("schedule", week=week_start))


if __name__ == "__main__":
    app.run(debug=True, port=5050, host="0.0.0.0")
