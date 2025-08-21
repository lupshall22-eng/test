
import os, time, json, logging, requests
from dotenv import load_dotenv
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# Logging (DEBUG)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger("ECT")

# ──────────────────────────────────────────────────────────────────────────────
# Simple in-memory state
USER_ADDRESS: dict[int, str] = {}      # telegram_user_id -> address (after /connect)
USER_COLLECTION: dict[int, str] = {}   # telegram_user_id -> collectionId (after selection)
AWAITING_FIND_FLAG = "awaiting_find_term"

# ──────────────────────────────────────────────────────────────────────────────
# Config
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENJIN_API = os.getenv("ENJIN_GRAPHQL", "https://platform.enjin.io/graphql")
ENJIN_API_KEY = os.getenv("ENJIN_API_KEY")

if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
if not ENJIN_API_KEY:
    raise SystemExit("Missing ENJIN_API_KEY in .env")

# ──────────────────────────────────────────────────────────────────────────────
# Load collection names (LIST of {"id","name"})
COLLECTIONS_PATH = "collections.json"  # your JSON file (list of objects)
try:
    with open(COLLECTIONS_PATH, "r", encoding="utf-8") as f:
        COLLECTION_NAMES: list[dict] = json.load(f)
    print(f"Loaded {len(COLLECTION_NAMES)} collection name entries from {COLLECTIONS_PATH}")
except FileNotFoundError:
    COLLECTION_NAMES = []
    print(f"⚠️ {COLLECTIONS_PATH} not found — name search will show nothing.")

# quick lookup map: id -> name
COLLECTION_NAME_MAP: dict[str, str] = {
    c.get("id"): c.get("name") for c in COLLECTION_NAMES if "id" in c and "name" in c
}

