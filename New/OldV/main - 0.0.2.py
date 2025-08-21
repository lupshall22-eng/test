import os, time
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict


# ── Simple in-memory stores
USER_ADDRESS: dict[int, str] = {}      # telegram_user_id -> address (after /connect)
USER_COLLECTION: dict[int, str] = {}   # telegram_user_id -> collectionId (after /setcollection)

# ── Config
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENJIN_API = os.getenv("ENJIN_GRAPHQL", "https://platform.enjin.io/graphql")
ENJIN_API_KEY = os.getenv("ENJIN_API_KEY")

if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
if not ENJIN_API_KEY:
    raise SystemExit("Missing ENJIN_API_KEY in .env")

# ── Enjin GraphQL helper (verbose error reporting)
def enjin_graphql(query: str, variables: dict | None = None) -> dict:
    try:
        r = requests.post(
            ENJIN_API,
            json={"query": query, "variables": variables or {}},
            headers={
                "Authorization": ENJIN_API_KEY,  # raw token, no "Bearer "
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

# ── Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Use /connect to link your Enjin wallet.")

async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) Get QR + verificationId (QUERY, no args)
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

    # 2) Poll verification (QUERY with variable)
    poll_q = """
    query GetAccountVerified($vid: String) {
      GetAccountVerified(verificationId: $vid) {
        verified
        account { address }
      }
    }
    """
    for _ in range(30):  # ~30s
        d = enjin_graphql(poll_q, {"vid": verification_id})["GetAccountVerified"]
        if d and d.get("verified"):
            addr = d["account"]["address"]
            USER_ADDRESS[update.effective_user.id] = addr
            await update.message.reply_text(
                f"✅ Wallet connected: {addr}\nYou can now set a collection with /setcollection <collectionId>."
            )
            return
        time.sleep(1)

    await update.message.reply_text("Still waiting for verification… try /connect again if needed.")

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
    from collections import defaultdict
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

# ── More commands
async def setcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USER_ADDRESS:
        await update.message.reply_text("Use /connect first to link your wallet.")
        return

    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /setcollection <collectionId>")
        return

    collection_id = parts[1].strip()
    if not collection_id.isdigit():
        await update.message.reply_text("CollectionId should be a number (BigInt). Try again.")
        return

    USER_COLLECTION[uid] = collection_id
    await update.message.reply_text(
        f"📚 Collection set to {collection_id}. Now run /collections to see your progress."
    )

async def collections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first to link your wallet.")
        return

    cid = USER_COLLECTION.get(uid)
    if not cid:
        await update.message.reply_text("Set a collection first: /setcollection <collectionId>")
        return

    # What user owns (by collection)
    owned_map = get_wallet_owned_by_collection(addr)
    have = owned_map.get(cid, set())

    try:
        all_ids = get_collection_token_ids(cid)
    except Exception as e:
        await update.message.reply_text(
            "I couldn’t read that collection (might need to Track it in Enjin Console).\n" + str(e)
        )
        return

    total = len(all_ids)
    if total == 0:
        await update.message.reply_text("That collection has no tokens or couldn’t be fetched.")
        return

    pct = round(100 * (len(have) / total), 2)

    have_set = set(have)
    cap = 120  # avoid Telegram message limit; paginate later if needed
    lines = []
    for tid in all_ids[:cap]:
        mark = "✅" if tid in have_set else "❌"
        lines.append(f"{mark} Token #{tid}")
    if len(all_ids) > cap:
        lines.append(f"…and {len(all_ids) - cap} more.")

    await update.message.reply_text(
        f"Collection {cid}: {len(have)}/{total} tokens ({pct}%).\n" + "\n".join(lines)
    )

async def mywallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    net = ENJIN_API
    if not addr:
        await update.message.reply_text("No wallet linked yet. Use /connect.")
        return
    await update.message.reply_text(f"🔎 Address: {addr}\n🌐 Endpoint: {net}")

async def debugwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a few token accounts from your wallet (collectionId, tokenId, balances)."""
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("No wallet linked. Use /connect.")
        return

    edges = fetch_all_token_accounts(addr)  # <-- use paginator
    lines = [f"Found {len(edges)} token accounts on wallet {addr}:"]
    for e in edges[:30]:  # show first 30
        n = e["node"]
        cid = n["token"]["collection"]["collectionId"]
        tid = n["token"]["tokenId"]
        bal = n.get("balance") or "0"
        rsv = n.get("reservedBalance") or "0"
        lines.append(f"• C{cid} T{tid}  balance={bal} reserved={rsv}")
    await update.message.reply_text("\n".join(lines))


async def debugcollection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show first 20 tokenIds from a collection."""
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /debugcollection <collectionId>")
        return
    cid = parts[1].strip()
    try:
        ids = get_collection_token_ids(cid)
    except Exception as e:
        await update.message.reply_text("Couldn’t fetch collection.\n" + str(e))
        return
    sample = ", ".join(ids[:20])
    await update.message.reply_text(f"Collection {cid} has {len(ids)} tokens.\nFirst 20: {sample}")

async def mycollections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    addr = USER_ADDRESS.get(uid)
    if not addr:
        await update.message.reply_text("Use /connect first to link your wallet.")
        return

    # Pull ALL token accounts via paginator
    edges = fetch_all_token_accounts(addr)

    # Aggregate counts per collection, counting balance + reservedBalance
    counts: dict[str, int] = {}
    for e in edges:
        n = e["node"]
        bal = int(n.get("balance") or 0)
        rsv = int(n.get("reservedBalance") or 0)
        if (bal + rsv) > 0:
            cid = str(n["token"]["collection"]["collectionId"])
            counts[cid] = counts.get(cid, 0) + 1

    if not counts:
        await update.message.reply_text("I didn’t find any tokens on this wallet yet.")
        return

    # Nicely sorted: most tokens first
    lines = [f"Owned collections for {addr} (showing all):"]
    for cid, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"• {cid}  ({cnt} tokens)")
    lines.append("\nUse /setcollection <collectionId> and then /collections.")
    await update.message.reply_text("\n".join(lines))


# ── App wiring
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections))
    app.add_handler(CommandHandler("mywallet", mywallet))
    app.add_handler(CommandHandler("debugwallet", debugwallet))
    app.add_handler(CommandHandler("debugcollection", debugcollection))
    app.add_handler(CommandHandler("mycollections", mycollections))
    print("Bot starting… Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
