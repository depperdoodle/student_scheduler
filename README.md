# Rounds — Student Caseload & Schedule

A local web app for managing a student caseload and auto-generating a weekly
schedule based on priority, minutes already seen, availability, and school.

## Running it just for yourself, locally

1. Open this folder in VS Code (`File > Open Folder...`).
2. Open a terminal in VS Code (`` Ctrl+` `` / `` Cmd+` ``).
3. (Recommended) create a virtual environment:
   ```
   python3 -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   ```
4. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
5. Run the app:
   ```
   python app.py
   ```
6. Open **http://localhost:5050** in your browser.
7. When prompted for a login, use `admin` / `changeme` (the built-in defaults —
   see the security note below).

The database (`scheduler.db`, a SQLite file) is created automatically the
first time you run the app, in this same folder. It persists between runs —
back it up if you want a snapshot of a term's data.

## Putting it online so someone else can use it too (e.g. from a Chromebook)

This app needs somewhere it can run continuously with a public web address.
**PythonAnywhere** is a good free option for this: it's beginner-friendly,
free, and — importantly for this app — its files persist, so your
`scheduler.db` won't get wiped out between visits.

1. Create a free account at **pythonanywhere.com**.
2. Go to the **Files** tab, upload `student_scheduler.zip` (this whole
   project, zipped), then open a **Bash console** (Consoles tab) and run:
   ```
   unzip student_scheduler.zip
   cd student_scheduler
   pip install --user -r requirements.txt
   ```
3. Go to the **Web** tab → **Add a new web app** → choose **Manual
   configuration** (not the Flask wizard) → pick the Python version that
   matches what you just installed with.
4. Still on the Web tab, open the **WSGI configuration file** link and
   replace its contents with:
   ```python
   import sys
   path = '/home/YOURUSERNAME/student_scheduler'
   if path not in sys.path:
       sys.path.append(path)
   from app import app as application
   ```
   (swap in your actual PythonAnywhere username).
5. On the same Web tab, find **Environment variables** and add:
   - `APP_USERNAME` — a username for you and your wife to share
   - `APP_PASSWORD` — a real password (please don't leave it as `changeme`)
   - `APP_SECRET_KEY` — any random string
6. Click the big green **Reload** button at the top of the Web tab.
7. Your app is now live at `https://YOURUSERNAME.pythonanywhere.com` — open
   that on the Chromebook, enter the username/password from step 5, and
   it'll prompt to save it like any site login.

To push future changes (like new features Claude adds), re-upload the
updated files the same way and hit Reload again.

### A note on privacy

This app holds real students' names, schools, and priority levels. The
built-in login (HTTP Basic Auth) keeps it from being wide open to anyone
who stumbles on the URL, but it's a simple protection, not enterprise-grade
security — don't reuse a sensitive password for `APP_PASSWORD`, and treat
the URL itself as something to keep private, similar to a shared document
link. If your school district has policies about where student data can be
stored (many do, under FERPA), it's worth checking those before hosting
data like this off a personally-run server.

## How it works

- **Caseload**: add/edit/remove students. Each has a name, priority (1–10),
  school, session length (minutes — can vary per student), lifetime minutes
  seen, whether they're OK to see in a group, and one or more available
  time-of-day windows (applies any weekday). Filter by school/priority and
  sort by any column.
- **Working Hours**: tell it which school you're at and when, for each day
  Mon–Fri. This is a recurring weekly template — edit it any time.
- **Weekly Schedule**: pick a week and click **Generate schedule**. All five
  days are advanced together, one tick at a time, round-robin — so if the
  same students could be seen on more than one day, no single day claims the
  whole shared pool before the others get a turn (this used to cause
  sessions to cluster oddly, e.g. Monday packed and the rest of the week
  empty). Which day gets first turn each round also rotates week to week.
  At every open moment:
  1. First tries to form a **group session**: if 2-3 students marked
     "OK to see in a group" share a school and have overlapping availability
     at that moment, the highest-priority (then least-minutes-seen) ones are
     booked together for a fixed 20-minute session.
  2. If grouping doesn't apply, it falls back to an **individual session**
     sized to that student's own session length, same priority/minutes-seen
     logic as before.
  You can then:
  - **Drag and drop** any student from the Caseload sidebar onto a slot to
    schedule them, drag a student from one slot to another to move them, or
    drag a slot's student back onto the sidebar to unschedule them. A slot
    caps out at 5 students, and a student already marked seen can't be
    dragged away until you unmark them.
  - Prefer clicking? Each slot's **"Adjust roster & attendance"** panel still
    has the checkbox-based roster editor as a precise fallback.
  - **Mark seen** per student in the slot, with an editable minutes count —
    this adds to that student's lifetime minutes-seen total. Unchecking it
    later reverses the credit.
  - **Reset week** clears every slot for that week entirely — including
    manual overrides and anyone marked seen — and automatically reverses any
    minutes that had been credited, so nobody's lifetime total is left
    inflated by a session that got wiped out. This is different from
    **Regenerate**, which leaves locked/seen slots alone and only rebuilds
    the rest.

## Project structure

```
app.py                 Flask routes
db.py                  SQLite schema + connection helper
scheduler.py           The auto-scheduling algorithm
templates/              HTML (Jinja2)
static/style.css        Styling
```
