import os, time, json, random, asyncio, sqlite3, requests
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

async def synccollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Starting full sync… This can take a few minutes. I'll update the console with progress.")
    before = collections_count()

    # Do the heavy fetch off the main event loop
    def _run():
        rows = fetch_all_collections_full(progress=_progress_line)
        _progress_done("✅ Fetch complete. Upserting to DB…")
        collections_upsert(rows)
        # Optional: auto-track in small batches to be gentle on API
        batch = 200
        ids = [cid for cid, _ in rows]
        for i in range(0, len(ids), batch):
            add_to_tracked(ids[i:i+batch])
            _progress_line(f"🧭 Tracked {min(i+batch, len(ids))}/{len(ids)}")
        _progress_done("✅ Tracking done.")
        sync_json_from_db_if_needed()
        return len(rows)

    total_rows = await asyncio.to_thread(_run)
    after = collections_count()
    added = max(0, after - before)
    await update.message.reply_text(
        f"Done! Upserted {total_rows} rows.\n"
        f"DB now has {after} collections (added/updated: ~{added})."
    )

# ── Console progress helpers + seeding flag
IS_SEEDING_COLLECTIONS = False

def _progress_line(text: str):
    # single-line live update in console (Windows-friendly)
    print("\r" + text[:120].ljust(120), end="", flush=True)

def _progress_done(text: str = ""):
    if text:
        print("\r" + text)
    else:
        print()



# ─────────────────────────────────────────────
# In-memory state
USER_ADDRESS: dict[int, str] = {}   # set in /connect
USER_COLLECTION: dict[int, str] = {}
AWAITING_FIND_FLAG = "awaiting_find_term"

# Paging config
PAGE_SIZE = 8
OWNED_PAGE_SIZE = 10
PROGRESS_PAGE_SIZE = 20

# TokenId cache (to speed up progress navigation)
TOKEN_CACHE: dict[str, dict] = {}  # {cid: {"ids":[...], "ts": float}}

# Paths
DATA_DIR = Path("data"); DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = DATA_DIR / "state.json"
COLLECTION_DB = DATA_DIR / "collection.db"   # <-- per your ask
ROLLS_DB      = DATA_DIR / "rolls.db"
COLLECTIONS_JSON = Path("collections.json")  # optional backup/export

# ─────────────────────────────────────────────
# Config
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENJIN_API = os.getenv("ENJIN_GRAPHQL", "https://platform.enjin.io/graphql")
ENJIN_API_KEY = os.getenv("ENJIN_API_KEY")
if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
if not ENJIN_API_KEY:
    raise SystemExit("Missing ENJIN_API_KEY in .env")

# If your key requires "Bearer ", set this True:
USE_BEARER = False

def gql_headers():
    if USE_BEARER:
        tok = ENJIN_API_KEY if ENJIN_API_KEY.startswith("Bearer ") else f"Bearer {ENJIN_API_KEY}"
    else:
        tok = ENJIN_API_KEY
    return {"Authorization": tok, "Content-Type": "application/json"}

# ─────────────────────────────────────────────
# State load/save
STATE: dict = {"users": {}}

def load_state():
    global STATE
    if STATE_PATH.exists():
        try:
            STATE = json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            STATE = {"users": {}}
    else:
        STATE = {"users": {}}
    for k, v in STATE.get("users", {}).items():
        try:
            uid = int(k)
        except ValueError:
            continue
        if v.get("address"):
            USER_ADDRESS[uid] = v["address"]
        if v.get("collection"):
            USER_COLLECTION[uid] = v["collection"]