# ──────────────────────────────────────────────────────────────────────────────
# Enjin GraphQL helper
def enjin_graphql(query: str, variables: dict | None = None) -> dict:
    try:
        r = requests.post(
            ENJIN_API,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": ENJIN_API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            data = r.json()
        except Exception:
            data = None
        r.raise_for_status()
        if not data:
            raise RuntimeError("Empty response from Enjin API")
        if "errors" in data:
            raise RuntimeError(str(data["errors"]))
        return data["data"]
    except requests.HTTPError as e:
        body = data if isinstance(data, dict) else (r.text if 'r' in locals() else '')
        raise RuntimeError(f"HTTP {r.status_code} from Enjin: {body}") from e

# ──────────────────────────────────────────────────────────────────────────────
# Helpers (Enjin)
def fetch_all_token_accounts(address: str) -> list[dict]:
    """Return ALL tokenAccounts edges for a wallet (paginates)."""
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
    edges = []
    after = None
    while True:
        data = enjin_graphql(q, {"account": address, "after": after})
        ta = data["GetWallet"]["tokenAccounts"]
        edges.extend(ta["edges"])
        if not ta["pageInfo"]["hasNextPage"]:
            break
        after = ta["pageInfo"]["endCursor"]
    return edges

def get_wallet_owned_by_collection(address: str) -> dict[str, set[str]]:
    edges = fetch_all_token_accounts(address)
    owned: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        n = e["node"]
        bal = int(n.get("balance") or 0)
        rsv = int(n.get("reservedBalance") or 0)
        if (bal + rsv) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            tid = str(n["token"]["tokenId"])
            owned[cid].add(tid)
    return owned

def get_collection_token_ids(collection_id: str, page_cap: int = 2000) -> list[str]:
    q = """
    query GetCollectionTokens($cid: BigInt!, $after: String) {
      GetCollection(collectionId: $cid) {
        tokens(after: $after) {
          totalCount
          pageInfo { endCursor hasNextPage }
          edges { node { tokenId } }
        }
      }
    }
    """
    all_ids: list[str] = []
    after = None
    while True:
        data = enjin_graphql(q, {"cid": int(collection_id), "after": after})["GetCollection"]["tokens"]
        all_ids.extend([str(edge["node"]["tokenId"]) for edge in data["edges"]])
        if not data["pageInfo"]["hasNextPage"] or len(all_ids) >= page_cap:
            break
        after = data["pageInfo"]["endCursor"]
    return all_ids

# ──────────────────────────────────────────────────────────────────────────────
# UI helpers (Telegram)
def show_main_keyboard(update: Update, text: str = "What would you like to do?"):
    kb = [
        [KeyboardButton("🔗 Connect wallet"), KeyboardButton("🔎 Find collection")],
        [KeyboardButton("🗂 Browse"), KeyboardButton("📈 My collections")],
        [KeyboardButton("✅ Progress")]
    ]
    markup = ReplyKeyboardMarkup(kb, resize_keyboard=True, selective=True)
    if getattr(update, "message", None):
        return update.message.reply_text(text, reply_markup=markup)
    if getattr(update, "callback_query", None):
        return update.callback_query.message.reply_text(text, reply_markup=markup)

# ──────────────────────────────────────────────────────────────────────────────
# Error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("UNCAUGHT ERROR: %s", context.error)

# ──────────────────────────────────────────────────────────────────────────────
# Commands
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/ping from %s", update.effective_user.id if update and update.effective_user else "?")
    await update.message.reply_text("pong")

async def debugawait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = context.user_data.get(AWAITING_FIND_FLAG)
    await update.message.reply_text(f"awaiting_find_term = {val!r}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("start() called")
    cmds = (
        "/connect – Link wallet\n"
        "/findcollection <name> – Search by name\n"
        "/setcollection <id> – Manually set collection\n"
        "/collections – Show progress\n"
        "/mycollections – List owned collections\n"
        "/mywallet – Show wallet\n"
        "/ping – Test the bot\n"
        "/debugawait – Show find-await flag"
    )
    await show_main_keyboard(update, "Welcome! Tap a button or use a command.\n\n" + cmds)

async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("connect() called")
    q = """
    query RequestAccount {
      RequestAccount {
        qrCode
        verificationId
      }
    }
    """
    data = enjin_graphql(q)
    qr_url = data["RequestAccount"]["qrCode"]
    verification_id = data["RequestAccount"]["verificationId"]
    await update.message.reply_photo(qr_url, caption="Scan with your Enjin Wallet to link.")
    poll_q = """
    query GetAccountVerified($vid: String) {
      GetAccountVerified(verificationId: $vid) {
        verified
        account { address }
      }
    }
    """
    for _ in range(30):
        d = enjin_graphql(poll_q, {"vid": verification_id})["GetAccountVerified"]
        if d and d.get("verified"):
            addr = d["account"]["address"]
            USER_ADDRESS[update.effective_user.id] = addr
            await update.message.reply_text(
                f"✅ Wallet connected: {addr}\nYou can now tap “🔎 Find collection” or use /findcollection."
            )
            return
        time.sleep(1)
    await update.message.reply_text("Still waiting for verification… try /connect again if needed.")

async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("mywallet() called")
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("No wallet linked. Use /connect.")
        return
    await update.message.reply_text(f"🔎 Address: {addr}\n🌐 Endpoint: {ENJIN_API}")

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("mycollections() called")
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first.")
        return
    edges = fetch_all_token_accounts(addr)
    counts: dict[str, int] = {}
    for e in edges:
        n = e["node"]
        bal = int(n.get("balance") or 0)
        rsv = int(n.get("reservedBalance") or 0)
        if (bal + rsv) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            counts[cid] = counts.get(cid, 0) + 1
    if not counts:
        await update.message.reply_text("No tokens found in wallet.")
        return
    lines = [f"Owned collections for {addr}:"]
    for cid, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        label = COLLECTION_NAME_MAP.get(cid, cid)
        lines.append(f"• {label} ({cid}) — {cnt} tokens")
    await update.message.reply_text("\n".join(lines))

async def setcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("setcollection() called args=%r", getattr(context, "args", None))
    uid = update.effective_user.id
    if uid not in USER_ADDRESS:
        await update.message.reply_text("Use /connect first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setcollection <collectionId>")
        return
    cid = context.args[0].strip()
    USER_COLLECTION[uid] = cid
    await update.message.reply_text(f"📚 Collection set to {COLLECTION_NAME_MAP.get(cid, cid)} ({cid}). Now run /collections.")

async def collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("collections() called")
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    cid = USER_COLLECTION.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first.")
        return
    if not cid:
        await update.message.reply_text("Set a collection first with /findcollection or /setcollection.")
        return
    owned_map = get_wallet_owned_by_collection(addr)
    have = owned_map.get(cid, set())
    try:
        all_ids = get_collection_token_ids(cid)
    except Exception as e:
        await update.message.reply_text("Could not fetch collection.\n" + str(e))
        return
    total = len(all_ids)
    if total == 0:
        await update.message.reply_text("No tokens found in that collection.")
        return
    pct = round(100 * (len(have) / total), 2)
    have_set = set(have)
    cap = 120
    lines = [f"{COLLECTION_NAME_MAP.get(cid, cid)} ({cid}): {len(have)}/{total} tokens ({pct}%)."]
    for tid in all_ids[:cap]:
        mark = "✅" if tid in have_set else "❌"
        lines.append(f"{mark} Token #{tid}")
    if len(all_ids) > cap:
        lines.append(f"…and {len(all_ids) - cap} more.")
    await update.message.reply_text("\n".join(lines))

# ── Search-by-name command — supports no-arg prompt and inline button results
async def findcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("findcollection() args=%r", getattr(context, "args", None))
    # If no args, switch to prompt mode
    if not context.args:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text(
            "Type a name or part of a name to search:",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    term = " ".join(context.args).lower()
    logger.debug("[FIND] searching for: %s", term)
    matches = [(c["id"], c["name"]) for c in COLLECTION_NAMES if term in c.get("name", "").lower()]
    if not matches:
        await show_main_keyboard(update, "No collections matched that search. Try again or tap a button.")
        return

    # Results as clickable buttons
    keyboard = [[InlineKeyboardButton(f"{name} ({cid})", callback_data=f"setcol:{cid}")]
                for cid, name in matches[:40]]  # cap to avoid overflow
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a collection:", reply_markup=reply_markup)

# ── Reply-keyboard button tap handler (fuzzy matching for safety)
async def on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "")
    text = raw.strip()
    norm = "".join(ch for ch in text if ch.isalnum() or ch.isspace()).lower()
    logger.debug("on_reply_button() got text=%r norm=%r (awaiting=%s)", text, norm, context.user_data.get(AWAITING_FIND_FLAG))

    if "connect wallet" in norm:
        await connect(update, context)
        return

    if "find collection" in norm:
        context.user_data[AWAITING_FIND_FLAG] = True
        await update.message.reply_text(
            "Type a name or part of a name to search:",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if "browse" in norm:
        await mycollections(update, context)
        return

    if "my collections" in norm:
        await mycollections(update, context)
        return

    if norm in ("progress", "show progress", "collection progress"):
        await collections(update, context)
        return
    # otherwise ignore (regular text)

# ── After we prompt for a term, capture the next message and run search
async def capture_find_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("capture_find_term() called (awaiting=%s)", context.user_data.get(AWAITING_FIND_FLAG))
    if not context.user_data.get(AWAITING_FIND_FLAG):
        return
    term = (update.message.text or "").strip()
    context.user_data[AWAITING_FIND_FLAG] = False
    logger.debug("capture_find_term() captured term: %r", term)

    # Reuse /findcollection logic by passing args
    saved_args = getattr(context, "args", None)
    context.args = [term]
    try:
        await findcollection(update, context)
    finally:
        context.args = saved_args
    # We restore the keyboard after the user taps a result.

# ── Inline button handler for search results
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.debug("button_handler() data=%r", query.data if query else None)
    await query.answer()

    data = query.data or ""
    if data.startswith("setcol:"):
        cid = data.split(":", 1)[1]
        uid = query.from_user.id
        USER_COLLECTION[uid] = cid
        label = COLLECTION_NAME_MAP.get(cid, cid)
        await query.edit_message_text(f"📚 Collection set to {label} ({cid}).")
        await show_main_keyboard(update, "Collection set. Tap ✅ Progress or run /collections.")

# ──────────────────────────────────────────────────────────────────────────────
# App wiring
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("debugawait", debugawait))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("mycollections", mycollections))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections))
    app.add_handler(CommandHandler("findcollection", findcollection))

    # Inline results click
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^setcol:"))

    # IMPORTANT: capture runs first so it grabs the user's typed search term
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_find_term), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply_button), group=1)

    # Global error handler
    app.add_error_handler(on_error)

    logger.info("Starting bot (polling)…")
    app.run_polling()

if __name__ == "__main__":
    main()
