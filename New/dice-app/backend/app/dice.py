from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os, pathlib, sqlite3, random, json
from datetime import datetime, timezone, date
from fastapi import Request 
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────
# Env & app
load_dotenv()
app = FastAPI(title="Dice Game API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # dev only; tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# Paths (project root = .../dice-app/)
BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "backend" / "storage" / "dice.db"

# after DB_PATH = BASE_DIR / "backend" / "storage" / "dice.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS users (
          telegram_id INTEGER PRIMARY KEY,
          username TEXT,
          first_name TEXT,
          last_name TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS wallets (
          telegram_id INTEGER PRIMARY KEY,
          address TEXT NOT NULL,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rolls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL,
          date_utc TEXT NOT NULL,        -- 'YYYY-MM-DD'
          roll_index INTEGER NOT NULL,   -- 1..50
          d1 INTEGER NOT NULL,
          d2 INTEGER NOT NULL,
          total INTEGER NOT NULL,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(telegram_id, date_utc, roll_index)
        );

        CREATE TABLE IF NOT EXISTS daily_totals (
          telegram_id INTEGER NOT NULL,
          date_utc TEXT NOT NULL,
          total_score INTEGER NOT NULL DEFAULT 0,
          rolls_count INTEGER NOT NULL DEFAULT 0,
          finalized_at DATETIME,
          PRIMARY KEY (telegram_id, date_utc)
        );

        CREATE TABLE IF NOT EXISTS weekly_totals (
          telegram_id INTEGER NOT NULL,
          week_id TEXT NOT NULL,         -- e.g. '2025-W34'
          total_score INTEGER NOT NULL DEFAULT 0,
          days_played INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (telegram_id, week_id)
        );
        -- Indexes (safe to re-run)
        CREATE INDEX IF NOT EXISTS idx_rolls_user_day 
          ON rolls(telegram_id, date_utc);
        CREATE INDEX IF NOT EXISTS idx_rolls_user_time 
          ON rolls(telegram_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_daily_date_score 
          ON daily_totals(date_utc, total_score DESC);
        CREATE INDEX IF NOT EXISTS idx_week_week_score 
          ON weekly_totals(week_id, total_score DESC);
                CREATE TABLE IF NOT EXISTS roll_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL,
          key TEXT NOT NULL,
          response_json TEXT NOT NULL,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(telegram_id, key)
        );
        CREATE INDEX IF NOT EXISTS idx_rollreq_user_time ON roll_requests(telegram_id, created_at);
        """)
        conn.commit()


# call it once at import time
init_db()

# static + web
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/web",    StaticFiles(directory=BASE_DIR / "web"),    name="web")

@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "web" / "index.html")
from fastapi import HTTPException

@app.get("/leaderboard")
def serve_leaderboard():
    path = BASE_DIR / "web" / "leaderboard.html"
    if not path.exists():
        # helpful error if the file isn't where we expect
        raise HTTPException(status_code=404, detail=f"leaderboard.html not found at {path}")
    return FileResponse(path)


# ─────────────────────────────────────────────
# Helpers & game config
TEST_USER_ID = 12345            # local-only; replace with Telegram auth later
MAX_DAILY = 50
COOLDOWN_S = 4                  # strict cooldown

def today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def week_id(dt: date | None = None) -> str:
    d = dt or datetime.now(timezone.utc).date()
    year, wk, _ = d.isocalendar()
    return f"{year}-W{wk:02d}"

def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=10)

def rolls_used_today(user_id: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM rolls WHERE telegram_id=? AND date_utc=?",
            (user_id, today_utc_str()),
        )
        return int(cur.fetchone()[0])

def seconds_since_last_roll(user_id: int) -> float:
    with db() as conn:
        cur = conn.execute(
            "SELECT strftime('%s','now') - strftime('%s', MAX(created_at)) "
            "FROM rolls WHERE telegram_id=?", (user_id,)
        )
        val = cur.fetchone()[0]
        try:
            return float(val if val is not None else 10_000.0)
        except Exception:
            return 10_000.0

def upsert_daily_and_weekly(user_id: int, add_total: int):
    tday = today_utc_str()
    wk = week_id()
    with db() as conn:
        conn.execute("""
            INSERT INTO daily_totals(telegram_id, date_utc, total_score, rolls_count)
            VALUES(?,?,?,1)
            ON CONFLICT(telegram_id, date_utc) DO UPDATE SET
              total_score = total_score + excluded.total_score,
              rolls_count = rolls_count + 1
        """, (user_id, tday, add_total))
        conn.execute("""
            INSERT INTO weekly_totals(telegram_id, week_id, total_score, days_played)
            VALUES(?,?,?,0)
            ON CONFLICT(telegram_id, week_id) DO UPDATE SET
              total_score = total_score + excluded.total_score
        """, (user_id, wk, add_total))
        conn.commit()

def json_error(status: int, code: str, **extra):
    return JSONResponse(status_code=status, content={"error": code, **extra})

def get_idempo(conn: sqlite3.Connection, user_id: int, key: str):
    cur = conn.execute(
        "SELECT response_json FROM roll_requests WHERE telegram_id=? AND key=?",
        (user_id, key)
    )
    row = cur.fetchone()
    return json.loads(row[0]) if row else None

def save_idempo(conn: sqlite3.Connection, user_id: int, key: str, resp: dict):
    conn.execute(
        "INSERT OR IGNORE INTO roll_requests(telegram_id, key, response_json) VALUES (?,?,?)",
        (user_id, key, json.dumps(resp, separators=(',', ':')))
    )


# ─────────────────────────────────────────────
# API

@app.get("/config")
def get_config():
    used = rolls_used_today(TEST_USER_ID)
    return {
        "rolls_left": max(0, MAX_DAILY - used),
        "cooldown": COOLDOWN_S,
        "daily_limit": MAX_DAILY,
        "user": {"telegram_id": TEST_USER_ID}
    }

@app.post("/roll")
def roll_dice(request: Request):
    user_id = TEST_USER_ID  # (dev mode); later we’ll swap to real Telegram auth
    idem_key = request.headers.get("X-Idempotency-Key")

    # If an idempotency key is supplied and we have a stored response, return it
    if idem_key:
        with db() as conn:
            prev = get_idempo(conn, user_id, idem_key)
            if prev:
                return prev

    # cooldown
    since = seconds_since_last_roll(user_id)
    if since < COOLDOWN_S:
        return json_error(429, "COOLDOWN_ACTIVE", seconds_remaining=round(COOLDOWN_S - since, 1))

    # limit
    used = rolls_used_today(user_id)
    if used >= MAX_DAILY:
        return json_error(400, "DAILY_LIMIT_REACHED")

    # server-side dice
    d1 = random.randint(1,6); d2 = random.randint(1,6)
    total = d1 + d2
    idx = used + 1
    tday = today_utc_str()

    with db() as conn:
        conn.execute("""
            INSERT INTO rolls(telegram_id, date_utc, roll_index, d1, d2, total, created_at)
            VALUES(?,?,?,?,?,?,datetime('now'))
        """, (user_id, tday, idx, d1, d2, total))
        conn.commit()

        upsert_daily_and_weekly(user_id, total)

        resp = {
            "d1": d1, "d2": d2, "total": total,
            "roll_index": idx,
            "rolls_left": MAX_DAILY - idx
        }

        # Save idempotent response (no-op if key missing)
        if idem_key:
            save_idempo(conn, user_id, idem_key, resp)
            conn.commit()

    return resp


@app.get("/leaderboard/daily")
def daily_leaderboard(limit: int = 20):
    tday = today_utc_str()
    with db() as conn:
        top = conn.execute("""
            SELECT telegram_id, total_score FROM daily_totals
            WHERE date_utc=? ORDER BY total_score DESC, telegram_id ASC
            LIMIT ?
        """, (tday, limit)).fetchall()
        rows = conn.execute("""
            SELECT telegram_id, total_score FROM daily_totals
            WHERE date_utc=? ORDER BY total_score DESC, telegram_id ASC
        """, (tday,)).fetchall()
    leaderboard = [{"rank": i+1, "user": str(uid), "score": sc} for i,(uid,sc) in enumerate(top)]
    your_rank = next((i+1 for i,(uid,_) in enumerate(rows) if uid==TEST_USER_ID), None)
    your_score = next((sc for uid,sc in rows if uid==TEST_USER_ID), 0)
    return {"date": tday, "leaderboard": leaderboard, "your_rank": your_rank, "your_score": your_score}

@app.get("/leaderboard/weekly")
def weekly_leaderboard(limit: int = 20):
    wk = week_id()
    with db() as conn:
        top = conn.execute("""
            SELECT telegram_id, total_score FROM weekly_totals
            WHERE week_id=? ORDER BY total_score DESC, telegram_id ASC
            LIMIT ?
        """, (wk, limit)).fetchall()
        rows = conn.execute("""
            SELECT telegram_id, total_score FROM weekly_totals
            WHERE week_id=? ORDER BY total_score DESC, telegram_id ASC
        """, (wk,)).fetchall()
    leaderboard = [{"rank": i+1, "user": str(uid), "score": sc} for i,(uid,sc) in enumerate(top)]
    your_rank = next((i+1 for i,(uid,_) in enumerate(rows) if uid==TEST_USER_ID), None)
    your_score = next((sc for uid,sc in rows if uid==TEST_USER_ID), 0)
    return {"week_id": wk, "leaderboard": leaderboard, "your_rank": your_rank, "your_score": your_score}