def save_state():
    try:
        STATE_PATH.write_text(json.dumps(STATE, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass

def user_state(uid: int) -> dict:
    u = STATE.setdefault("users", {}).setdefault(str(uid), {})
    u.setdefault("address", USER_ADDRESS.get(uid))
    u.setdefault("collection", USER_COLLECTION.get(uid))
    u.setdefault("last_view", None)
    return u

load_state()

# ─────────────────────────────────────────────
# Reply helpers
MAX_CHUNK = 3500

async def safe_reply(update: Update, text: str, reply_markup=None):
    if getattr(update, "message", None) is None:
        if getattr(update, "callback_query", None):
            # ensure we don't send "not modified" on identical content
            try:
                await update.callback_query.edit_message_text(text[:4096], reply_markup=reply_markup)
            except Exception:
                # fall back to sending a new message
                await update.callback_query.message.reply_text(text[:4096], reply_markup=reply_markup)
        return
    if len(text) <= MAX_CHUNK:
        return await update.message.reply_text(text, reply_markup=reply_markup)
    lines = text.split("\n")
    buf, cur = [], 0
    first = True
    for ln in lines:
        if cur + len(ln) + 1 > MAX_CHUNK:
            await update.message.reply_text("\n".join(buf), reply_markup=(reply_markup if first else None))
            first = False; buf, cur = [], 0
        buf.append(ln); cur += len(ln) + 1
    if buf:
        await update.message.reply_text("\n".join(buf))

async def edit_or_send(update: Update, text: str, reply_markup=None):
    if getattr(update, "callback_query", None):
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            return
        except Exception:
            pass
    await safe_reply(update, text, reply_markup=reply_markup)

# ─────────────────────────────────────────────
# SQLite — collection.db & rolls.db
def get_conn(path: Path):
    return sqlite3.connect(path)

def init_collection_db():
    conn = get_conn(COLLECTION_DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS collections (
        id   TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at INTEGER DEFAULT (strftime('%s','now')),
        updated_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_col_name ON collections(name)")
    conn.commit(); conn.close()

def collections_upsert(rows: list[tuple[str, str]]):
    if not rows: return
    now = int(time.time())
    conn = get_conn(COLLECTION_DB)
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO collections(id, name, created_at, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,
          updated_at=excluded.updated_at
    """, [(str(cid), (nm or f"Collection {cid}"), now, now) for cid, nm in rows])
    conn.commit(); conn.close()

def collections_bulk_insert_ids(ids: list[str]):
    if not ids: return
    now = int(time.time())
    conn = get_conn(COLLECTION_DB); cur = conn.cursor()
    cur.executemany("""
        INSERT OR IGNORE INTO collections(id, name, created_at, updated_at)
        VALUES(?,?,?,?)
    """, [(str(cid), f"Collection {cid}", now, now) for cid in ids])
    conn.commit(); conn.close()

def collections_get_name(cid: str) -> str | None:
    conn = get_conn(COLLECTION_DB); cur = conn.cursor()
    cur.execute("SELECT name FROM collections WHERE id=?", (str(cid),))
    row = cur.fetchone(); conn.close()
    return row[0] if row else None

def collections_search(term: str, limit: int = 400) -> list[tuple[str, str]]:
    like = f"%{term}%"
    conn = get_conn(COLLECTION_DB); cur = conn.cursor()
    cur.execute("""
        SELECT id, name FROM collections
        WHERE LOWER(name) LIKE LOWER(?)
        ORDER BY name ASC
        LIMIT ?
    """, (like, limit))
    rows = cur.fetchall(); conn.close()
    return [(r[0], r[1]) for r in rows]

def collections_all_ids() -> list[str]:
    conn = get_conn(COLLECTION_DB); cur = conn.cursor()
    cur.execute("SELECT id FROM collections")
    out = [r[0] for r in cur.fetchall()]
    conn.close()
    return out

# rolls.db
def init_rolls_db():
    conn = get_conn(ROLLS_DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rolls (
        user_id INTEGER,
        username TEXT,
        wallet TEXT,
        roll INTEGER,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cooldowns (
        user_id INTEGER PRIMARY KEY,
        next_time REAL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        wallet TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit(); conn.close()

init_collection_db()
init_rolls_db()

# JSON backup helpers
def load_collections_json() -> list[dict]:
    try:
        return json.loads(COLLECTIONS_JSON.read_text("utf-8"))
    except Exception:
        return []

def save_collections_json(entries: list[dict]):
    try:
        COLLECTIONS_JSON.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def sync_json_from_db_if_needed():
    db_ids = set(collections_all_ids())
    file_entries = load_collections_json()
    file_ids = {e["id"] for e in file_entries if "id" in e}
    if db_ids - file_ids:
        conn = get_conn(COLLECTION_DB); cur = conn.cursor()
        cur.execute("SELECT id, name FROM collections ORDER BY name ASC")
        entries = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        conn.close()
        save_collections_json(entries)

# ─────────────────────────────────────────────
# Enjin GraphQL helpers
def enjin_graphql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(ENJIN_API, json={"query": query, "variables": variables or {}}, headers=gql_headers(), timeout=30)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(str(body["errors"]))
    return body["data"]

def attr(attrs, key):
    for a in attrs or []:
        if (a.get("key") or "").lower() == key.lower():
            return a.get("value")
    return None

def resolve_name_from_uri(uri):
    if not uri:
        return ""
    for attempt in range(4):
        try:
            r = requests.get(uri, headers={"Accept":"application/json","User-Agent":"ECT/1.0"}, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.2 * (attempt + 1))
                continue
            if not r.ok:
                return ""
            data = r.json()
            name = data.get("name")
            if isinstance(name, str):
                return name.strip()
            attrs = data.get("attributes")
            if isinstance(attrs, dict):
                v = attrs.get("name")
                if isinstance(v, dict):
                    vv = v.get("value")
                    if isinstance(vv, str):
                        return vv.strip()
            if isinstance(attrs, list):
                for item in attrs:
                    if item.get("key") == "name" and isinstance(item.get("value"), str):
                        return item["value"].strip()
            return ""
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    return ""

def fetch_all_collections() -> list[tuple[str, str]]:
    """Return [(collectionId, name)] for ALL collections, with console progress."""
    rows, cursor = [], None
    page = 0
    t0 = time.time()
    c_from_attr = c_from_uri = c_empty = 0

    _progress_line("[SYNC] Starting full collections sync…")
    headers = {"Authorization": f"Bearer {ENJIN_API_KEY}", "Content-Type": "application/json"}

    while True:
        page += 1
        payload = {"query": COLLECTIONS_GQL, "variables": {"after": cursor, "first": 200}}
        r = requests.post(ENJIN_API, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        body = r.json()
        if "errors" in body:
            raise RuntimeError(body["errors"])

        data = body["data"]["GetCollections"]
        batch = []
        for e in data.get("edges", []):
            node = e.get("node") or {}
            cid = str(node.get("collectionId"))
            attrs = node.get("attributes") or []
            name = attr(attrs, "name")
            if name:
                c_from_attr += 1
            else:
                uri = attr(attrs, "uri")
                name = resolve_name_from_uri(uri)
                if name:
                    c_from_uri += 1
                else:
                    c_empty += 1
            batch.append((cid, name or f"Collection {cid}"))
        rows.extend(batch)

        elapsed = time.time() - t0
        _progress_line(
            f"[SYNC] Page {page:>3} | total rows: {len(rows):>6} | "
            f"name(attr):{c_from_attr} uri:{c_from_uri} empty:{c_empty} | "
            f"elapsed: {elapsed:5.1f}s"
        )

        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]

    _progress_done(f"[SYNC] Done. {len(rows)} collections in {time.time()-t0:.1f}s "
                   f"(attr:{c_from_attr}, uri:{c_from_uri}, empty:{c_empty})")
    return rows

def ensure_collections_seeded_with_progress(min_rows: int = 1):
    """If collections.db is empty, do a full sync once with console progress."""
    try:
        conn = get_conn(COLLECTION_DB)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM collections")
        count = cur.fetchone()[0]
        conn.close()
    except Exception:
        count = 0

    if count >= min_rows:
        print(f"[SYNC] collections.db already has {count} rows. Skipping initial seed.")
        return

    print(f"[SYNC] collections.db has {count} rows. Seeding from Enjin…")
    rows = fetch_all_collections()
    collections_upsert(rows)
    print(f"[SYNC] Seed complete: inserted/upserted {len(rows)} rows.")

    # ─────────────────────────────────────────────
# Full collection fetch (paged) + progress

COLLECTIONS_GQL_FULL = """
query GetCollections($after: String, $first: Int = 200) {
  GetCollections(after: $after, first: $first) {
    edges {
      node {
        collectionId
        attributes { key value }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

def _attr(attrs, key):
    for a in attrs or []:
        if (a.get("key") or "").lower() == key.lower():
            return a.get("value")
    return None

def _resolve_name_from_uri(uri: str | None) -> str:
    if not uri:
        return ""
    for attempt in range(4):
        try:
            r = requests.get(uri, headers={"Accept":"application/json","User-Agent":"ECT/1.0"}, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.2 * (attempt + 1)); continue
            if not r.ok:
                return ""
            data = r.json()
            if isinstance(data.get("name"), str) and data["name"].strip():
                return data["name"].strip()
            attrs2 = data.get("attributes")
            if isinstance(attrs2, dict):
                v = attrs2.get("name")
                if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip():
                    return v["value"].strip()
            if isinstance(attrs2, list):
                for it in attrs2:
                    if it.get("key") == "name" and isinstance(it.get("value"), str) and it["value"].strip():
                        return it["value"].strip()
            return ""
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    return ""

def fetch_all_collections_full(progress=None) -> list[tuple[str, str]]:
    """
    Fetch ALL collections from Enjin with paging.
    Returns list of (id, name). Uses attributes.name first, then URI metadata.
    """
    rows, after, page = [], None, 0
    while True:
        data = enjin_graphql(COLLECTIONS_GQL_FULL, {"after": after, "first": 200})["GetCollections"]
        edges = data.get("edges", [])
        for e in edges:
            node = e.get("node") or {}
            cid = str(node.get("collectionId"))
            attrs = node.get("attributes") or []
            name = _attr(attrs, "name")
            if not (isinstance(name, str) and name.strip()):
                name = _resolve_name_from_uri(_attr(attrs, "uri"))
            rows.append((cid, name or f"Collection {cid}"))

        page += 1
        if progress:
            progress(f"⛏️ Fetching collections… page {page}, total {len(rows)}")

        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return rows

def collections_count() -> int:
    conn = get_conn(COLLECTION_DB)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM collections")
    n = cur.fetchone()[0]
    conn.close()
    return int(n)


def add_to_tracked(collection_ids: list[str]) -> None:
    if not collection_ids: return
    m = """
    mutation Track($ids: [String!]!) {
      AddToTracked(type: COLLECTION, chainIds: $ids)
    }
    """
    try:
        enjin_graphql(m, {"ids": [str(c) for c in collection_ids]})
    except Exception:
        pass

def _attr(attrs, key):
    for a in attrs or []:
        if (a.get("key") or "").lower() == key.lower():
            return a.get("value")
    return None

def resolve_name_via_attributes_or_uri(cid: str) -> str | None:
    # Try attributes first
    q = """
    query GetCollectionMeta($cid: BigInt!) {
      GetCollection(collectionId: $cid) { attributes { key value } }
    }
    """
    attrs = []
    try:
        attrs = enjin_graphql(q, {"cid": int(cid)})["GetCollection"].get("attributes") or []
    except Exception:
        pass
    nm = _attr(attrs, "name")
    if isinstance(nm, str) and nm.strip():
        return nm.strip()
    uri = _attr(attrs, "uri")
    if isinstance(uri, str) and uri.strip():
        for attempt in range(4):
            try:
                r = requests.get(uri, headers={"Accept":"application/json","User-Agent":"ECT/1.0"}, timeout=20)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.1 * (attempt + 1)); continue
                if not r.ok:
                    break
                data = r.json()
                if isinstance(data.get("name"), str) and data["name"].strip():
                    return data["name"].strip()
                attrs2 = data.get("attributes")
                if isinstance(attrs2, dict):
                    v = attrs2.get("name")
                    if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip():
                        return v["value"].strip()
                if isinstance(attrs2, list):
                    for it in attrs2:
                        if it.get("key") == "name" and isinstance(it.get("value"), str) and it["value"].strip():
                            return it["value"].strip()
                break
            except Exception:
                time.sleep(0.7 * (attempt + 1))
    return None

def resolve_and_store_name(cid: str) -> str:
    nm = collections_get_name(cid)
    if nm: return nm
    name = resolve_name_via_attributes_or_uri(cid) or f"Collection {cid}"
    collections_upsert([(cid, name)])
    # keep JSON backup in sync
    entries = load_collections_json()
    if not any(e.get("id") == cid for e in entries):
        entries.append({"id": cid, "name": name}); save_collections_json(entries)
    return name

# Wallet/token helpers
def fetch_all_token_accounts(address: str) -> list[dict]:
    q = """
    query WalletTokens($account: String, $after: String) {
      GetWallet(account: $account) {
        tokenAccounts(after: $after, first: 200) {
          pageInfo { endCursor hasNextPage }
          edges {
            node {
              balance
              reservedBalance
              token { tokenId collection { collectionId } }
            }
          }
        }
      }
    }
    """
    edges, after = [], None
    while True:
        d = enjin_graphql(q, {"account": address, "after": after})["GetWallet"]["tokenAccounts"]
        edges.extend(d["edges"])
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]
    return edges

def get_wallet_owned_by_collection(address: str) -> dict[str, set[str]]:
    owned: dict[str, set[str]] = defaultdict(set)
    for e in fetch_all_token_accounts(address):
        n = e["node"]
        if int(n.get("balance") or 0) + int(n.get("reservedBalance") or 0) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            tid = str(n["token"]["tokenId"])
            owned[cid].add(tid)
    return owned

def get_collection_token_ids(cid: str, page_cap: int = 20000) -> list[str]:
    q = """
    query GetCollections($after: String, $first: Int = 200) {
  GetCollections(after: $after, first: $first) {
    edges {
      node {
        collectionId
        attributes { key value }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
    """
    out, after = [], None
    while True:
        d = enjin_graphql(q, {"cid": int(cid), "after": after})["GetCollection"]["tokens"]
        out.extend([str(edge["node"]["tokenId"]) for edge in d["edges"]])
        if not d["pageInfo"]["hasNextPage"] or len(out) >= page_cap:
            break
        after = d["pageInfo"]["endCursor"]
    return out

def get_collection_token_ids_cached(cid: str, max_age_sec: int = 1800, force: bool = False) -> list[str]:
    now = time.time()
    ent = TOKEN_CACHE.get(cid)
    if (not force) and ent and (now - ent.get("ts", 0) < max_age_sec):
        return ent["ids"]
    ids = get_collection_token_ids(cid)
    ids_sorted = sort_token_ids(ids)
    TOKEN_CACHE[cid] = {"ids": ids_sorted, "ts": now}
    return ids_sorted

# Sort/filter
def sort_token_ids(ids: list[str]) -> list[str]:
    def keyfn(s: str): return (0, int(s)) if s.isdigit() else (1, s)
    return sorted(ids, key=keyfn)

def filter_ids(ids: list[str], have_set: set[str], mode: str) -> list[str]:
    if mode == "missing": return [t for t in ids if t not in have_set]
    if mode == "owned":   return [t for t in ids if t in have_set]
    return ids

def next_mode(mode: str) -> str:
    return {"all": "missing", "missing": "owned", "owned": "all"}.get(mode or "all", "all")

# ─────────────────────────────────────────────
# Reply keyboard
def show_main_keyboard(update: Update, text: str = "What would you like to do?"):
    kb = [
        [KeyboardButton("🔗 Connect wallet"), KeyboardButton("🔎 Find collection")],
        [KeyboardButton("📈 My collections"), KeyboardButton("🎲 Dice")],
    ]
    markup = ReplyKeyboardMarkup(kb, resize_keyboard=True, selective=True)
    if getattr(update, "message", None):
        return update.message.reply_text(text, reply_markup=markup)
    if getattr(update, "callback_query", None):
        return update.callback_query.message.reply_text(text, reply_markup=markup)

# ─────────────────────────────────────────────
# Inline keyboards & renderers (Find / Owned / Progress)
def build_find_keyboard(matches: list[tuple[str, str]], page: int) -> InlineKeyboardMarkup:
    total = len(matches)
    start, end = page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, total)
    rows = [[InlineKeyboardButton(f"{name} ({cid})", callback_data=f"setcol:{cid}")]
            for cid, name in matches[start:end]]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="find:prev"))
    if end < total: nav.append(InlineKeyboardButton("Next ➡️", callback_data="find:next"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="find:close")])
    return InlineKeyboardMarkup(rows)

async def render_find_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s = context.user_data.get("find") or {}
    matches = s.get("matches") or []
    page = int(s.get("page") or 0)
    term = s.get("term", "")
    total_pages = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    kb = build_find_keyboard(matches, page)
    title = f"Results for “{term}” — {len(matches)} total (page {page+1}/{total_pages})"
    if edit and getattr(update, "callback_query", None):
        await edit_or_send(update, title, reply_markup=kb)
    else:
        await update.message.reply_text(title, reply_markup=kb)
    uid = update.effective_user.id
    u = user_state(uid); u["last_view"] = "find"; u["find"] = {"term": term, "matches": matches, "page": page}
    save_state()

def build_owned_keyboard(rows_in: list[tuple[str, int]], page: int) -> InlineKeyboardMarkup:
    total = len(rows_in)
    start, end = page * OWNED_PAGE_SIZE, min((page + 1) * OWNED_PAGE_SIZE, total)
    rows = []
    for cid, cnt in rows_in[start:end]:
        nm = collections_get_name(cid) or f"Collection {cid}"
        rows.append([InlineKeyboardButton(f"{nm} ({cid}) — {cnt}", callback_data=f"owned:set:{cid}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="owned:prev"))
    if end < total: nav.append(InlineKeyboardButton("Next ➡️", callback_data="owned:next"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="owned:close")])
    return InlineKeyboardMarkup(rows)

async def render_owned_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s = context.user_data.get("owned") or {}
    rows = s.get("rows") or []
    page = int(s.get("page") or 0)
    total_pages = max(1, (len(rows) + OWNED_PAGE_SIZE - 1) // OWNED_PAGE_SIZE)
    kb = build_owned_keyboard(rows, page)
    title = f"Your collections — {len(rows)} total (page {page+1}/{total_pages})"
    if edit and getattr(update, "callback_query", None):
        await edit_or_send(update, title, reply_markup=kb)
    else:
        await update.message.reply_text(title, reply_markup=kb)
    uid = update.effective_user.id
    u = user_state(uid); u["last_view"] = "owned"; u["owned"] = {"rows": rows, "page": page}
    save_state()

def build_progress_keyboard(from_find: bool = False, from_owned: bool = False) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("⬅️ Prev", callback_data="prog:prev"),
        InlineKeyboardButton("Next ➡️", callback_data="prog:next"),
        InlineKeyboardButton("🔁 Toggle View", callback_data="prog:toggle"),
        InlineKeyboardButton("🔄 Refresh", callback_data="prog:refresh"),
    ]
    row2 = []
    if from_find:  row2.append(InlineKeyboardButton("⬅️ Back to results", callback_data="prog:back"))
    if from_owned: row2.append(InlineKeyboardButton("⬅️ Back to owned list", callback_data="prog:back_owned"))
    row2.append(InlineKeyboardButton("❌ Close", callback_data="prog:close"))
    return InlineKeyboardMarkup([row1, row2])

async def render_progress_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s = context.user_data.get("progress") or {}
    cid = s.get("cid") or ""
    name = s.get("name") or (collections_get_name(cid) or cid)
    all_ids: list[str] = s.get("ids") or []
    have_set: set[str] = s.get("have") or set()
    mode: str = s.get("mode") or "all"
    page = int(s.get("page") or 0)

    total_all = len(all_ids)
    have_count = sum(1 for t in all_ids if t in have_set)
    overall_pct = round(100 * have_count / total_all, 2) if total_all else 0.0

    ids = filter_ids(all_ids, have_set, mode)
    total = len(ids)
    total_pages = max(1, (total + PROGRESS_PAGE_SIZE - 1) // PROGRESS_PAGE_SIZE)
    if page >= total_pages:
        page = max(0, total_pages - 1); s["page"] = page

    start, end = page * PROGRESS_PAGE_SIZE, min((page + 1) * PROGRESS_PAGE_SIZE, total)
    lines = [("✅" if tid in have_set else "❌") + f" Token #{tid}" for tid in ids[start:end]]
    mode_label = {"all": "All tokens", "missing": "Only missing", "owned": "Only owned"}[mode]
    header = f"{name} ({cid}) — {have_count}/{total_all} owned ({overall_pct}%)\nView: {mode_label} • Page {page+1}/{total_pages}\n"
    text = header + ("\n".join(lines) if lines else "(No tokens in this view.)")
    kb = build_progress_keyboard(s.get("from_find", False), s.get("from_owned", False))

    if edit and getattr(update, "callback_query", None):
        await edit_or_send(update, text, reply_markup=kb)
    else:
        await safe_reply(update, text, reply_markup=kb)

    uid = update.effective_user.id
    u = user_state(uid); u["last_view"] = "progress"
    prog_copy = {**s, "have": sorted(list(s.get("have", set())))}  # JSON-serializable
    u["progress"] = prog_copy
    if s.get("cid"):
        u["collection"] = s["cid"]; USER_COLLECTION[uid] = s["cid"]
    save_state()

# ─────────────────────────────────────────────
# Commands — collections & wallet
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = user_state(uid)
    last = u.get("last_view")
    if last == "progress" and u.get("progress"):
        p = dict(u["progress"]); p["have"] = set(p.get("have", []))
        context.user_data["progress"] = p
        await render_progress_page(update, context, edit=False); return
    if last == "find" and u.get("find"):
        context.user_data["find"] = u["find"]; await render_find_page(update, context, edit=False); return
    if last == "owned" and u.get("owned"):
        context.user_data["owned"] = u["owned"]; await render_owned_page(update, context, edit=False); return

    msg = (
        "/connect – Link wallet\n"
        "/findcollection <name> – Search by name\n"
        "/setcollection <id> – Manually set collection\n"
        "/collections – Show progress\n"
        "/mycollections – List owned collections\n"
        "/mywallet – Show wallet\n"
        "/disconnect – Forget saved wallet\n"
        "/dice – Play the dice game 🎲"
    )
    await show_main_keyboard(update, "Welcome! Tap a button or use a command.\n\n" + msg)

async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = "query { RequestAccount { qrCode verificationId } }"
    data = enjin_graphql(q)["RequestAccount"]
    await update.message.reply_photo(data["qrCode"], caption="Scan with your Enjin Wallet to link.")
    poll_q = """
    query GetAccountVerified($vid: String) {
      GetAccountVerified(verificationId: $vid) { verified account { address } }
    }
    """
    for _ in range(30):
        d = enjin_graphql(poll_q, {"vid": data["verificationId"]})["GetAccountVerified"]
        if d and d.get("verified"):
            addr = d["account"]["address"]
            USER_ADDRESS[update.effective_user.id] = addr
            u = user_state(update.effective_user.id); u["address"] = addr; save_state()
            # persist in rolls users for prizes later
            upsert_user(update.effective_user.id, update.effective_user.username, addr)
            await update.message.reply_text("✅ Wallet connected. Use 🔎 Find collection or /findcollection.")
            return
        time.sleep(1)
    await update.message.reply_text("Still waiting… run /connect again if needed.")

async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ADDRESS.pop(uid, None)
    u = user_state(uid); u["address"] = None; save_state()
    await update.message.reply_text("🔌 Disconnected. I won't remember your wallet address anymore.")

async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = USER_ADDRESS.get(update.effective_user.id)
    if not addr:
        await update.message.reply_text("No wallet linked. Use /connect."); return
    await update.message.reply_text(f"🔎 Address: {addr}\n🌐 Endpoint: {ENJIN_API}")

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = USER_ADDRESS.get(update.effective_user.id)
    if not addr:
        await update.message.reply_text("Use /connect first."); return

    counts: dict[str, int] = {}
    for e in fetch_all_token_accounts(addr):
        n = e["node"]
        if int(n.get("balance") or 0) + int(n.get("reservedBalance") or 0) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            counts[cid] = counts.get(cid, 0) + 1

    if not counts:
        await update.message.reply_text("No tokens found in wallet."); return

    # make sure names exist in DB
    unknown = [cid for cid in counts if collections_get_name(cid) is None]
    if unknown:
        add_to_tracked(unknown)
        for cid in unknown:
            resolve_and_store_name(cid)

    owned_rows = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    context.user_data["owned"] = {"rows": owned_rows, "page": 0}
    await render_owned_page(update, context, edit=False)

async def setcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USER_ADDRESS:
        await update.message.reply_text("Use /connect first."); return
    if not context.args:
        await update.message.reply_text("Usage: /setcollection <collectionId>"); return
    cid = context.args[0].strip()
    add_to_tracked([cid])
    label = resolve_and_store_name(cid)
    USER_COLLECTION[uid] = cid
    u = user_state(uid); u["collection"] = cid; save_state()
    await update.message.reply_text(f"📚 Collection set to {label} ({cid}). Now run /collections.")

async def collections_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    cid = USER_COLLECTION.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first."); return
    if not cid:
        await update.message.reply_text("Set a collection first with 🔎 Find collection or /setcollection."); return

    add_to_tracked([cid])
    label = resolve_and_store_name(cid)
    try:
        ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=1800, force=False)
    except Exception as e:
        await update.message.reply_text("Could not fetch collection.\n" + str(e)); return
    if not ids_sorted:
        await update.message.reply_text("No tokens found in that collection."); return

    have_set = get_wallet_owned_by_collection(addr).get(cid, set())
    context.user_data["progress"] = {
        "cid": cid, "name": label, "ids": ids_sorted,
        "have": set(have_set), "page": 0, "mode": "all",
    }
    await render_progress_page(update, context, edit=False)

# ─────────────────────────────────────────────
# Search command (DB first; fallback JSON)
async def findcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text("Type a name or part of a name to search:", reply_markup=ReplyKeyboardRemove())
        return
    term = " ".join(context.args).strip()
    matches = collections_search(term, limit=400)
    if not matches:
        entries = load_collections_json()
        matches = [(e["id"], e["name"]) for e in entries if term.lower() in e.get("name","").lower()]
    if not matches:
        await show_main_keyboard(update, "No collections matched. Try again or tap a button."); return
    context.user_data["find"] = {"term": term, "matches": matches, "page": 0}
    await render_find_page(update, context, edit=False)

# Reply-keyboard taps
async def on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "")
    norm = "".join(ch for ch in raw if ch.isalnum() or ch.isspace()).lower()
    if "connect wallet" in norm:
        await connect(update, context); return
    if "find collection" in norm:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text("Type a name or part of a name to search:", reply_markup=ReplyKeyboardRemove())
        return
    if "my collections" in norm:
        await mycollections(update, context); return
    if "dice" in norm:
        await dice_command(update, context); return

# Capture search term after prompt
async def capture_find_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get(AWAITING_FIND_FLAG):
        return
    term = (update.message.text or "").strip()
    context.user_data[AWAITING_FIND_FLAG] = False
    saved_args = getattr(context, "args", None)
    context.args = [term]
    try:
        await findcollection(update, context)
    finally:
        context.args = saved_args

# ─────────────────────────────────────────────
# Dice game — DB & logic
def upsert_user(user_id: int, username: str | None, wallet: str | None):
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, wallet, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username,
          wallet=COALESCE(excluded.wallet, users.wallet),
          updated_at=CURRENT_TIMESTAMP
    """, (user_id, username, wallet))
    conn.commit(); conn.close()

def get_wallet_for_user(user_id: int) -> str | None:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("SELECT wallet FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone(); conn.close()
    if row and row[0]:
        return row[0]
    w = USER_ADDRESS.get(user_id)
    if w: upsert_user(user_id, None, w)
    return w

def can_roll(user_id: int) -> tuple[bool, int]:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("SELECT next_time FROM cooldowns WHERE user_id=?", (user_id,))
    row = cur.fetchone(); conn.close()
    now = time.time()
    if row and row[0] and float(row[0]) > now:
        return False, int(float(row[0]) - now)
    return True, 0

def set_cooldown(user_id: int, secs: int = 4):
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("REPLACE INTO cooldowns (user_id, next_time) VALUES (?,?)", (user_id, time.time()+secs))
    conn.commit(); conn.close()

def record_roll(user_id: int, username: str, wallet: str | None, roll: int):
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("INSERT INTO rolls (user_id, username, wallet, roll) VALUES (?,?,?,?)",
                (user_id, username, wallet or "", roll))
    conn.commit(); conn.close()

def today_sum(user_id: int) -> int:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(roll),0) FROM rolls WHERE user_id=? AND date(ts)=date('now','localtime')", (user_id,))
    v = cur.fetchone()[0] or 0; conn.close(); return int(v)

def weekly_sum(user_id: int) -> int:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(roll),0) FROM rolls
        WHERE user_id=?
          AND strftime('%Y-%W', ts, 'localtime') = strftime('%Y-%W','now','localtime')
    """, (user_id,))
    v = cur.fetchone()[0] or 0; conn.close(); return int(v)

def leaderboard_daily(limit=10) -> list[tuple[str, int]]:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(username, CAST(user_id AS TEXT)) AS name, SUM(roll) AS total
        FROM rolls
        WHERE date(ts)=date('now','localtime')
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT ?
    """, (limit,))
    rows = [(r[0], int(r[1])) for r in cur.fetchall()]
    conn.close(); return rows

def leaderboard_weekly(limit=10) -> list[tuple[str, int]]:
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(username, CAST(user_id AS TEXT)) AS name, SUM(roll) AS total
        FROM rolls
        WHERE strftime('%Y-%W', ts, 'localtime') = strftime('%Y-%W','now','localtime')
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT ?
    """, (limit,))
    rows = [(r[0], int(r[1])) for r in cur.fetchall()]
    conn.close(); return rows

def format_roll(roll: int) -> str:
    if roll == 100: return f"🎲 You rolled **{roll}** — 💯 JACKPOT!"
    if 95 <= roll <= 99: return f"🎲 You rolled **{roll}** — 🔥 Epic roll!"
    if 90 <= roll <= 94: return f"🎲 You rolled **{roll}** — ✨ Great roll!"
    if roll == 1: return f"🎲 You rolled **{roll}** — 💀 Ouch, critical fail!"
    if roll == 2: return f"🎲 You rolled **{roll}** — 😬 Painful…"
    return f"🎲 You rolled **{roll}**!"

def progress_bar(used: int, total: int = 50) -> str:
    filled_blocks = (used * 10) // total
    return f"{'▰'*filled_blocks}{'▱'*(10-filled_blocks)} ({used}/{total} rolls)"

# Dice UI
async def dice_game_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    user = update.effective_user
    rolls_today = today_sum(user.id)
    weekly = weekly_sum(user.id)
    text = (
        f"🎲 Welcome to Dice Game, {user.first_name}!\n\n"
        f"📅 Today’s score: **{rolls_today}**\n"
        f"🗓️ Weekly score: **{weekly}**\n"
        f"{progress_bar(rolls_today, 50)}\n"
    )
    kb = [
        [InlineKeyboardButton("🎲 Roll", callback_data="roll")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
         InlineKeyboardButton("📊 My Rank", callback_data="myrank")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def roll_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    can, remain = can_roll(user.id)
    if not can:
        await update.callback_query.answer(f"⏳ Wait {remain}s before rolling again.", show_alert=True)
        return
    roll = random.randint(1, 100)
    set_cooldown(user.id, 4)
    wallet = get_wallet_for_user(user.id)
    upsert_user(user.id, user.username, wallet)
    record_roll(user.id, user.username or str(user.id), wallet, roll)
    text = format_roll(roll) + f"\n\n📅 Today: {today_sum(user.id)} | 🗓️ Week: {weekly_sum(user.id)}"
    await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    await asyncio.sleep(2)
    await dice_game_menu(update, context, edit=True)

async def leaderboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    daily = leaderboard_daily()
    weekly = leaderboard_weekly()
    today_txt = "🏆 Daily Leaderboard\n" + "\n".join([f"{i+1}. {u} — {s}" for i,(u,s) in enumerate(daily)])
    week_txt  = "🏆 Weekly Leaderboard\n" + "\n".join([f"{i+1}. {u} — {s}" for i,(u,s) in enumerate(weekly)])
    await update.callback_query.edit_message_text(today_txt + "\n\n" + week_txt)
    await asyncio.sleep(5)
    await dice_game_menu(update, context, edit=True)

async def myrank_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn(ROLLS_DB); cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)+1 FROM (
          SELECT user_id, SUM(roll) AS total FROM rolls
          WHERE date(ts)=date('now','localtime') GROUP BY user_id HAVING total > ?
        ) t
    """, (today_sum(user.id),))
    daily_rank = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*)+1 FROM (
          SELECT user_id, SUM(roll) AS total FROM rolls
          WHERE strftime('%Y-%W', ts, 'localtime')=strftime('%Y-%W','now','localtime')
          GROUP BY user_id HAVING total > ?
        ) t
    """, (weekly_sum(user.id),))
    weekly_rank = cur.fetchone()[0]
    conn.close()
    text = f"📅 Today: #{daily_rank} with {today_sum(user.id)}\n🗓️ Week: #{weekly_rank} with {weekly_sum(user.id)}"
    await update.callback_query.edit_message_text(text)
    await asyncio.sleep(3)
    await dice_game_menu(update, context, edit=True)

async def dice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await dice_game_menu(update, context, edit=False)

# ─────────────────────────────────────────────
# Callback handler routing (Dice + Collections UIs)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    data = q.data or ""

    # Dice buttons
    if data == "roll":        await roll_button(update, context); return
    if data == "leaderboard": await leaderboard_button(update, context); return
    if data == "myrank":      await myrank_button(update, context); return

    # Find pager/close
    if data.startswith("find:"):
        s = context.user_data.get("find") or {}
        page = int(s.get("page") or 0)
        total = len(s.get("matches") or [])
        if data == "find:prev" and page > 0:
            s["page"] = page - 1; context.user_data["find"] = s
            await render_find_page(update, context, edit=True); return
        if data == "find:next" and (page + 1) * PAGE_SIZE < total:
            s["page"] = page + 1; context.user_data["find"] = s
            await render_find_page(update, context, edit=True); return
        if data == "find:close":
            context.user_data.pop("find", None)
            u = user_state(q.from_user.id)
            if u.get("last_view") == "find":
                u["last_view"] = None; save_state()
            await edit_or_send(update, "Search closed.")
            await show_main_keyboard(update, "What would you like to do next?"); return
        return

    # Owned pager/select/close
    if data.startswith("owned:"):
        s = context.user_data.get("owned") or {}
        page = int(s.get("page") or 0)
        total = len(s.get("rows") or [])
        if data == "owned:prev" and page > 0:
            s["page"] = page - 1; context.user_data["owned"] = s
            await render_owned_page(update, context, edit=True); return
        if data == "owned:next" and (page + 1) * OWNED_PAGE_SIZE < total:
            s["page"] = page + 1; context.user_data["owned"] = s
            await render_owned_page(update, context, edit=True); return
        if data == "owned:close":
            context.user_data.pop("owned", None)
            u = user_state(q.from_user.id)
            if u.get("last_view") == "owned":
                u["last_view"] = None; save_state()
            await edit_or_send(update, "Owned list closed.")
            await show_main_keyboard(update, "What would you like to do next?"); return
        if data.startswith("owned:set:"):
            cid = data.split(":", 2)[2]
            add_to_tracked([cid]); label = resolve_and_store_name(cid)
            USER_COLLECTION[q.from_user.id] = cid
            u = user_state(q.from_user.id); u["collection"] = cid; save_state()
            addr = USER_ADDRESS.get(q.from_user.id)
            if not addr:
                await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
                await show_main_keyboard(update, "Link a wallet to view progress."); return
            try:
                ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=1800, force=False)
            except Exception as e:
                await edit_or_send(update, "Could not fetch collection.\n" + str(e)); return
            if not ids_sorted:
                await edit_or_send(update, "No tokens found in that collection."); return
            have_set = get_wallet_owned_by_collection(addr).get(cid, set())
            context.user_data["progress"] = {
                "cid": cid, "name": label, "ids": ids_sorted,
                "have": set(have_set), "page": 0, "mode": "all", "from_owned": True
            }
            await render_progress_page(update, context, edit=True); return
        return

    # Progress pager/toggle/refresh/back/close
    if data.startswith("prog:"):
        s = context.user_data.get("progress") or {}
        page = int(s.get("page") or 0)
        mode = s.get("mode") or "all"
        cid  = s.get("cid")
        ids  = s.get("ids") or []
        have = s.get("have") or set()
        filtered_total = len(filter_ids(ids, have, mode))

        if data == "prog:prev" and page > 0:
            s["page"] = page - 1; context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:next" and (page + 1) * PROGRESS_PAGE_SIZE < filtered_total:
            s["page"] = page + 1; context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:toggle":
            s["mode"] = next_mode(mode); s["page"] = 0
            context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:refresh":
            if cid:
                add_to_tracked([cid])
                s["name"] = resolve_and_store_name(cid)
                try:
                    ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=0, force=True)
                    s["ids"] = ids_sorted
                    addr = USER_ADDRESS.get(q.from_user.id)
                    have_set = get_wallet_owned_by_collection(addr).get(cid, set()) if addr else set()
                    s["have"] = set(have_set); s["page"] = 0
                    context.user_data["progress"] = s
                except Exception:
                    pass
            await render_progress_page(update, context, edit=True); return
        if data == "prog:back":
            find_state = context.user_data.get("find") or user_state(q.from_user.id).get("find")
            if find_state:
                context.user_data["find"] = find_state; await render_find_page(update, context, edit=True)
            return
        if data == "prog:back_owned":
            owned_state = context.user_data.get("owned") or user_state(q.from_user.id).get("owned")
            if owned_state:
                context.user_data["owned"] = owned_state; await render_owned_page(update, context, edit=True)
            return
        if data == "prog:close":
            context.user_data.pop("progress", None)
            u = user_state(q.from_user.id)
            if u.get("last_view") == "progress":
                u["last_view"] = None; save_state()
            await edit_or_send(update, "Progress closed.")
            await show_main_keyboard(update, "What would you like to do next?"); return
        return

    # Select from Find → jump straight into progress
    if data.startswith("setcol:"):
        cid = data.split(":", 1)[1]
        add_to_tracked([cid]); label = resolve_and_store_name(cid)
        USER_COLLECTION[q.from_user.id] = cid
        u = user_state(q.from_user.id); u["collection"] = cid; save_state()
        addr = USER_ADDRESS.get(q.from_user.id)
        if not addr:
            await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
            await show_main_keyboard(update, "Link a wallet to view progress."); return
        try:
            ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=1800, force=False)
        except Exception as e:
            await edit_or_send(update, "Could not fetch collection.\n" + str(e)); return
        if not ids_sorted:
            await edit_or_send(update, "No tokens found in that collection."); return
        have_set = get_wallet_owned_by_collection(addr).get(cid, set())
        context.user_data["progress"] = {
            "cid": cid, "name": label, "ids": ids_sorted,
            "have": set(have_set), "page": 0, "mode": "all", "from_find": True
        }
        await render_progress_page(update, context, edit=True)

