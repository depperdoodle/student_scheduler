import json
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify

from db import get_db, init_db, row_to_student, GROUP_SIZE_MAX, GRADE_OPTIONS
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
    grade_filter = request.args.get("grade", "").strip()
    sort = request.args.get("sort", "priority_desc")

    query = "SELECT * FROM students WHERE active=1"
    params = []
    if school_filter:
        query += " AND school = ?"
        params.append(school_filter)
    if min_priority:
        query += " AND priority >= ?"
        params.append(int(min_priority))
    if grade_filter:
        query += " AND grade = ?"
        params.append(grade_filter)

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
        grade_filter=grade_filter,
        grade_options=GRADE_OPTIONS,
        sort=sort,
        group_size_max=GROUP_SIZE_MAX,
    )


@app.route("/students/add", methods=["POST"])
def add_student():
    name = request.form.get("name", "").strip()
    priority = int(request.form.get("priority", 5))
    school = request.form.get("school", "").strip()
    grade = request.form.get("grade", "").strip()
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

    if grade not in GRADE_OPTIONS:
        flash("Pick a valid grade level.", "error")
        return redirect(url_for("students"))

    priority = max(1, min(10, priority))

    conn = get_db()
    conn.execute(
        """INSERT INTO students (name, priority, school, grade, session_length, minutes_seen, availability, groupable, active)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1)""",
        (name, priority, school, grade, session_length, json.dumps(availability), groupable),
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
    grade = request.form.get("grade", "").strip()
    session_length = int(request.form.get("session_length", 30))
    minutes_seen = int(request.form.get("minutes_seen", 0))
    groupable = 1 if request.form.get("groupable") == "on" else 0
    starts = request.form.getlist("avail_start")
    ends = request.form.getlist("avail_end")

    availability = []
    for s, e in zip(starts, ends):
        if s and e:
            availability.append({"start": s, "end": e})

    if grade not in GRADE_OPTIONS:
        flash("Pick a valid grade level.", "error")
        return redirect(url_for("students"))

    conn = get_db()
    conn.execute(
        """UPDATE students SET name=?, priority=?, school=?, grade=?, session_length=?,
           minutes_seen=?, availability=?, groupable=? WHERE id=?""",
        (name, priority, school, grade, session_length, minutes_seen, json.dumps(availability), groupable, student_id),
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
                  st.name, st.priority, st.minutes_seen, st.session_length, st.school, st.grade
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
        """SELECT id, name, school, grade, priority, groupable FROM students
           WHERE active=1 ORDER BY priority DESC, name COLLATE NOCASE"""
    ).fetchall()

    week_count_rows = conn.execute(
        """SELECT es.student_id, COUNT(*) AS c FROM entry_students es
           JOIN schedule_entries se ON se.id = es.entry_id
           WHERE se.week_start=?
           GROUP BY es.student_id""",
        (week_start_str,),
    ).fetchall()
    week_counts = {r["student_id"]: r["c"] for r in week_count_rows}

    events = conn.execute(
        """SELECT * FROM event_blocks WHERE week_start = ?
           ORDER BY day_of_week, start_time""",
        (week_start_str,),
    ).fetchall()

    by_day = {i: [] for i in range(5)}
    for e in entries:
        entry = dict(e)
        entry["students"] = _load_entry_students(conn, entry["id"])
        by_day[e["day_of_week"]].append({"kind": "session", "start_time": entry["start_time"], "data": entry})
    for ev in events:
        event = dict(ev)
        by_day[ev["day_of_week"]].append({"kind": "event", "start_time": event["start_time"], "data": event})
    for day in by_day:
        by_day[day].sort(key=lambda item: item["start_time"])
    conn.close()

    return render_template(
        "schedule.html",
        by_day=by_day,
        day_names=DAY_NAMES,
        week_start=week_start_str,
        prev_week=prev_week,
        next_week=next_week,
        all_students=all_students,
        week_counts=week_counts,
        has_entries=len(entries) > 0,
        group_size_max=GROUP_SIZE_MAX,
    )


@app.route("/schedule/event/add", methods=["POST"])
def add_event_block():
    week_start = request.form.get("week_start")
    day_of_week = int(request.form.get("day_of_week"))
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    label = request.form.get("label", "").strip()

    if not (start_time and end_time and label) or start_time >= end_time:
        flash("Enter a label and a valid time range for the event.", "error")
        return redirect(url_for("schedule", week=week_start))

    conn = get_db()
    conn.execute(
        """INSERT INTO event_blocks (week_start, day_of_week, start_time, end_time, label)
           VALUES (?, ?, ?, ?, ?)""",
        (week_start, day_of_week, start_time, end_time, label),
    )
    conn.commit()
    conn.close()
    flash(f'Added "{label}".', "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/event/<int:event_id>/delete", methods=["POST"])
def delete_event_block(event_id):
    week_start = request.form.get("week_start")
    conn = get_db()
    conn.execute("DELETE FROM event_blocks WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    flash("Event removed.", "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/generate", methods=["POST"])
def generate_schedule():
    week_start = request.form.get("week_start")
    generate_schedule_for_week(week_start)
    flash("Schedule generated.", "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/reset", methods=["POST"])
def reset_schedule():
    week_start = request.form.get("week_start")

    conn = get_db()
    seen_rows = conn.execute(
        """SELECT es.student_id, es.minutes_credited FROM entry_students es
           JOIN schedule_entries se ON se.id = es.entry_id
           WHERE se.week_start=? AND es.seen=1""",
        (week_start,),
    ).fetchall()
    for r in seen_rows:
        credited = r["minutes_credited"] or 0
        if credited:
            conn.execute(
                "UPDATE students SET minutes_seen = MAX(0, minutes_seen - ?) WHERE id=?",
                (credited, r["student_id"]),
            )

    entry_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM schedule_entries WHERE week_start=?", (week_start,)
        ).fetchall()
    ]
    for eid in entry_ids:
        conn.execute("DELETE FROM entry_students WHERE entry_id=?", (eid,))
    conn.execute("DELETE FROM schedule_entries WHERE week_start=?", (week_start,))
    conn.commit()
    conn.close()
    flash("Week reset — every slot cleared, and any credited minutes were reversed.", "success")
    return redirect(url_for("schedule", week=week_start))


