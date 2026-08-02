import json
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


def generate_schedule_for_week(week_start, scan_increment=5):
    """
    Builds (or rebuilds) the auto-generated portion of a week's schedule.

    Each work block is scanned minute-by-minute (in scan_increment steps).
    At every open moment, grouping is tried FIRST: if two or more active,
    groupable students at that school have availability covering a fixed
    GROUP_SESSION_LENGTH-minute window, the highest-priority (then
    least-minutes-seen) ones -- up to GROUP_SIZE_MAX -- are booked together
    as one group session. If grouping doesn't apply, the slot falls back to
    an individual session sized to that student's own session_length.

    Entries that are locked (manually overridden) or that already have a
    student marked seen are left untouched; their students are treated as
    already-scheduled for the week, and their time ranges are treated as
    occupied so nothing else gets double-booked into them.
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

    for block in blocks:
        day = block["day_of_week"]
        school = block["school"]
        block_start = time_to_min(block["start_time"])
        block_end = time_to_min(block["end_time"])

        # Occupied (preserved) ranges within this exact day/school block.
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

            if len(group_candidates) >= MIN_GROUP_TO_FORM:
                group_candidates.sort(key=lambda s: (-s["priority"], s["minutes_seen"]))
                chosen = group_candidates[:GROUP_SIZE_MAX]
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

    conn.commit()
    conn.close()
