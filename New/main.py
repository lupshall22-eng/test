# === main.py (WebApp + caches + external wallet save + Dice API merged) ===
import os, time, json, asyncio, sqlite3, requests, random, pathlib
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone, date
from typing import Optional

from dotenv import load_dotenv

# Telegram
from telegram import (
    Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    WebAppInfo,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# FastAPI
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Env
load_dotenv()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENJIN_API = os.getenv("ENJIN_GRAPHQL", "https://platform.enjin.io/graphql")
ENJIN_API_KEY = os.getenv("ENJIN_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://test-97pb.onrender.com/web/index.html").strip()  # e.g., https://your-domain.tld/web/index.html

# External WebApp DB endpoint to save wallets
WEBAPP_WALLET_ENDPOINT = os.getenv("WEBAPP_WALLET_ENDPOINT", "").strip()
WEBAPP_API_KEY = os.getenv("WEBAPP_API_KEY", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBAPP_URL = (os.getenv("WEBAPP_URL", "").strip()
              or (f"{PUBLIC_URL}/web/index.html" if PUBLIC_URL else ""))

if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
if not ENJIN_API_KEY:
    raise SystemExit("Missing ENJIN_API_KEY in .env")

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
# ---- Fast caches ----
OWNED_CACHE: dict[int, dict] = {}  # {telegram_user_id: {"ts": float, "owned": dict[str,set[str]]}}
OWNED_MAX_AGE = 300  # 5 minutes
TOKEN_CACHE_MAX_AGE = 1800  # 30 minutes

# Paths
DATA_DIR = Path("data"); DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = DATA_DIR / "state.json"
COLLECTION_DB = DATA_DIR / "collection.db"   # collections table (id, name)
APP_DB        = DATA_DIR / "app.db"          # tiny local cache for speed
COLLECTIONS_JSON = Path("collections.json")  # optional backup/export

# ─────────────────────────────────────────────
# Enjin config
USE_BEARER = False
def gql_headers():
    if USE_BEARER:
        tok = ENJIN_API_KEY if ENJIN_API_KEY.startswith("Bearer ") else f"Bearer {ENJIN_API_KEY}"
    else:
        tok = ENJIN_API_KEY
    return {"Authorization": tok, "Content-Type": "application/json"}

# OUTBOUND: save wallet to your WebApp DB
def post_wallet_to_webapp(telegram_id: int, username: str | None, wallet: str) -> None:
    if not WEBAPP_WALLET_ENDPOINT:
        print("ℹ️ WEBAPP_WALLET_ENDPOINT not set; skipping external wallet save.")
        return
    payload = {"telegram_id": telegram_id, "username": username or "", "wallet_address": wallet}
    headers = {"Content-Type": "application/json"}
    if WEBAPP_API_KEY:
        headers["X-API-Key"] = WEBAPP_API_KEY
    try:
        r = requests.post(WEBAPP_WALLET_ENDPOINT, json=payload, headers=headers, timeout=10)
        if not r.ok:
            print(f"⚠️ WebApp wallet save failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ WebApp wallet save error: {e}")

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
            try:
                await update.callback_query.edit_message_text(text[:4096], reply_markup=reply_markup)
            except Exception:
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
# SQLite — collection.db & app.db
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

# app.db for generic user cache (kept)
def init_app_db():
    conn = get_conn(APP_DB)
    cur = conn.cursor()
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
init_app_db()

# Wallet cache helpers (kept)
def cache_user_wallet(user_id: int, username: str | None, wallet: str | None):
    conn = get_conn(APP_DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, wallet, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username,
          wallet=COALESCE(excluded.wallet, users.wallet),
          updated_at=CURRENT_TIMESTAMP
    """, (user_id, username, wallet))
    conn.commit()
    conn.close()

def get_cached_wallet(user_id: int) -> str | None:
    conn = get_conn(APP_DB)
    cur = conn.cursor()
    cur.execute("SELECT wallet FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

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

async def enjin_graphql_async(query: str, variables: dict | None = None) -> dict:
    def _do():
        r = requests.post(
            ENJIN_API,
            json={"query": query, "variables": variables or {}},
            headers=gql_headers(),
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        if "errors" in body:
            raise RuntimeError(str(body["errors"]))
        return body["data"]
    return await asyncio.to_thread(_do)

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

async def _fetch_owned_map(address: str) -> dict[str, set[str]]:
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
    owned: dict[str, set[str]] = defaultdict(set)
    after = None
    while True:
        data = await enjin_graphql_async(q, {"account": address, "after": after})
        ta = data["GetWallet"]["tokenAccounts"]
        for e in ta["edges"]:
            n = e["node"]
            if int(n.get("balance") or 0) + int(n.get("reservedBalance") or 0) > 0:
                cid = str(n["token"]["collection"]["collectionId"])
                tid = str(n["token"]["tokenId"])
                owned[cid].add(tid)
        if not ta["pageInfo"]["hasNextPage"]:
            break
        after = ta["pageInfo"]["endCursor"]
    return owned

def _owned_cache_get(uid: int):
    ent = OWNED_CACHE.get(uid)
    if ent and (time.time() - ent["ts"] < OWNED_MAX_AGE):
        return ent["owned"]
    return None

def _owned_cache_put(uid: int, owned_map: dict[str, set[str]]):
    OWNED_CACHE[uid] = {"ts": time.time(), "owned": owned_map}

async def refresh_owned_cache(uid: int, address: str):
    owned = await _fetch_owned_map(address)
    _owned_cache_put(uid, owned)
    return owned

def get_wallet_owned_by_collection(address: str) -> dict[str, set[str]]:
    owned: dict[str, set[str]] = defaultdict(set)
    for e in fetch_all_token_accounts(address):
        n = e["node"]
        if int(n.get("balance") or 0) + int(n.get("reservedBalance") or 0) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            tid = str(n["token"]["tokenId"])
            owned[cid].add(tid)
    return owned

def sort_token_ids(ids: list[str]) -> list[str]:
    def keyfn(s: str): return (0, int(s)) if s.isdigit() else (1, s)
    return sorted(ids, key=keyfn)

def get_collection_token_ids(cid: str, page_cap: int = 20000) -> list[str]:
    q = """
    query GetCollectionTokens($cid: BigInt!, $after: String) {
      GetCollection(collectionId: $cid) {
        tokens(after: $after) {
          pageInfo { endCursor hasNextPage }
          edges { node { tokenId } }
        }
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

def get_collection_token_ids_cached(cid: str, max_age_sec: int = TOKEN_CACHE_MAX_AGE, force: bool = False) -> list[str]:
    now = time.time()
    ent = TOKEN_CACHE.get(cid)
    if (not force) and ent and (now - ent.get("ts", 0) < max_age_sec):
        return ent["ids"]
    ids = get_collection_token_ids(cid)
    ids_sorted = sort_token_ids(ids)
    TOKEN_CACHE[cid] = {"ids": ids_sorted, "ts": now}
    return ids_sorted

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
        [KeyboardButton("📈 My collections")],
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
# Commands — collections & wallet + WebApp
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = user_state(uid)
    last = u.get("last_view")

    # Inline WebApp button (only if we have a URL)
    open_webapp = None
    if WEBAPP_URL:
        open_webapp = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎲 Play Dice Dash", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )

    # Restore last view if present
    if last == "progress" and u.get("progress"):
        p = dict(u["progress"]); p["have"] = set(p.get("have", []))
        context.user_data["progress"] = p
        await render_progress_page(update, context, edit=False)
        if open_webapp:
            await safe_reply(update, "You can also launch the Web App:", open_webapp)
        return

    if last == "find" and u.get("find"):
        context.user_data["find"] = u["find"]
        await render_find_page(update, context, edit=False)
        if open_webapp:
            await safe_reply(update, "You can also launch the Web App:", open_webapp)
        return

    if last == "owned" and u.get("owned"):
        context.user_data["owned"] = u["owned"]
        await render_owned_page(update, context, edit=False)
        if open_webapp:
            await safe_reply(update, "You can also launch the Web App:", open_webapp)
        return

    # Default welcome + reply keyboard
    msg = (
        "/connect – Link wallet\n"
        "/findcollection <name> – Search by name\n"
        "/setcollection <id> – Manually set collection\n"
        "/collections – Show progress\n"
        "/mycollections – List owned collections\n"
        "/mywallet – Show wallet\n"
        "/disconnect – Forget saved wallet\n"
    )
    await show_main_keyboard(update, "Welcome! Tap a button or use a command.\n\n" + msg)

    # Also offer the WebApp button, if available
    if open_webapp:
        await safe_reply(update, "Or launch the Web App:", open_webapp)

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

            uid = update.effective_user.id
            USER_ADDRESS[uid] = addr
            u = user_state(uid); u["address"] = addr; save_state()

            cache_user_wallet(uid, update.effective_user.username, addr)

            def _push():
                post_wallet_to_webapp(uid, update.effective_user.username, addr)
            await asyncio.to_thread(_push)

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

async def syncwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or str(uid)
    wallet = get_cached_wallet(uid) or USER_ADDRESS.get(uid)
    if not wallet:
        await update.message.reply_text("No wallet saved yet. Use /connect first.")
        return
    cache_user_wallet(uid, username, wallet)
    def _push(): post_wallet_to_webapp(uid, update.effective_user.username, wallet)
    await asyncio.to_thread(_push)
    await update.message.reply_text("✅ Wallet sync requested. Check your web app DB/logs.")

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first.")
        return

    owned = _owned_cache_get(uid)
    if owned is None:
        try:
            await update.message.reply_text("⏳ Gathering your collections…")
            owned = await refresh_owned_cache(uid, addr)
        except Exception as e:
            await update.message.reply_text("Could not load wallet.\n" + str(e))
            return
    else:
        context.application.create_task(refresh_owned_cache(uid, addr))

    unknown = [cid for cid in owned.keys() if collections_get_name(cid) is None]
    if unknown:
        add_to_tracked(unknown)
        for cid in unknown:
            resolve_and_store_name(cid)

    counts = {cid: len(tset) for cid, tset in owned.items()}
    if not counts:
        await update.message.reply_text("No tokens found in wallet.")
        return

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
    username = update.effective_user.username or str(uid)
    wallet = get_cached_wallet(uid) or USER_ADDRESS.get(uid)
    if wallet:
        cache_user_wallet(uid, username, wallet)

    addr = wallet
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

    owned_cached = _owned_cache_get(uid)
    if owned_cached is None and addr:
        context.application.create_task(refresh_owned_cache(uid, addr))
        have_set = set()
    else:
        have_set = owned_cached.get(cid, set())

    context.user_data["progress"] = {
        "cid": cid, "name": label, "ids": ids_sorted,
        "have": set(have_set), "page": 0, "mode": "all",
    }
    await render_progress_page(update, context, edit=False)

# ─────────────────────────────────────────────
# Search command (DB first; fallback JSON)
async def findcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or str(uid)
    wallet = get_cached_wallet(uid) or USER_ADDRESS.get(uid)
    if wallet:
        cache_user_wallet(uid, username, wallet)
        context.application.create_task(refresh_owned_cache(uid, wallet))

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
# Callback handler routing (Collections UIs)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    data = q.data or ""

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
            s["page"] = page - 1
            context.user_data["owned"] = s
            await render_owned_page(update, context, edit=True)
            return

        if data == "owned:next" and (page + 1) * OWNED_PAGE_SIZE < total:
            s["page"] = page + 1
            context.user_data["owned"] = s
            await render_owned_page(update, context, edit=True)
            return

        if data == "owned:close":
            context.user_data.pop("owned", None)
            u = user_state(q.from_user.id)
            if u.get("last_view") == "owned":
                u["last_view"] = None
                save_state()
            await edit_or_send(update, "Owned list closed.")
            await show_main_keyboard(update, "What would you like to do next?")
            return

        if data.startswith("owned:set:"):
            cid = data.split(":", 2)[2]
            add_to_tracked([cid])
            label = resolve_and_store_name(cid)

            USER_COLLECTION[q.from_user.id] = cid
            u = user_state(q.from_user.id)
            u["collection"] = cid
            save_state()

            addr = USER_ADDRESS.get(q.from_user.id)
            if not addr:
                await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
                await show_main_keyboard(update, "Link a wallet to view progress.")
                return

            try:
                ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=1800, force=False)
            except Exception as e:
                await edit_or_send(update, "Could not fetch collection.\n" + str(e))
                return

            if not ids_sorted:
                await edit_or_send(update, "No tokens found in that collection.")
                return

            owned_cached = _owned_cache_get(q.from_user.id)
            if owned_cached is None:
                context.application.create_task(refresh_owned_cache(q.from_user.id, addr))
                have_set = set()
            else:
                have_set = owned_cached.get(cid, set())

            context.user_data["progress"] = {
                "cid": cid,
                "name": label,
                "ids": ids_sorted,
                "have": set(have_set),
                "page": 0,
                "mode": "all",
                "from_owned": True,
            }
            await render_progress_page(update, context, edit=True)
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
        add_to_tracked([cid])
        label = resolve_and_store_name(cid)

        USER_COLLECTION[q.from_user.id] = cid
        u = user_state(q.from_user.id)
        u["collection"] = cid
        save_state()

        addr = USER_ADDRESS.get(q.from_user.id)
        if not addr:
            await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
            await show_main_keyboard(update, "Link a wallet to view progress.")
            return

        try:
            ids_sorted = get_collection_token_ids_cached(cid, max_age_sec=1800, force=False)
        except Exception as e:
            await edit_or_send(update, "Could not fetch collection.\n" + str(e))
            return

        if not ids_sorted:
            await edit_or_send(update, "No tokens found in that collection.")
            return

        owned_cached = _owned_cache_get(q.from_user.id)
        if owned_cached is None:
            context.application.create_task(refresh_owned_cache(q.from_user.id, addr))
            have_set = set()
        else:
            have_set = owned_cached.get(cid, set())

        context.user_data["progress"] = {
            "cid": cid,
            "name": label,
            "ids": ids_sorted,
            "have": set(have_set),
            "page": 0,
            "mode": "all",
            "from_find": True,
        }
        await render_progress_page(update, context, edit=True)
        return

# ─────────────────────────────────────────────
# WebApp data handler (optional)
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wad = update.message.web_app_data
    if not wad:
        return
    try:
        await update.message.reply_text(f"Received WebApp data:\n{wad.data[:1000]}")
    except Exception:
        await update.message.reply_text("Received WebApp data.")

# ─────────────────────────────────────────────
# Hourly: refresh collections
def get_all_collection_ids_from_api() -> list[str]:
    q = """
    query GetCollections($after: String, $first: Int = 200) {
      GetCollections(after: $after, first: $first) {
        edges { node { collectionId attributes { key value } } }
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
# Dispatcher
def build_application() -> Application:
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
    app.add_handler(CommandHandler("syncwallet", syncwallet))

    # Callback queries (collections)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Text taps & capture search term
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_find_term), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply_button), group=1)

    # WebApp data
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))

    # Hourly collections refresh
    try:
        app.job_queue.run_repeating(hourly_collections_refresh, interval=3600, first=10)
    except Exception:
        print("ℹ️ JobQueue not available. Skipping hourly refresh.")

    return app

# ─────────────────────────────────────────────
# FastAPI app (bot + dice API + static)
fastapi_app = FastAPI(title="Telegram Bot + Dice API")

# CORS
FRONTEND_ORIGINS = os.getenv("FRONTEND_ORIGINS", "*")
_allow_origins = [o.strip() for o in FRONTEND_ORIGINS.split(",")] if FRONTEND_ORIGINS else ["*"]
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Static mounts (if folders exist)
_FILE_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _FILE_DIR.parent
_STATIC_DIR = _PROJECT_ROOT / "static"
_WEB_DIR = _PROJECT_ROOT / "web"
if _STATIC_DIR.exists():
    fastapi_app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
if _WEB_DIR.exists():
    fastapi_app.mount("/web", StaticFiles(directory=_WEB_DIR), name="web")

# Health (single route)
@fastapi_app.api_route("/", methods=["GET", "HEAD"])
async def health():
    return {"ok": True, "service": "telegram-bot + dice-api"}

# Optional: serve /leaderboard page if present
@fastapi_app.get("/leaderboard")
async def serve_leaderboard_page():
    path = _WEB_DIR / "leaderboard.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"leaderboard.html not found at {path}")
    return FileResponse(path)

# ─────────────────────────────────────────────
# Dice game (DB + routes)
MAX_DAILY = int(os.getenv("MAX_DAILY", "50"))
COOLDOWN_S = float(os.getenv("COOLDOWN_S", "4"))
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "12345"))  # dev fallback

_DB_PATH = pathlib.Path(os.getenv("DATABASE_PATH", _FILE_DIR / "storage" / "dice.db")).resolve()

def _init_db():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH, timeout=10) as conn:
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
          date_utc TEXT NOT NULL,
          roll_index INTEGER NOT NULL,
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
          week_id TEXT NOT NULL,
          total_score INTEGER NOT NULL DEFAULT 0,
          days_played INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (telegram_id, week_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rolls_user_day  ON rolls(telegram_id, date_utc);
        CREATE INDEX IF NOT EXISTS idx_rolls_user_time ON rolls(created_at);
        CREATE INDEX IF NOT EXISTS idx_daily_date_score ON daily_totals(date_utc, total_score DESC);
        CREATE INDEX IF NOT EXISTS idx_week_week_score  ON weekly_totals(week_id, total_score DESC);
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

def _db():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(_DB_PATH, timeout=10)

_init_db()

def _today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def _week_id(dt: Optional[date] = None) -> str:
    d = dt or datetime.now(timezone.utc).date()
    y, wk, _ = d.isocalendar()
    return f"{y}-W{wk:02d}"

def _rolls_used_today(user_id: int) -> int:
    with _db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM rolls WHERE telegram_id=? AND date_utc=?", (user_id, _today_utc_str()))
        return int(cur.fetchone()[0])

def _seconds_since_last_roll(user_id: int) -> float:
    with _db() as conn:
        cur = conn.execute(
            "SELECT strftime('%s','now') - strftime('%s', MAX(created_at)) FROM rolls WHERE telegram_id=?",
            (user_id,)
        )
        val = cur.fetchone()[0]
        try:
            return float(val if val is not None else 10_000.0)
        except Exception:
            return 10_000.0

def _upsert_daily_weekly(user_id: int, add_total: int):
    tday = _today_utc_str()
    wk = _week_id()
    with _db() as conn:
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

def _json_error(status: int, code: str, **extra):
    return JSONResponse(status_code=status, content={"error": code, **extra})

def _get_idempo(conn: sqlite3.Connection, user_id: int, key: str):
    row = conn.execute("SELECT response_json FROM roll_requests WHERE telegram_id=? AND key=?", (user_id, key)).fetchone()
    return json.loads(row[0]) if row else None

def _save_idempo(conn: sqlite3.Connection, user_id: int, key: str, resp: dict):
    conn.execute("INSERT OR IGNORE INTO roll_requests(telegram_id, key, response_json) VALUES (?,?,?)",
                 (user_id, key, json.dumps(resp, separators=(',', ':'))))

def _resolve_user_id(x_tg_id: Optional[str]) -> int:
    try:
        if x_tg_id and x_tg_id.isdigit():
            return int(x_tg_id)
    except Exception:
        pass    # fallback to test id
    return TEST_USER_ID

@fastapi_app.get("/config")
async def dice_config(x_tg_id: Optional[str] = Header(None)):
    uid = _resolve_user_id(x_tg_id)
    used = _rolls_used_today(uid)
    return {
        "rolls_left": max(0, MAX_DAILY - used),
        "cooldown": COOLDOWN_S,
        "daily_limit": MAX_DAILY,
        "user": {"telegram_id": uid},
    }

@fastapi_app.post("/roll")
async def dice_roll(request: Request, x_tg_id: Optional[str] = Header(None)):
    uid = _resolve_user_id(x_tg_id)
    idem_key = request.headers.get("X-Idempotency-Key")

    if idem_key:
        with _db() as conn:
            prev = _get_idempo(conn, uid, idem_key)
            if prev:
                return prev

    since = _seconds_since_last_roll(uid)
    if since < COOLDOWN_S:
        return _json_error(429, "COOLDOWN_ACTIVE", seconds_remaining=round(COOLDOWN_S - since, 1))

    used = _rolls_used_today(uid)
    if used >= MAX_DAILY:
        return _json_error(400, "DAILY_LIMIT_REACHED", daily_limit=MAX_DAILY)

    d1 = random.randint(1, 6); d2 = random.randint(1, 6)
    total = d1 + d2
    idx = used + 1
    tday = _today_utc_str()

    with _db() as conn:
        conn.execute("""
            INSERT INTO rolls(telegram_id, date_utc, roll_index, d1, d2, total, created_at)
            VALUES(?,?,?,?,?,?,datetime('now'))
        """, (uid, tday, idx, d1, d2, total))
        conn.commit()

        _upsert_daily_weekly(uid, total)

        resp = {
            "d1": d1, "d2": d2, "total": total,
            "roll_index": idx,
            "rolls_left": MAX_DAILY - idx,
            "daily_limit": MAX_DAILY,
        }
        if idem_key:
            _save_idempo(conn, uid, idem_key, resp); conn.commit()

    return resp

@fastapi_app.get("/leaderboard/daily")
async def dice_leaderboard_daily(limit: int = 20, x_tg_id: Optional[str] = Header(None)):
    tday = _today_utc_str()
    viewer = _resolve_user_id(x_tg_id)
    with _db() as conn:
        top = conn.execute("""
            SELECT telegram_id, total_score FROM daily_totals
            WHERE date_utc=? ORDER BY total_score DESC, telegram_id ASC LIMIT ?
        """, (tday, limit)).fetchall()
        rows = conn.execute("""
            SELECT telegram_id, total_score FROM daily_totals
            WHERE date_utc=? ORDER BY total_score DESC, telegram_id ASC
        """, (tday,)).fetchall()
    leaderboard = [{"rank": i+1, "user": str(uid), "score": sc} for i,(uid,sc) in enumerate(top)]
    your_rank = next((i+1 for i,(uid,_) in enumerate(rows) if uid==viewer), None)
    your_score = next((sc for uid,sc in rows if uid==viewer), 0)
    return {"date": tday, "leaderboard": leaderboard, "your_rank": your_rank, "your_score": your_score}

@fastapi_app.get("/leaderboard/weekly")
async def dice_leaderboard_weekly(limit: int = 20, x_tg_id: Optional[str] = Header(None)):
    wk = _week_id()
    viewer = _resolve_user_id(x_tg_id)
    with _db() as conn:
        top = conn.execute("""
            SELECT telegram_id, total_score FROM weekly_totals
            WHERE week_id=? ORDER BY total_score DESC, telegram_id ASC LIMIT ?
        """, (wk, limit)).fetchall()
        rows = conn.execute("""
            SELECT telegram_id, total_score FROM weekly_totals
            WHERE week_id=? ORDER BY total_score DESC, telegram_id ASC
        """, (wk,)).fetchall()
    leaderboard = [{"rank": i+1, "user": str(uid), "score": sc} for i,(uid,sc) in enumerate(top)]
    your_rank = next((i+1 for i,(uid,_) in enumerate(rows) if uid==viewer), None)
    your_score = next((sc for uid, sc in rows if uid == viewer), 0)
    return {"week_id": wk, "leaderboard": leaderboard, "your_rank": your_rank, "your_score": your_score}

# ─────────────────────────────────────────────
# Wallet API (server-to-server)
class WalletIn(BaseModel):
    telegram_id: int
    username: Optional[str] = ""
    wallet_address: str

def require_api_key(x_api_key: Optional[str] = Header(None)):
    expected = os.getenv("WEBAPP_API_KEY", "").strip()
    if not expected:
        return  # dev mode: allow
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")

@fastapi_app.post("/api/wallets")
async def save_wallet(payload: WalletIn, _=Depends(require_api_key)):
    w = (payload.wallet_address or "").strip()
    if not (w and len(w) >= 10):
        raise HTTPException(status_code=400, detail="wallet_address looks invalid")
    try:
        cache_user_wallet(
            user_id=payload.telegram_id,
            username=(payload.username or "").strip(),
            wallet=w,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

@fastapi_app.get("/api/wallets/{telegram_id}")
async def get_wallet(telegram_id: int, _=Depends(require_api_key)):
    try:
        w = get_cached_wallet(telegram_id)
        return {"telegram_id": telegram_id, "wallet_address": w}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

# ─────────────────────────────────────────────
# PTB Application & webhooks
application = build_application()  # PTB Application instance

@fastapi_app.on_event("startup")
async def _on_startup():
    await application.initialize()
    await application.start()
    if PUBLIC_URL:
        try:
            await application.bot.set_webhook(
                url=f"{PUBLIC_URL}/webhook",
                secret_token=(TELEGRAM_WEBHOOK_SECRET or None),
                drop_pending_updates=True,
            )
            print(f"✅ Webhook set to {PUBLIC_URL}/webhook")
        except Exception as e:
            print(f"⚠️ Failed to set webhook: {e}")
    else:
        print("⚠️ PUBLIC_URL not set; webhook will not be configured.")

@fastapi_app.on_event("shutdown")
async def _on_shutdown():
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        pass

@fastapi_app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

# ─────────────────────────────────────────────
# Local dev entrypoint
if __name__ == "__main__":
    import uvicorn
    # IMPORTANT: module path must match your file location (New/main.py → "New.main")
    uvicorn.run("New.main:fastapi_app", host="0.0.0.0", port=PORT, reload=False)