# ─────────────────────────────────────────────
# Hourly: refresh collections
def get_all_collection_ids_from_api() -> list[str]:
    q = """
query GetCollections($after: String, $first: Int = 200) {
  GetCollections(after: $after, first: $first) {
    edges {
      node {
        collectionId
        attributes { key value }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
    """
    ids, after = [], None
    while True:
        data = enjin_graphql(q, {"after": after})["GetCollections"]
        for e in data["edges"]:
            ids.append(str(e["node"]["collectionId"]))
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return ids

async def hourly_collections_refresh(context: ContextTypes.DEFAULT_TYPE):
    try:
        ids = get_all_collection_ids_from_api()
        if not ids: return
        collections_bulk_insert_ids(ids)
        add_to_tracked(ids[:200])  # small batch
        # resolve some placeholder names each hour
        conn = get_conn(COLLECTION_DB); cur = conn.cursor()
        cur.execute("""
            SELECT id FROM collections
            WHERE name LIKE 'Collection %'
            ORDER BY updated_at ASC
            LIMIT 200
        """)
        todo = [r[0] for r in cur.fetchall()]
        conn.close()
        rows = []
        for cid in todo:
            nm = resolve_name_via_attributes_or_uri(cid) or f"Collection {cid}"
            rows.append((cid, nm))
        collections_upsert(rows)
        sync_json_from_db_if_needed()
        print(f"✅ Collections refreshed: {len(ids)} ids (resolved {len(rows)} names).")
    except Exception as e:
        print(f"⚠️ Error refreshing collections: {e}")