@app.route("/schedule/<int:entry_id>/card")
def entry_card_fragment(entry_id):
    week_start = request.args.get("week")
    conn = get_db()
    e = conn.execute("SELECT * FROM schedule_entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        conn.close()
        return "", 404
    entry = dict(e)
    entry["students"] = _load_entry_students(conn, entry_id)
    all_students = conn.execute(
        """SELECT id, name, school, grade, priority, groupable FROM students
           WHERE active=1 ORDER BY priority DESC, name COLLATE NOCASE"""
    ).fetchall()
    conn.close()
    return render_template(
        "_entry_card.html",
        e=entry,
        week_start=week_start,
        all_students=all_students,
        group_size_max=GROUP_SIZE_MAX,
    )


@app.route("/api/schedule/<int:entry_id>/add", methods=["POST"])
def api_add_student(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        student_id = int(data.get("student_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="Missing student."), 400

    conn = get_db()
    entry = conn.execute("SELECT * FROM schedule_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        conn.close()
        return jsonify(ok=False, message="That slot no longer exists."), 404

    existing = conn.execute(
        "SELECT * FROM entry_students WHERE entry_id=?", (entry_id,)
    ).fetchall()
    if any(r["student_id"] == student_id for r in existing):
        conn.close()
        return jsonify(ok=True)

    if len(existing) >= GROUP_SIZE_MAX:
        conn.close()
        return jsonify(ok=False, message=f"This slot already has the max of {GROUP_SIZE_MAX} students.")

    conn.execute(
        "INSERT INTO entry_students (entry_id, student_id, seen) VALUES (?, ?, 0)",
        (entry_id, student_id),
    )
    total = len(existing) + 1
    conn.execute(
        "UPDATE schedule_entries SET locked=1, is_group=? WHERE id=?",
        (1 if total > 1 else 0, entry_id),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/schedule/<int:entry_id>/remove", methods=["POST"])
def api_remove_student(entry_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        student_id = int(data.get("student_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="Missing student."), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM entry_students WHERE entry_id=? AND student_id=?",
        (entry_id, student_id),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=True)
    if row["seen"]:
        conn.close()
        return jsonify(
            ok=False,
            message="This student is already marked seen in this slot — unmark attendance first.",
        )

    conn.execute("DELETE FROM entry_students WHERE id=?", (row["id"],))
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM entry_students WHERE entry_id=?", (entry_id,)
    ).fetchone()["c"]
    conn.execute(
        "UPDATE schedule_entries SET locked=1, is_group=? WHERE id=?",
        (1 if remaining > 1 else 0, entry_id),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


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
