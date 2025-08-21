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
def get_wallet_owned_by_collection(address: str) -> dict[str, set[str]]:
    q = """
    query WalletTokens($account: String) {
      GetWallet(account: $account) {
        tokenAccounts {
          edges {
            node {
              balance
              token { tokenId collection { collectionId } }
            }
          }
        }
      }
    }
    """
    edges = enjin_graphql(q, {"account": address})["GetWallet"]["tokenAccounts"]["edges"]
    owned: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        node = e["node"]
        bal = int(node.get("balance") or 0)
        if bal > 0:
            cid = str(node["token"]["collection"]["collectionId"])  # normalize to str
            tid = str(node["token"]["tokenId"])                    # normalize to str
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

# ── App wiring
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect))
    app.add_handler(CommandHandler("setcollection", setcollection))
    app.add_handler(CommandHandler("collections", collections))
    print("Bot starting… Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