# ─────────────────────────────────────────────
# Dispatcher + main
def main():
    # migrate from JSON once (if exists)
    if COLLECTIONS_JSON.exists():
        try:
            existing = json.loads(COLLECTIONS_JSON.read_text("utf-8"))
            rows = [(e["id"], e["name"]) for e in existing if "id" in e and "name" in e]
            collections_upsert(rows)
            print(f"📦 Migrated {len(rows)} collections from JSON → DB")
        except Exception as e:
            print(f"⚠️ Migration failed: {e}")
               
                # 🔹 Seed DB once if empty (shows progress in console)
    ensure_collections_seeded_with_progress(min_rows=1)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("disconnect", disconnect))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("mycollections", mycollections))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections_cmd))
    app.add_handler(CommandHandler("findcollection", findcollection))
    app.add_handler(CommandHandler("dice", dice_command))
    app.add_handler(CommandHandler("synccollections", synccollections))


    # Callback queries (dice + collections)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Text taps & capture search term
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_find_term), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply_button), group=1)

# Admin-only sync command
    ADMIN_ID = 7667571007
    app.add_handler(CommandHandler("synccollections", synccollections, filters=filters.User(ADMIN_ID)))

    # Hourly collections refresh
    app.job_queue.run_repeating(hourly_collections_refresh, interval=3600, first=10)

    print("Bot starting… Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
