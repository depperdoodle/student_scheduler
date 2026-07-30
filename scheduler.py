import json
from datetime import datetime

from db import get_db, GROUP_SESSION_LENGTH, GROUP_SIZE_MAX

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

MIN_GROUP_TO_FORM = 2  # need at least this many overlapping groupable students to bother grouping


def time_to_min(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def min_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def window_covers(availability, start, end):
    """True if some availability window fully contains [start, end)."""
    for w in availability:
        if time_to_min(w["start"]) <= start and end <= time_to_min(w["end"]):
            return True
    return False


def _day_steps(conn, week_start, day, day_blocks, students, scheduled_ids, scan_increment):
    """
    A generator that fills one day's blocks (in order), yielding control back
    to the caller after every single tick — one assignment, or one no-match
    scan-increment nudge. Driving several of these round-robin (one `next()`
    each, in turn) makes every day advance through its blocks in lockstep,
    so all five days compete for the same shared, not-yet-scheduled students
    at roughly the same "moment," instead of one day exhausting the pool
    before the others get a turn.
    """
    for block in day_blocks:
        school = block["school"]
        block_start = time_to_min(block["start_time"])
        block_end = time_to_min(block["end_time"])

        remaining = conn.execute(
            """SELECT start_time, end_time FROM schedule_entries
               WHERE week_start=? AND day_of_week=? AND school=?""",
            (week_start, day, school),
        ).fetchall()
        occupied = sorted(
            (time_to_min(r["start_time"]), time_to_min(r["end_time"])) for r in remaining
        )

        def next_occupied_start(after):
            for s, e in occupied:
                if s >= after:
                    return s
            return block_end

        def inside_occupied(t):
            for s, e in occupied:
                if s <= t < e:
                    return e
            return None

        cur = block_start
        while cur < block_end:
            bump = inside_occupied(cur)
            if bump is not None:
                cur = bump
                yield
                continue

            ceiling = min(block_end, next_occupied_start(cur))

            group_candidates = [
                s for s in students
                if s["school"] == school
                and s["groupable"]
                and s["id"] not in scheduled_ids
                and cur + GROUP_SESSION_LENGTH <= ceiling
                and window_covers(s["availability"], cur, cur + GROUP_SESSION_LENGTH)
            ]

            # Auto-grouping only bundles students in the same grade together;
            # mixed-grade groups are still possible, just via manual
            # drag-and-drop/override, not the automatic scheduler. When more
            # than one grade has enough overlapping students at this moment,
            # whichever grade's best-ranked (highest priority, then fewest
            # minutes seen) candidate wins the slot.
            by_grade = {}
            for s in group_candidates:
                by_grade.setdefault(s["grade"], []).append(s)

            grade_pool = None
            best_leader = None
            for grade, bucket in by_grade.items():
                if len(bucket) < MIN_GROUP_TO_FORM:
                    continue
                bucket.sort(key=lambda s: (-s["priority"], s["minutes_seen"]))
                leader = bucket[0]
                if best_leader is None or (leader["priority"], -leader["minutes_seen"]) > (best_leader["priority"], -best_leader["minutes_seen"]):
                    best_leader = leader
                    grade_pool = bucket

            if grade_pool is not None:
                chosen = grade_pool[:GROUP_SIZE_MAX]
                end_slot = cur + GROUP_SESSION_LENGTH
                cur_id = conn.execute(
                    """INSERT INTO schedule_entries
                       (week_start, day_of_week, start_time, end_time, school, is_group, locked)
                       VALUES (?, ?, ?, ?, ?, 1, 0)""",
                    (week_start, day, min_to_time(cur), min_to_time(end_slot), school),
                ).lastrowid
                for s in chosen:
                    conn.execute(
                        "INSERT INTO entry_students (entry_id, student_id, seen) VALUES (?, ?, 0)",
                        (cur_id, s["id"]),
                    )
                    scheduled_ids.add(s["id"])
                occupied.append((cur, end_slot))
                occupied.sort()
                cur = end_slot
                yield
                continue

            individual_candidates = [
                s for s in students
                if s["school"] == school
                and s["id"] not in scheduled_ids
                and cur + s["session_length"] <= ceiling
                and window_covers(s["availability"], cur, cur + s["session_length"])
            ]

            if not individual_candidates:
                cur += scan_increment
                yield
                continue

            best = sorted(individual_candidates, key=lambda s: (-s["priority"], s["minutes_seen"]))[0]
            end_slot = cur + best["session_length"]
            cur_id = conn.execute(
                """INSERT INTO schedule_entries
                   (week_start, day_of_week, start_time, end_time, school, is_group, locked)
                   VALUES (?, ?, ?, ?, ?, 0, 0)""",
                (week_start, day, min_to_time(cur), min_to_time(end_slot), school),
            ).lastrowid
            conn.execute(
                "INSERT INTO entry_students (entry_id, student_id, seen) VALUES (?, ?, 0)",
                (cur_id, best["id"]),
            )
            scheduled_ids.add(best["id"])
            occupied.append((cur, end_slot))
            occupied.sort()
            cur = end_slot
            yield


def generate_schedule_for_week(week_start, scan_increment=5):
    """
    Builds (or rebuilds) the auto-generated portion of a week's schedule.

    All five days are advanced in lockstep, one tick at a time, in
    round-robin order — rather than fully filling Monday's blocks before
    even looking at Tuesday's. That way, when the same students could be
    seen on more than one day, no single day gets to claim the whole shared
    pool before the others get a turn, which is what caused sessions to
    cluster unevenly across the week. Which day gets first turn within each
    round also rotates week to week (based on the ISO week number), so no
    day has a permanent edge over the long run either.

    Within that, grouping is tried first at every open moment: 2-3
    groupable students at the same school AND same grade with overlapping
    availability get a fixed 20-minute group session (mixed-grade groups are
    still possible, but only via manual drag-and-drop/override, never the
    automatic scheduler). If grouping doesn't apply, it falls back to an
    individual session sized to that student's own session_length.

    Entries that are locked (manually overridden/drag-and-dropped) or that
    already have a student marked seen are left untouched; their students
    are treated as already-scheduled for the week, and their time ranges
    are treated as occupied so nothing else gets double-booked into them.
    """
    conn = get_db()

    # Preserve anything locked or with a seen student; clear the rest.
    preserved_ids = {
        r["entry_id"] for r in conn.execute(
            "SELECT DISTINCT entry_id FROM entry_students WHERE seen=1"
        ).fetchall()
    }
    all_week_entries = conn.execute(
        "SELECT id FROM schedule_entries WHERE week_start=?", (week_start,)
    ).fetchall()
    for row in all_week_entries:
        eid = row["id"]
        if eid in preserved_ids:
            continue
        locked = conn.execute(
            "SELECT locked FROM schedule_entries WHERE id=?", (eid,)
        ).fetchone()["locked"]
        if locked:
            continue
        conn.execute("DELETE FROM entry_students WHERE entry_id=?", (eid,))
        conn.execute("DELETE FROM schedule_entries WHERE id=?", (eid,))
    conn.commit()

    blocks = conn.execute(
        "SELECT * FROM work_blocks ORDER BY day_of_week, start_time"
    ).fetchall()
    students = [dict(s) for s in conn.execute("SELECT * FROM students WHERE active=1").fetchall()]
    for s in students:
        s["availability"] = json.loads(s["availability"] or "[]")

    scheduled_ids = {
        r["student_id"] for r in conn.execute(
            """SELECT DISTINCT es.student_id FROM entry_students es
               JOIN schedule_entries se ON se.id = es.entry_id
               WHERE se.week_start=?""",
            (week_start,),
        ).fetchall()
    }

    blocks_by_day = {i: [] for i in range(5)}
    for b in blocks:
        blocks_by_day[b["day_of_week"]].append(b)

    try:
        iso_week = datetime.strptime(week_start, "%Y-%m-%d").isocalendar()[1]
    except ValueError:
        iso_week = 0
    offset = iso_week % 5
    day_order = [(offset + i) % 5 for i in range(5)]

    generators = {}
    for day in day_order:
        if blocks_by_day[day]:
            generators[day] = _day_steps(
                conn, week_start, day, blocks_by_day[day], students, scheduled_ids, scan_increment
            )

    active_days = [d for d in day_order if d in generators]
    while active_days:
        for day in list(active_days):
            try:
                next(generators[day])
            except StopIteration:
                active_days.remove(day)

    conn.commit()
    conn.close()
