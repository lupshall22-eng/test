import os
import time
import json
import requests
import sqlite3
import random
import asyncio
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# In-memory + persisted state
USER_ADDRESS: dict[int, str] = {}
USER_COLLECTION: dict[int, str] = {}
AWAITING_FIND_FLAG = "awaiting_find_term"

# Paging config
PAGE_SIZE = 8            # Find results per page
OWNED_PAGE_SIZE = 10     # Owned collections per page
PROGRESS_PAGE_SIZE = 20  # Tokens per page in progress view

# Timezone (for dice day/week boundaries)
TZ = ZoneInfo("Europe/London")

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

# ─────────────────────────────────────────────
# Collections lookup
COLLECTIONS_PATH = "collections.json"
try:
    with open(COLLECTIONS_PATH, "r", encoding="utf-8") as f:
        COLLECTION_NAMES: list[dict] = json.load(f)
except FileNotFoundError:
    COLLECTION_NAMES = []
COLLECTION_NAME_MAP: dict[str, str] = {
    c["id"]: c["name"] for c in COLLECTION_NAMES if "id" in c and "name" in c
}

# ─────────────────────────────────────────────
# Simple JSON persistence for UI state
STATE_PATH = Path("data/state.json")
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    # Warm in-memory maps from state
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
        STATE_PATH.write_text(
            json.dumps(STATE, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
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
# Small utilities
MAX_CHUNK = 3500  # margin for safety

async def safe_reply(update: Update, text: str, reply_markup=None):
    """Split long plain-text messages into safe chunks."""
    if getattr(update, "message", None) is None:
        if getattr(update, "callback_query", None):
            await update.callback_query.edit_message_text(text[:4096], reply_markup=reply_markup)
        return
    if len(text) <= MAX_CHUNK:
        return await update.message.reply_text(text, reply_markup=reply_markup)
    lines = text.split("\n")
    buf, cur = [], 0
    first = True
    for ln in lines:
        if cur + len(ln) + 1 > MAX_CHUNK:
            await update.message.reply_text("\n".join(buf), reply_markup=(reply_markup if first else None))
            first = False
            buf, cur = [], 0
        buf.append(ln)
        cur += len(ln) + 1
    if buf:
        await update.message.reply_text("\n".join(buf))

async def edit_or_send(update: Update, text: str, reply_markup=None):
    """Try editing the existing message; fall back to sending a new one."""
    if getattr(update, "callback_query", None):
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            return
        except Exception:
            pass
    await safe_reply(update, text, reply_markup=reply_markup)

# ─────────────────────────────────────────────
# Enjin GraphQL
def enjin_graphql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        ENJIN_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": ENJIN_API_KEY, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(str(data["errors"]))
    return data["data"]

# Enjin helpers
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

def get_collection_token_ids(collection_id: str, page_cap: int = 20000) -> list[str]:
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
    all_ids, after = [], None
    while True:
        d = enjin_graphql(q, {"cid": int(collection_id), "after": after})["GetCollection"]["tokens"]
        all_ids.extend([str(edge["node"]["tokenId"]) for edge in d["edges"]])
        if not d["pageInfo"]["hasNextPage"] or len(all_ids) >= page_cap:
            break
        after = d["pageInfo"]["endCursor"]
    return all_ids

# Sorting / filtering
def sort_token_ids(ids: list[str]) -> list[str]:
    def keyfn(s: str):  # numeric first, then lexicographic
        return (0, int(s)) if s.isdigit() else (1, s)
    return sorted(ids, key=keyfn)

def filter_ids(ids: list[str], have_set: set[str], mode: str) -> list[str]:
    if mode == "missing":
        return [t for t in ids if t not in have_set]
    if mode == "owned":
        return [t for t in ids if t in have_set]
    return ids

def next_mode(mode: str) -> str:
    return {"all": "missing", "missing": "owned", "owned": "all"}.get(mode or "all", "all")

# ─────────────────────────────────────────────
# Reply keyboard (+ Dice)
def show_main_keyboard(update: Update, text: str = "What would you like to do?"):
    kb = [
        [KeyboardButton("🔗 Connect wallet"), KeyboardButton("🔎 Find collection")],
        [KeyboardButton("📈 My collections")],
        [KeyboardButton("🎲 Dice")],
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
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="find:prev"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data="find:next"))
    if nav:
        rows.append(nav)
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
    # Persist view
    uid = update.effective_user.id
    u = user_state(uid)
    u["last_view"] = "find"
    u["find"] = {"term": term, "matches": matches, "page": page}
    save_state()

def build_owned_keyboard(rows_in: list[tuple[str, int]], page: int) -> InlineKeyboardMarkup:
    total = len(rows_in)
    start, end = page * OWNED_PAGE_SIZE, min((page + 1) * OWNED_PAGE_SIZE, total)
    rows = []
    for cid, cnt in rows_in[start:end]:
        label = COLLECTION_NAME_MAP.get(cid, cid)
        rows.append([InlineKeyboardButton(f"{label} ({cid}) — {cnt}", callback_data=f"owned:set:{cid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="owned:prev"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data="owned:next"))
    if nav:
        rows.append(nav)
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
    # Persist view
    uid = update.effective_user.id
    u = user_state(uid)
    u["last_view"] = "owned"
    u["owned"] = {"rows": rows, "page": page}
    save_state()

def build_progress_keyboard(from_find: bool = False, from_owned: bool = False) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("⬅️ Prev", callback_data="prog:prev"),
        InlineKeyboardButton("Next ➡️", callback_data="prog:next"),
        InlineKeyboardButton("🔁 Toggle View", callback_data="prog:toggle"),
    ]
    row2 = []
    if from_find:
        row2.append(InlineKeyboardButton("⬅️ Back to results", callback_data="prog:back"))
    if from_owned:
        row2.append(InlineKeyboardButton("⬅️ Back to owned list", callback_data="prog:back_owned"))
    row2.append(InlineKeyboardButton("❌ Close", callback_data="prog:close"))
    return InlineKeyboardMarkup([row1, row2])

async def render_progress_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s = context.user_data.get("progress") or {}
    cid = s.get("cid") or ""
    name = s.get("name") or cid
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
        page = max(0, total_pages - 1)
        s["page"] = page

    start, end = page * PROGRESS_PAGE_SIZE, min((page + 1) * PROGRESS_PAGE_SIZE, total)
    lines = [("✅" if tid in have_set else "❌") + f" Token #{tid}" for tid in ids[start:end]]
    mode_label = {"all": "All tokens", "missing": "Only missing", "owned": "Only owned"}[mode]
    header = (
        f"{name} ({cid}) — {have_count}/{total_all} owned ({overall_pct}%)\n"
        f"View: {mode_label} • Page {page+1}/{total_pages}\n"
    )
    text = header + ("\n".join(lines) if lines else "(No tokens in this view.)")
    kb = build_progress_keyboard(s.get("from_find", False), s.get("from_owned", False))

    if edit and getattr(update, "callback_query", None):
        await edit_or_send(update, text, reply_markup=kb)
    else:
        await safe_reply(update, text, reply_markup=kb)

    # Persist view
    uid = update.effective_user.id
    u = user_state(uid)
    u["last_view"] = "progress"
    prog_copy = {**s, "have": sorted(list(s.get("have", set())))}  # JSON-safe copy
    u["progress"] = prog_copy
    if s.get("cid"):
        u["collection"] = s["cid"]
        USER_COLLECTION[uid] = s["cid"]
    save_state()

# ─────────────────────────────────────────────
# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = user_state(uid)

    # Restore last view if available
    last = u.get("last_view")
    if last == "progress" and u.get("progress"):
        p = dict(u["progress"]); p["have"] = set(p.get("have", []))
        context.user_data["progress"] = p
        await render_progress_page(update, context, edit=False); return
    if last == "find" and u.get("find"):
        context.user_data["find"] = u["find"]
        await render_find_page(update, context, edit=False); return
    if last == "owned" and u.get("owned"):
        context.user_data["owned"] = u["owned"]
        await render_owned_page(update, context, edit=False); return

    # Otherwise show main menu
    msg = (
        "/connect – Link wallet\n"
        "/findcollection <name> – Search by name\n"
        "/setcollection <id> – Manually set collection\n"
        "/collections – Show progress\n"
        "/mycollections – List owned collections\n"
        "/mywallet – Show wallet\n"
        "/disconnect – Forget saved wallet\n"
        "/dice – Open the dice game"
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
            u = user_state(update.effective_user.id)
            u["address"] = addr
            save_state()
            # Persist in SQLite users table for prizes later
            db_upsert_wallet(update.effective_user.id, addr)
            await update.message.reply_text("✅ Wallet connected. Use 🔎 Find collection or /findcollection.")
            return
        time.sleep(1)
    await update.message.reply_text("Still waiting… run /connect again if needed.")

async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ADDRESS.pop(uid, None)
    u = user_state(uid)
    u["address"] = None
    save_state()
    db_upsert_wallet(uid, None)  # clear wallet in SQLite
    await update.message.reply_text("🔌 Disconnected. I won't remember your wallet address anymore.")

async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = USER_ADDRESS.get(update.effective_user.id)
    if not addr:
        await update.message.reply_text("No wallet linked. Use /connect.")
        return
    await update.message.reply_text(
        f"🔎 Address: {addr}\n"
        f"🌐 Endpoint: {ENJIN_API}"
    )

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = USER_ADDRESS.get(update.effective_user.id)
    if not addr:
        await update.message.reply_text("Use /connect first.")
        return

    counts: dict[str, int] = {}
    for e in fetch_all_token_accounts(addr):
        n = e["node"]
        if int(n.get("balance") or 0) + int(n.get("reservedBalance") or 0) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            counts[cid] = counts.get(cid, 0) + 1

    if not counts:
        await update.message.reply_text("No tokens found in wallet.")
        return

    owned_rows = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    context.user_data["owned"] = {"rows": owned_rows, "page": 0}
    await render_owned_page(update, context, edit=False)

async def setcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USER_ADDRESS:
        await update.message.reply_text("Use /connect first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setcollection <collectionId>")
        return
    cid = context.args[0].strip()
    USER_COLLECTION[uid] = cid
    u = user_state(uid)
    u["collection"] = cid
    save_state()
    await update.message.reply_text(f"📚 Collection set to {COLLECTION_NAME_MAP.get(cid, cid)} ({cid}). Now run /collections.")

async def collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    cid = USER_COLLECTION.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first.")
        return
    if not cid:
        await update.message.reply_text("Set a collection first with 🔎 Find collection or /setcollection.")
        return

    try:
        all_ids = get_collection_token_ids(cid)
    except Exception as e:
        await update.message.reply_text("Could not fetch collection.\n" + str(e))
        return
    if not all_ids:
        await update.message.reply_text("No tokens found in that collection.")
        return

    sorted_ids = sort_token_ids(all_ids)
    have_set = get_wallet_owned_by_collection(addr).get(cid, set())

    context.user_data["progress"] = {
        "cid": cid,
        "name": COLLECTION_NAME_MAP.get(cid, cid),
        "ids": sorted_ids,
        "have": set(have_set),
        "page": 0,
        "mode": "all",  # all | missing | owned
    }
    await render_progress_page(update, context, edit=False)

# ─────────────────────────────────────────────
# Search command (paged results)
async def findcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text("Type a name or part of a name to search:", reply_markup=ReplyKeyboardRemove())
        return
    term = " ".join(context.args).lower().strip()
    matches = [(c["id"], c["name"]) for c in COLLECTION_NAMES if term in c.get("name", "").lower()]
    if not matches:
        await show_main_keyboard(update, "No collections matched. Try again or tap a button.")
        return
    context.user_data["find"] = {"term": term, "matches": matches, "page": 0}
    await render_find_page(update, context, edit=False)

# Reply-keyboard taps (+ Dice)
async def on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "")
    text = raw.strip()
    norm = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).lower()
    if "connect wallet" in norm:
        await connect(update, context); return
    if "find collection" in norm:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text("Type a name or part of a name to search:", reply_markup=ReplyKeyboardRemove())
        return
    if "my collections" in norm:
        await mycollections(update, context); return
    if "dice" in norm:
        await dice(update, context); return

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
# Inline button handler (Find / Owned / Progress)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    data = q.data or ""

    # Find paging/close
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
                u["last_view"] = None
                save_state()
            await edit_or_send(update, "Search closed.")
            await show_main_keyboard(update, "What would you like to do next?")
            return
        return

    # Owned paging/select/close
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
                u["last_view"] = None
                save_state()
            await edit_or_send(update, "Owned list closed.")
            await show_main_keyboard(update, "What would you like to do next?")
            return
        if data.startswith("owned:set:"):
            cid = data.split(":", 2)[2]
            USER_COLLECTION[q.from_user.id] = cid
            label = COLLECTION_NAME_MAP.get(cid, cid)
            u = user_state(q.from_user.id)
            u["collection"] = cid
            save_state()

            addr = USER_ADDRESS.get(q.from_user.id)
            if not addr:
                await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
                await show_main_keyboard(update, "Link a wallet to view progress.")
                return
            try:
                all_ids = get_collection_token_ids(cid)
            except Exception as e:
                await edit_or_send(update, "Could not fetch collection.\n" + str(e))
                return
            if not all_ids:
                await edit_or_send(update, "No tokens found in that collection.")
                return
            sorted_ids = sort_token_ids(all_ids)
            have_set = get_wallet_owned_by_collection(addr).get(cid, set())
            context.user_data["progress"] = {
                "cid": cid,
                "name": label,
                "ids": sorted_ids,
                "have": set(have_set),
                "page": 0,
                "mode": "all",
                "from_owned": True,
            }
            await render_progress_page(update, context, edit=True)
            return
        return

    # Progress paging/toggle/close/back
    if data.startswith("prog:"):
        s = context.user_data.get("progress") or {}
        page = int(s.get("page") or 0)
        mode = s.get("mode") or "all"
        ids = s.get("ids") or []
        have = s.get("have") or set()
        filtered_total = len(filter_ids(ids, have, mode))

        if data == "prog:prev" and page > 0:
            s["page"] = page - 1; context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:next" and (page + 1) * PROGRESS_PAGE_SIZE < filtered_total:
            s["page"] = page + 1; context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:toggle":
            s["mode"] = next_mode(mode)
            s["page"] = 0
            context.user_data["progress"] = s
            await render_progress_page(update, context, edit=True); return
        if data == "prog:back":  # back to Find results
            find_state = context.user_data.get("find") or user_state(q.from_user.id).get("find")
            if find_state:
                context.user_data["find"] = find_state
                await render_find_page(update, context, edit=True)
            return
        if data == "prog:back_owned":  # back to Owned list
            owned_state = context.user_data.get("owned") or user_state(q.from_user.id).get("owned")
            if owned_state:
                context.user_data["owned"] = owned_state
                await render_owned_page(update, context, edit=True)
            return
        if data == "prog:close":
            context.user_data.pop("progress", None)
            u = user_state(q.from_user.id)
            if u.get("last_view") == "progress":
                u["last_view"] = None
                save_state()
            await edit_or_send(update, "Progress closed.")
            await show_main_keyboard(update, "What would you like to do next?")
            return
        return

    # Selecting from Find results (set collection) — jump straight into Progress
    if data.startswith("setcol:"):
        cid = data.split(":", 1)[1]
        USER_COLLECTION[q.from_user.id] = cid
        label = COLLECTION_NAME_MAP.get(cid, cid)
        u = user_state(q.from_user.id)
        u["collection"] = cid
        save_state()

        addr = USER_ADDRESS.get(q.from_user.id)
        if not addr:
            await edit_or_send(update, f"📚 Collection set to {label} ({cid}). Now /connect to link a wallet.")
            await show_main_keyboard(update, "Link a wallet to view progress.")
            return
        try:
            all_ids = get_collection_token_ids(cid)
        except Exception as e:
            await edit_or_send(update, "Could not fetch collection.\n" + str(e))
            return
        if not all_ids:
            await edit_or_send(update, "No tokens found in that collection.")
            return
        sorted_ids = sort_token_ids(all_ids)
        have_set = get_wallet_owned_by_collection(addr).get(cid, set())
        context.user_data["progress"] = {
            "cid": cid,
            "name": label,
            "ids": sorted_ids,
            "have": set(have_set),
            "page": 0,
            "mode": "all",
            "from_find": True,
        }
        await render_progress_page(update, context, edit=True)

# ─────────────────────────────────────────────
# 🎲 Dice Game — 2d6 with 4s cooldown, live countdown, SQLite persistence
DICE_DB = "ect.db"
DICE_MAX_ROLLS_PER_DAY = 50
ROLL_COOLDOWN = 4  # seconds
DICE_MSG_KEY = "dice_msg"

def db_conn():
    conn = sqlite3.connect(DICE_DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    # users table (wallet for prizes)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        wallet  TEXT
    );
    """)
    # rolls table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS dice_rolls (
        user_id INTEGER NOT NULL,
        ts      INTEGER NOT NULL,
        day     TEXT    NOT NULL,  -- YYYY-MM-DD (Europe/London)
        week    INTEGER NOT NULL,  -- ISO week number
        year    INTEGER NOT NULL,  -- ISO week year
        value   INTEGER NOT NULL,  -- 2..12 (2d6 total)
        PRIMARY KEY (user_id, ts)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rolls_day_user ON dice_rolls(day, user_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rolls_week_user ON dice_rolls(year, week, user_id);")
    conn.commit()
    conn.close()

def db_upsert_wallet(user_id: int, wallet: str | None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, wallet) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET wallet=excluded.wallet",
        (user_id, wallet)
    )
    conn.commit()
    conn.close()

def _now() -> datetime:
    return datetime.now(TZ)

def _today_str() -> str:
    return _now().date().isoformat()

def _week_year():
    iso = _now().date().isocalendar()
    return iso.year, iso.week

def last_roll_ts(user_id: int) -> int | None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(ts) FROM dice_rolls WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row and row[0] is not None else None

def can_roll(user_id: int) -> tuple[bool, int]:
    """Returns (allowed, remaining_seconds)."""
    lr = last_roll_ts(user_id)
    if lr is None:
        return True, 0
    now = int(_now().timestamp())
    elapsed = now - lr
    if elapsed >= ROLL_COOLDOWN:
        return True, 0
    return False, ROLL_COOLDOWN - elapsed

def rolls_used_today(user_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dice_rolls WHERE user_id=? AND day=?", (user_id, _today_str()))
    n = int(cur.fetchone()[0] or 0)
    conn.close()
    return n

def insert_roll(user_id: int, total: int):
    ts = int(_now().timestamp())
    day = _today_str()
    yr, wk = _week_year()
    conn = db_conn()
    conn.execute(
        "INSERT INTO dice_rolls (user_id, ts, day, week, year, value) VALUES (?,?,?,?,?,?)",
        (user_id, ts, day, wk, yr, total)
    )
    conn.commit()
    conn.close()

def daily_total(user_id: int) -> int:
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT SUM(value) FROM dice_rolls WHERE user_id=? AND day=?", (user_id, _today_str()))
    s = int(cur.fetchone()[0] or 0)
    conn.close()
    return s

def weekly_total(user_id: int) -> int:
    yr, wk = _week_year()
    conn = db_conn(); cur = conn.cursor()
    cur.execute("SELECT SUM(value) FROM dice_rolls WHERE user_id=? AND year=? AND week=?", (user_id, yr, wk))
    s = int(cur.fetchone()[0] or 0)
    conn.close()
    return s

def daily_leaderboard(limit: int = 10) -> list[tuple[int, int]]:
    conn = db_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT user_id, SUM(value) AS total
        FROM dice_rolls
        WHERE day=?
        GROUP BY user_id
        ORDER BY total DESC, user_id ASC
        LIMIT ?
    """, (_today_str(), limit))
    rows = [(int(u), int(t)) for (u, t) in cur.fetchall()]
    conn.close()
    return rows

def weekly_leaderboard(limit: int = 10) -> list[tuple[int, int]]:
    yr, wk = _week_year()
    conn = db_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT user_id, SUM(value) AS total
        FROM dice_rolls
        WHERE year=? AND week=?
        GROUP BY user_id
        ORDER BY total DESC, user_id ASC
        LIMIT ?
    """, (yr, wk, limit))
    rows = [(int(u), int(t)) for (u, t) in cur.fetchall()]
    conn.close()
    return rows

def rank_in_rows(rows: list[tuple[int, int]], user_id: int) -> int | None:
    for i, (u, _) in enumerate(rows, start=1):
        if u == user_id:
            return i
    return None

def progress_bar(used: int) -> str:
    blocks = 10
    filled = min(blocks, used // 5)  # 5 rolls per block (50/day)
    return "▮" * filled + "▯" * (blocks - filled)

def badge_for_total(total: int) -> str:
    # 2d6: notable totals
    if total == 12: return "💎 Natural 12"
    if total == 11: return "🔥 High"
    if total == 2:  return "💀 Snake eyes"
    if total >= 9:  return "✨ Nice"
    return "🎲"

def dice_keyboard(ready: bool = True, remaining: int = 0) -> InlineKeyboardMarkup:
    if ready:
        roll_btn = InlineKeyboardButton("🎲 Roll 2d6", callback_data="dice:roll")
    else:
        roll_btn = InlineKeyboardButton(f"🎲 Roll (⏳ {remaining}s)", callback_data="dice:wait")

    rows = [
        [roll_btn],
        [
            InlineKeyboardButton("📅 Daily",  callback_data="dice:lb:daily"),
            InlineKeyboardButton("🗓 Weekly", callback_data="dice:lb:weekly"),
            InlineKeyboardButton("🏅 My Rank", callback_data="dice:rank"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="dice:close")]
    ]
    return InlineKeyboardMarkup(rows)

def dice_panel_text(user_id: int, last: tuple[int,int,int] | None = None) -> str:
    used = rolls_used_today(user_id)
    bar = progress_bar(used)
    d_tot = daily_total(user_id)
    w_tot = weekly_total(user_id)

    lines = []
    if last is not None:
        d1, d2, total = last
        lines.append(f"{badge_for_total(total)}  You rolled {d1} + {d2} = <b>{total}</b>")
        lines.append("")
    lines.append("🎲 <b>Daily Dice (2d6)</b>")
    lines.append(f"Rolls today: {used}/{DICE_MAX_ROLLS_PER_DAY}  {bar}")
    lines.append(f"Daily total: <b>{d_tot}</b>")
    lines.append(f"Weekly total: <b>{w_tot}</b>")
    return "\n".join(lines)

async def open_dice_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = dice_panel_text(uid, last=None)
    msg = await update.message.reply_html(txt, reply_markup=dice_keyboard(True))
    context.user_data[DICE_MSG_KEY] = {"chat_id": msg.chat.id, "message_id": msg.message_id}

async def dice_roll_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    allowed, remaining = can_roll(uid)

    used = rolls_used_today(uid)
    if used >= DICE_MAX_ROLLS_PER_DAY:
        txt = (
            "🎲 <b>Daily Dice (2d6)</b>\n"
            f"Rolls today: {used}/{DICE_MAX_ROLLS_PER_DAY}  {progress_bar(used)}\n"
            "<i>No rolls left. Come back tomorrow.</i>"
        )
        await update.callback_query.edit_message_text(txt, reply_markup=dice_keyboard(False, 0), parse_mode="HTML")
        return

    if not allowed:
        # live countdown on the button
        for r in range(remaining, 0, -1):
            try:
                await update.callback_query.edit_message_reply_markup(reply_markup=dice_keyboard(False, r))
            except Exception:
                pass
            await asyncio.sleep(1)
        # restore ready
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=dice_keyboard(True, 0))
        except Exception:
            pass
        return

    # Roll 2d6
    d1 = random.randint(1, 6)
    d2 = random.randint(1, 6)
    total = d1 + d2
    insert_roll(uid, total)

    # Update panel with result + start cooldown
    txt = dice_panel_text(uid, last=(d1, d2, total))
    try:
        await update.callback_query.edit_message_text(txt, reply_markup=dice_keyboard(False, ROLL_COOLDOWN), parse_mode="HTML")
    except Exception:
        # fallback: send fresh
        msg = await update.effective_message.reply_html(txt, reply_markup=dice_keyboard(False, ROLL_COOLDOWN))
        context.user_data[DICE_MSG_KEY] = {"chat_id": msg.chat.id, "message_id": msg.message_id}

    for r in range(ROLL_COOLDOWN, 0, -1):
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=dice_keyboard(False, r))
        except Exception:
            pass
        await asyncio.sleep(1)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=dice_keyboard(True))
    except Exception:
        pass

async def dice_daily_lb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = daily_leaderboard(10)
    if not rows:
        txt = "🏆 <b>Daily Leaderboard</b>\n<i>No rolls yet today. Be first.</i>"
    else:
        lines = ["🏆 <b>Daily Leaderboard</b>"]
        for i, (u, total) in enumerate(rows, start=1):
            name = "You" if u == update.effective_user.id else f"User {u}"
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else ""))
            lines.append(f"{i}. {name} — <b>{total}</b> {medal}")
        txt = "\n".join(lines)
    await update.callback_query.edit_message_text(txt, reply_markup=dice_keyboard(True), parse_mode="HTML")

async def dice_weekly_lb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = weekly_leaderboard(10)
    yr, wk = _week_year()
    if not rows:
        txt = f"🏆 <b>Weekly Leaderboard</b> (W{wk} {yr})\n<i>No rolls yet this week.</i>"
    else:
        lines = [f"🏆 <b>Weekly Leaderboard</b> (W{wk} {yr})"]
        lines.append("<i>Score = sum of all daily 2d6 totals</i>")
        for i, (u, total) in enumerate(rows, start=1):
            name = "You" if u == update.effective_user.id else f"User {u}"
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else ""))
            lines.append(f"{i}. {name} — <b>{total}</b> {medal}")
        txt = "\n".join(lines)
    await update.callback_query.edit_message_text(txt, reply_markup=dice_keyboard(True), parse_mode="HTML")

def encouragement(rank: int | None, scope_label: str) -> str:
    if rank is None: return f"{scope_label}: no rank yet — keep rolling."
    if rank <= 10:   return f"{scope_label}: #{rank} — on the board."
    if rank <= 50:   return f"{scope_label}: #{rank} — you’re close. Push!"
    return f"{scope_label}: #{rank} — steady luck wins."

async def dice_myrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d_tot = daily_total(uid)
    w_tot = weekly_total(uid)
    d_rows = daily_leaderboard(1000)
    w_rows = weekly_leaderboard(1000)
    d_rank = rank_in_rows(d_rows, uid)
    w_rank = rank_in_rows(w_rows, uid)
    lines = ["🏅 <b>My Rank</b>"]
    lines.append(f"📅 Today: total <b>{d_tot}</b> — {encouragement(d_rank, 'today')}")
    lines.append(f"🗓 This week: total <b>{w_tot}</b> — {encouragement(w_rank, 'this week')}")
    txt = "\n".join(lines)
    await update.callback_query.edit_message_text(txt, reply_markup=dice_keyboard(True), parse_mode="HTML")

# Public entry points
async def dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = dice_panel_text(update.effective_user.id, last=None)
    msg = await update.message.reply_html(txt, reply_markup=dice_keyboard(True))
    context.user_data[DICE_MSG_KEY] = {"chat_id": msg.chat.id, "message_id": msg.message_id}

async def dice_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    data = q.data or ""
    if data == "dice:roll":      await dice_roll_action(update, context); return
    if data == "dice:lb:daily":  await dice_daily_lb(update, context); return
    if data == "dice:lb:weekly": await dice_weekly_lb(update, context); return
    if data == "dice:rank":      await dice_myrank(update, context); return
    if data == "dice:wait":
        return
    if data == "dice:close":
        await q.edit_message_text("🎲 Dice closed."); return

# ─────────────────────────────────────────────
# App wiring
def main():
    # init SQLite for dice + users (auto-creates ect.db)
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("disconnect", disconnect))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("mycollections", mycollections))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections))
    app.add_handler(CommandHandler("findcollection", findcollection))

    # Dice
    app.add_handler(CommandHandler("dice", dice))
    app.add_handler(CallbackQueryHandler(dice_buttons, pattern=r"^dice:"))

    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(setcol:|find:|owned:|prog:)"))

    # Capture typed term after prompting for /findcollection
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_find_term), group=0)
    # Reply-keyboard taps (with Dice)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply_button), group=1)

    print("Bot starting… Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
