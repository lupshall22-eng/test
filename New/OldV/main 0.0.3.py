import os, time, json, requests
from dotenv import load_dotenv
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ── Simple in-memory stores
USER_ADDRESS: dict[int, str] = {}      # telegram_user_id -> address (after /connect)
USER_COLLECTION: dict[int, str] = {}   # telegram_user_id -> collectionId (after /setcollection)

# ── Load config
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENJIN_API = os.getenv("ENJIN_GRAPHQL", "https://platform.enjin.io/graphql")
ENJIN_API_KEY = os.getenv("ENJIN_API_KEY")

if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
if not ENJIN_API_KEY:
    raise SystemExit("Missing ENJIN_API_KEY in .env")

# ── Load collection names from collections.json
try:
    import json


    with open("collections.json", "r", encoding="utf-8") as f:
        COLLECTION_NAMES = json.load(f)
except FileNotFoundError:
    COLLECTION_NAMES = {}
    print("⚠️ collections.json not found — /findcollection will not work until you create it.")

# ── Enjin GraphQL helper
def enjin_graphql(query: str, variables: dict | None = None) -> dict:
    try:
        r = requests.post(
            ENJIN_API,
            json={"query": query, "variables": variables or {}},
            headers={
                "Authorization": ENJIN_API_KEY,  # raw token
                "Content-Type": "application/json",
            },
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

# ── Helpers
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

def get_collection_token_ids(collection_id: str, page_cap: int = 1000) -> list[str]:
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

# ── Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = "/connect - Link wallet\n" \
           "/findcollection <name> - Search collection by name\n" \
           "/setcollection <id> - Manually set collection\n" \
           "/collections - Show progress in collection\n" \
           "/mycollections - List all owned collections\n" \
           "/mywallet - Show linked wallet\n" \
           "/debugwallet - Debug wallet token accounts\n" \
           "/debugcollection <id> - Debug a collection"
    await update.message.reply_text("Welcome! Here are the commands:\n" + cmds)

async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                f"✅ Wallet connected: {addr}\nYou can now /findcollection or /setcollection."
            )
            return
        time.sleep(1)
    await update.message.reply_text("Still waiting for verification… try /connect again if needed.")

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
    name = COLLECTION_NAMES.get(cid, cid)
    await update.message.reply_text(f"📚 Collection set to {name} ({cid}). Now run /collections.")

async def collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    lines = [f"Collection {COLLECTION_NAMES.get(cid, cid)} ({cid}): {len(have)}/{total} tokens ({pct}%)."]
    for tid in all_ids[:cap]:
        mark = "✅" if tid in have_set else "❌"
        lines.append(f"{mark} Token #{tid}")
    if len(all_ids) > cap:
        lines.append(f"…and {len(all_ids) - cap} more.")
    await update.message.reply_text("\n".join(lines))

async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("No wallet linked. Use /connect.")
        return
    await update.message.reply_text(f"🔎 Address: {addr}\n🌐 Endpoint: {ENJIN_API}")

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        lines.append(f"• {COLLECTION_NAMES.get(cid, cid)} ({cid}) — {cnt} tokens")
    await update.message.reply_text("\n".join(lines))

# ── New: findcollection
async def findcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = (update.message.text or "").split(maxsplit=1)
    if len(term) < 2:
        await update.message.reply_text("Usage: /findcollection <search term>")
        return
    term = term[1].lower()

    matches = [(c["id"], c["name"]) for c in COLLECTION_NAMES if term in c["name"].lower()]

    if not matches:
        await update.message.reply_text("No collections found.")
        return

    lines = [f"• {cid} — {name}" for cid, name in matches[:20]]
    await update.message.reply_text("\n".join(lines))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("setcol:"):
        cid = query.data.split(":", 1)[1]
        uid = query.from_user.id
        USER_COLLECTION[uid] = cid
        await query.edit_message_text(
            f"📚 Collection set to {COLLECTION_NAMES.get(cid, cid)} ({cid}). Now run /collections."
        )

# ── Main
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("mycollections", mycollections))
    app.add_handler(CommandHandler("findcollection", findcollection))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot starting… Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
