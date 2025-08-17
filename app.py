import sqlite3, os, datetime, json
from flask import Flask, request, jsonify
from flask_cors import CORS

DB_PATH = os.environ.get("ROOMATE_DB", "roomate.db")

app = Flask(__name__)
CORS(app)

def now_iso():
    return datetime.datetime.utcnow().isoformat()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS households(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS members(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT,
        join_order INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS chores(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        cadence TEXT NOT NULL DEFAULT 'weekly', -- future-friendly
        created_at TEXT NOT NULL,
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
    );

    -- A “rotation week” is identified by the Monday date (ISO YYYY-MM-DD)
    CREATE TABLE IF NOT EXISTS assignments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_id INTEGER NOT NULL,
        week_start TEXT NOT NULL,
        chore_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'assigned', -- assigned | done
        proof_url TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(household_id, week_start, chore_id),
        FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
        FOREIGN KEY (chore_id) REFERENCES chores(id) ON DELETE CASCADE,
        FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()

def monday_of_week(d=None):
    d = d or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday()))

def iso_date(d: datetime.date) -> str:
    return d.isoformat()

def pick_member_round_robin(conn, household_id, week_start):
    """
    Round-robin across members based on historical assignments.
    We compute each member's total # of past assignments and use rotating order.
    """
    cur = conn.cursor()
    # who’s in the house
    cur.execute("SELECT id, name, join_order FROM members WHERE household_id=? ORDER BY join_order ASC, id ASC", (household_id,))
    members = cur.fetchall()
    if not members:
        return []

    # counts to rebalance
    cur.execute("""
        SELECT member_id, COUNT(*) as cnt
        FROM assignments
        WHERE household_id=? AND week_start < ?
        GROUP BY member_id
    """, (household_id, week_start))
    hist = {row["member_id"]: row["cnt"] for row in cur.fetchall()}
    scored = []
    for m in members:
        scored.append((hist.get(m["id"], 0), m["join_order"], m["id"]))
    # Sort by (fewest assigned so far, then original join order for stability)
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    # Return member ids in fair order
    return [m_id for _,__,m_id in scored]

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": now_iso()})

# ----- Households
@app.route("/api/households", methods=["POST"])
def create_household():
    data = request.json or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "name is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO households(name, created_at) VALUES(?,?)", (name, now_iso()))
    hid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": hid, "name": name})

@app.route("/api/households/<int:hid>", methods=["GET"])
def get_household(hid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM households WHERE id=?", (hid,))
    h = cur.fetchone()
    if not h:
        return jsonify({"error": "not found"}), 404
    # include members, chores
    cur.execute("SELECT * FROM members WHERE household_id=? ORDER BY join_order ASC, id ASC", (hid,))
    members = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM chores WHERE household_id=? ORDER BY id DESC", (hid,))
    chores = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify({"household": dict(h), "members": members, "chores": chores})

# ----- Members
@app.route("/api/households/<int:hid>/members", methods=["POST"])
def add_member(hid):
    data = request.json or {}
    name = data.get("name")
    email = data.get("email")
    if not name:
        return jsonify({"error": "name is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    # join_order = next integer
    cur.execute("SELECT COALESCE(MAX(join_order), 0)+1 FROM members WHERE household_id=?", (hid,))
    join_order = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO members(household_id, name, email, join_order, created_at) VALUES(?,?,?,?,?)",
        (hid, name, email, join_order, now_iso())
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": mid, "name": name, "email": email, "join_order": join_order})

# ----- Chores
@app.route("/api/households/<int:hid>/chores", methods=["POST"])
def add_chore(hid):
    data = request.json or {}
    title = data.get("title")
    cadence = data.get("cadence", "weekly")
    if not title:
        return jsonify({"error": "title is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chores(household_id, title, cadence, created_at) VALUES(?,?,?,?)",
        (hid, title, cadence, now_iso())
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": cid, "title": title, "cadence": cadence})

# ----- Rotation / Assignments
@app.route("/api/households/<int:hid>/assignments/current", methods=["GET"])
def get_current_assignments(hid):
    week_start = request.args.get("week_start")
    if week_start is None:
        week_start = iso_date(monday_of_week())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.*, c.title AS chore_title, m.name AS member_name
        FROM assignments a
        JOIN chores c ON c.id=a.chore_id
        JOIN members m ON m.id=a.member_id
        WHERE a.household_id=? AND a.week_start=?
        ORDER BY a.id ASC
    """, (hid, week_start))
    out = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify({"week_start": week_start, "assignments": out})

@app.route("/api/households/<int:hid>/rotate", methods=["POST"])
def rotate(hid):
    """
    Generate assignments for the requested week (or this week) if not already present.
    Fair round-robin based on historical totals.
    """
    data = request.json or {}
    week_start = data.get("week_start") or iso_date(monday_of_week())
    conn = get_db()
    cur = conn.cursor()

    # If already assigned, just return them
    cur.execute("SELECT COUNT(*) FROM assignments WHERE household_id=? AND week_start=?", (hid, week_start))
    if cur.fetchone()[0] > 0:
        conn.close()
        return get_current_assignments(hid)

    # fetch chores & members
    cur.execute("SELECT id FROM chores WHERE household_id=? ORDER BY id ASC", (hid,))
    chores = [row["id"] for row in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM members WHERE household_id=?", (hid,))
    member_count = cur.fetchone()[0]
    if not chores or member_count == 0:
        conn.close()
        return jsonify({"error": "Need at least 1 chore and 1 member"}), 400

    order = pick_member_round_robin(conn, hid, week_start)
    if not order:
        conn.close()
        return jsonify({"error": "No members"}), 400

    # assign chores in a cycling manner over the fair order
    i = 0
    created = []
    for chore_id in chores:
        member_id = order[i % len(order)]
        i += 1
        cur.execute("""
            INSERT INTO assignments(household_id, week_start, chore_id, member_id, status, created_at)
            VALUES(?,?,?,?,?,?)
        """, (hid, week_start, chore_id, member_id, "assigned", now_iso()))
        created.append(cur.lastrowid)

    conn.commit()
    conn.close()
    return get_current_assignments(hid)

@app.route("/api/assignments/<int:aid>/complete", methods=["POST"])
def complete_assignment(aid):
    data = request.form if request.form else (request.json or {})
    proof_url = data.get("proof_url")  # keep it simple (paste image link)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE assignments SET status='done', proof_url=? WHERE id=?", (proof_url, aid))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "not found"}), 404
    conn.commit()
    conn.close()
    return jsonify({"id": aid, "status": "done", "proof_url": proof_url})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)