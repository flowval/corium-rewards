"""
Corium points tracker - Telegram bot
Command: /update  -> checks Hunters + Community boards for the tracked names
                     and replies with individual + combined SOL totals.

Run locally:
    pip install -r requirements.txt && playwright install chromium
    export TELEGRAM_BOT_TOKEN="123456:ABC..."     (from @BotFather)
    python corium_bot.py

Deploy on Render: see Dockerfile. On a Render web service a tiny health
endpoint answers 200 on $PORT so a pinger can keep the free instance awake.

Notes:
- corium.so/points is rendered by JavaScript, so a plain HTTP request returns
  an empty shell. This bot drives a real headless browser (Playwright).
- /debug lists the JSON endpoints the page calls. If you find the real API
  URL, scraping that directly is faster and more reliable than the DOM.
"""

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

URL = "https://corium.so/points"
NAMES = ["cosmi", "hnnicalls"]
BOARDS = {
    "Hunters": {"tab_re": r"^\s*Hunters.{0,2}\s*board\s*$", "hash": "#hunters"},
    "Community": {"tab_re": r"^\s*Community\s*board\s*$", "hash": "#community"},
}

# Matches e.g. "12.34 SOL", "1,234 SOL", "◎ 5.2", "5.2◎"
SOL_RE = re.compile(
    r"(?:(\d[\d,]*\.?\d*)\s*(?:SOL|◎))|(?:◎\s*(\d[\d,]*\.?\d*))", re.IGNORECASE
)

CACHE_FILE = Path(__file__).with_name("corium_cache.json")
REFRESH_MINUTES = int(os.environ.get("CHECK_MINUTES", "3"))  # auto-check interval

# Chat IDs that get change alerts. On Render set NOTIFY_CHAT_ID (comma separated);
# /notify also adds a chat but is forgotten on restart. If NOTIFY_CHAT_ID is set,
# only those chats may use the bot.
ENV_IDS = {int(x) for x in os.environ.get("NOTIFY_CHAT_ID", "").replace(" ", "").split(",") if x}
SUBSCRIBERS: set[int] = set(ENV_IDS)

# Guess at how "dust" appears in a row, e.g. "1,234 dust" or "dust: 1234".
# Send me the /debug row text if this doesn't match.
DUST_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*dust|dust\s*[:=]?\s*(\d[\d,]*\.?\d*)", re.IGNORECASE
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its URLs contain the bot token
log = logging.getLogger("corium")

# JS run inside the page: find rows that contain a given name and return their text
FIND_ROWS_JS = """
(name) => {
  const needle = name.toLowerCase();
  const out = new Set();
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    if (el.children.length > 0) continue;               // leaf nodes only
    const t = (el.textContent || '').trim().toLowerCase();
    if (!t || !t.includes(needle)) continue;
    // climb to the row: first ancestor whose text has a decimal (SOL) figure
    let row = el;
    for (let i = 0; i < 5 && row.parentElement; i++) {
      row = row.parentElement;
      if (/\\d+\\.\\d+/.test(row.innerText || '')) break;
    }
    out.add((row.innerText || '').replace(/\\s+/g, ' ').trim());
  }
  return Array.from(out);
}
"""


DEC_RE = re.compile(r"(\d[\d,]*\.\d+)")  # SOL amounts always have a decimal point
ONCHAIN_RE = re.compile(r"(\d[\d,]*)\s*on[- ]?chain", re.IGNORECASE)
LABEL = {"Hunters": "DUST", "Community": "weekly DUST"}


def parse_sol(text: str) -> float | None:
    """SOL = the decimal number in the row (dust/points are whole numbers)."""
    m = DEC_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


_scrape_lock = asyncio.Lock()


def row_score(row: str, name: str) -> int:
    """How much a text block looks like a real leaderboard row for `name`.
    Real rows start with the rank ("6 hnnicalls ...") or show "N on chain";
    feed items / sticky bars / toasts that merely mention the name score 0."""
    score = 0
    if re.match(rf"^\D{{0,3}}\d{{1,4}}\s+{re.escape(name)}\b", row.strip(), re.IGNORECASE):
        score += 4
    if ONCHAIN_RE.search(row):
        score += 2
    if parse_sol(row) is not None and parse_dust(row) is not None:
        score += 1
    return score


def parse_dust(text: str) -> float | None:
    """Hunters: '509 on chain'. Community: the whole number just before the SOL amount."""
    text = text or ""
    m = ONCHAIN_RE.search(text)
    if m:
        return float(m.group(1).replace(",", ""))
    d = DEC_RE.search(text)
    if not d:
        return None
    nums = re.findall(r"\d[\d,]*", text[: d.start()])
    return float(nums[-1].replace(",", "")) if nums else None


LATEST: dict = {"results": None, "at": None}  # newest reading, kept in memory for instant replies
_pw = None
_browser = None


async def get_browser():
    """One Chromium for the life of the bot - launching it every time was the slowest part."""
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        if _pw is None:
            _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
        )
    return _browser


async def reset_browser() -> None:
    global _browser
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass
    _browser = None


async def new_page():
    browser = await get_browser()
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})

    async def skip_heavy(route):  # images/fonts/video aren't needed and slow the load
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", skip_heavy)
    return page


async def wait_js(page, expr: str, timeout: int) -> None:
    try:
        await page.wait_for_function(expr, timeout=timeout)
    except Exception:
        pass


async def scrape(probe: bool = False) -> tuple[dict, list[str]]:
    """One scrape at a time (Render instances have little RAM)."""
    async with _scrape_lock:
        try:
            results, urls = await _scrape_impl(probe)
        except Exception:
            await reset_browser()
            raise
    if has_data(results):
        LATEST["results"] = merge_missing(LATEST["results"] or {}, results)
        LATEST["at"] = datetime.now()
    return results, urls


async def read_board(page, board: str) -> dict:
    """Find the tracked names on the visible board; stop scrolling as soon as all are found."""
    best: dict[str, tuple] = {}
    for _ in range(8):
        for name in NAMES:
            for row in await page.evaluate(FIND_ROWS_JS, name):
                if board == "Community" and ONCHAIN_RE.search(row):
                    continue  # that's a Hunters row still on screen
                key = (row_score(row, name), -len(row))
                if key[0] >= 1 and (name not in best or key > best[name][0]):
                    best[name] = (key, row)
        if len(best) == len(NAMES):
            break
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(250)
    return {n: {"sol": parse_sol(r), "dust": parse_dust(r), "row": r} for n, (_k, r) in best.items()}


async def _scrape_impl(probe: bool = False) -> tuple[dict, list[str]]:
    """Return ({board: {name: {'sol', 'dust', 'row'}}}, json_urls)."""
    results = {b: {n: {"sol": None, "dust": None, "row": None} for n in NAMES} for b in BOARDS}
    json_urls: list[str] = []
    page = await new_page()
    tasks: list = []

    async def note(resp):
        """Remember JSON calls the page makes; in probe mode, flag the ones carrying our names."""
        try:
            if "json" not in resp.headers.get("content-type", ""):
                return
            tag = ""
            if probe:
                body = (await resp.text()).lower()
                if any(n in body for n in NAMES):
                    tag = "★ "
            # query strings are dropped: they can carry keys or tokens
            json_urls.append(f"{tag}{resp.request.method} {resp.url.split('?')[0]}")
        except Exception:
            pass

    try:
        page.on("response", lambda resp: tasks.append(asyncio.create_task(note(resp))))
        for board, cfg in BOARDS.items():
            await page.goto(URL + cfg["hash"], wait_until="domcontentloaded", timeout=45000)
            # the site keeps connections open, so never wait for "networkidle"
            await wait_js(page, "/sol|◎/i.test(document.body.innerText)", 15000)
            try:
                await page.get_by_text(re.compile(cfg["tab_re"], re.I)).first.click(timeout=3000)
            except Exception:
                pass
            # wait until the right board is showing: only Hunters has "on chain"
            marker = "/on chain/i.test(document.body.innerText)"
            await wait_js(page, marker if board == "Hunters" else "!" + marker, 8000)
            results[board].update(await read_board(page, board))
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await page.close()
    return results, json_urls


def save_cache(results: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps({"saved_at": datetime.now().isoformat(timespec="seconds"), "results": results})
    )


def load_cache() -> tuple[dict, str] | None:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data["results"], data["saved_at"]
    except Exception:
        return None


def fmt(x: float | None) -> str:
    return "not found" if x is None else f"{x:,.4f} SOL"


def build_message(results: dict, offline_since: str | None = None, note: str | None = None) -> str:
    lines = ["📊 *Corium update*"]
    if offline_since:
        lines.append(f"📴 _Offline, showing saved data from {offline_since}_")
    if note:
        lines.append(note)
    lines.append("")
    per_name = {n: 0.0 for n in NAMES}
    per_board = {b: 0.0 for b in BOARDS}

    for board, names in results.items():
        lines.append(f"*{board}*")
        for name, d in names.items():
            dust = f"  |  {LABEL[board]} {d['dust']:,.0f}" if d.get("dust") is not None else ""
            lines.append(f"  • {name}: {fmt(d['sol'])}{dust}")
            if d["sol"] is not None:
                per_name[name] += d["sol"]
                per_board[board] += d["sol"]
        lines.append(f"  ↳ both accounts: {per_board[board]:,.4f} SOL")
        lines.append("")

    lines.append("*Per account (Hunters + Community)*")
    for n in NAMES:
        lines.append(f"  • {n}: {per_name[n]:,.4f} SOL")
    lines.append("")
    lines.append(f"*TOTAL combined: {sum(per_name.values()):,.4f} SOL*")

    missing = [
        f"{b}/{n}" for b, ns in results.items() for n, d in ns.items() if d["sol"] is None
    ]
    if missing:
        lines += ["", "⚠️ Could not read: " + ", ".join(missing)]
    return "\n".join(lines)


def has_data(results: dict) -> bool:
    return any(
        d["sol"] is not None or d.get("dust") is not None
        for ns in results.values()
        for d in ns.values()
    )


def diff_results(old: dict, new: dict) -> list[str]:
    out = []
    for board, names in new.items():
        for name, d in names.items():
            o = old.get(board, {}).get(name, {})
            if d.get("row") and not o.get("row"):
                out.append(
                    f"➕ {name} appeared on {board}: {fmt(d['sol'])}"
                    + (f", {LABEL.get(board, 'dust')} {d['dust']:,.0f}" if d.get("dust") is not None else "")
                )
                continue
            for field, label in (("sol", "SOL"), ("dust", LABEL.get(board, "dust"))):
                nd = 4 if field == "sol" else 0
                a, b = o.get(field), d.get(field)
                if b is None:
                    continue
                if a is None:
                    out.append(f"➕ {name} / {board}: {label} {b:,.{nd}f} (new)")
                elif abs(b - a) > 1e-9:
                    arrow = "🟢 ▲" if b > a else "🔴 ▼"
                    out.append(
                        f"{arrow} {name} / {board}: {label} {a:,.{nd}f} → {b:,.{nd}f} ({b - a:+,.{nd}f})"
                    )
    return out


_pending = None  # signature of a change waiting for a second matching reading


def signature(results: dict) -> tuple:
    return tuple(
        (b, n, d.get("sol"), d.get("dust"))
        for b, ns in sorted(results.items())
        for n, d in sorted(ns.items())
    )


def merge_missing(old: dict, new: dict) -> dict:
    """If a name wasn't found this time, keep its previous reading."""
    out = {b: {n: dict(d) for n, d in ns.items()} for b, ns in new.items()}
    for b, ns in out.items():
        for n, d in ns.items():
            prev = old.get(b, {}).get(n)
            if d.get("row") is None and prev and prev.get("row"):
                out[b][n] = prev
    return out


async def process_results(bot, results: dict) -> None:
    """Compare with the saved snapshot. A change only alerts once the next check
    shows the same numbers again, so one-off bad readings never reach you."""
    global _pending
    if not has_data(results):  # parse failure - don't overwrite a good snapshot
        return
    cached = load_cache()
    if not cached:  # first run: just record a baseline
        save_cache(results)
        return
    results = merge_missing(cached[0], results)
    changes = diff_results(cached[0], results)
    if not changes:
        _pending = None
        save_cache(results)
        return
    sig = signature(results)
    if _pending != sig:
        _pending = sig  # first sighting - wait for the next check to confirm
        return
    _pending = None
    save_cache(results)
    if not SUBSCRIBERS:
        return
    total = sum(d["sol"] or 0 for ns in results.values() for d in ns.values())
    text = "🔔 Corium change\n" + "\n".join(changes) + f"\n\nTotal combined: {total:,.4f} SOL"
    for cid in SUBSCRIBERS:
        try:
            await bot.send_message(cid, text)
        except Exception:
            log.exception("could not notify %s", cid)


def allowed(update: Update) -> bool:
    return not ENV_IDS or update.effective_chat.id in ENV_IDS


async def myid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


async def notify_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    cid = update.effective_chat.id
    SUBSCRIBERS.add(cid)
    await update.message.reply_text(
        f"🔔 Alerts on for this chat (checking every {REFRESH_MINUTES} min).\n"
        f"To keep this after restarts, set NOTIFY_CHAT_ID={cid} in Render."
    )


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Alerts off for this chat.")


def age_text(at: datetime) -> str:
    secs = int((datetime.now() - at).total_seconds())
    return f"{secs}s ago" if secs < 90 else f"{secs // 60} min ago"


async def refresh_message(msg, bot, shown: dict | None) -> None:
    """Scrape in the background, then swap the instant reply for fresh numbers."""
    try:
        results, _ = await scrape()
        await process_results(bot, results)
        text = build_message(LATEST["results"] if has_data(results) else results)
    except Exception as e:
        log.warning("refresh failed: %s", safe_err(e))
        text = (
            build_message(shown, note="⚠️ _couldn't refresh, showing the last reading_")
            if shown
            else f"❌ Couldn't read Corium: {safe_err(e)}"
        )
    try:
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception:
        pass  # Telegram refuses an edit that changes nothing


async def update_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    shown, at = LATEST["results"], LATEST["at"]
    if shown is None:  # nothing in memory yet (just started) - try the saved file
        cached = load_cache()
        if cached:
            shown, at = cached[0], datetime.fromisoformat(cached[1])
    if shown is not None and at is not None:
        # answer instantly with the newest reading, then refresh it in the background
        msg = await update.message.reply_text(
            build_message(shown, note=f"⚡ _{age_text(at)}, refreshing..._"), parse_mode="Markdown"
        )
    else:
        msg = await update.message.reply_text("⏳ First check, one moment...")
    ctx.application.create_task(refresh_message(msg, ctx.bot, shown))


async def debug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shows the raw rows matched and JSON endpoints the page calls."""
    if not allowed(update):
        return
    msg = await update.message.reply_text("⏳ Debugging...")
    try:
        results, urls = await scrape(probe=True)
        out = []
        for b, ns in results.items():
            for n, d in ns.items():
                out.append(f"{b}/{n}: {d['row']}")
        out.append("\nJSON calls (★ = response contains your names):\n" + "\n".join(sorted(set(urls), key=lambda u: (not u.startswith("★"), u))[:25]))
        await msg.edit_text("\n".join(out)[:4000])
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


# ---------------------------------------------------------------------------
# On-chain wallet watcher: alerts when a tracked wallet buys (or sells) in the
# FINAL STRETCH of a Corium curve (last 10% = 76.5 -> 85 SOL).
# Corium coins are Meteora Dynamic Bonding Curve pools using Corium's config,
# so a swap is a Corium trade when its tx touches both of these accounts.
# ---------------------------------------------------------------------------
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
WALLET_POLL_SECONDS = int(os.environ.get("WALLET_POLL_SECONDS", "60"))
SOL_MINT = "So11111111111111111111111111111111111111112"
DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
CORIUM_CONFIG = "HdY7SdqgdquPcSSFD6gkFaLUQ4zRTdCjPDaPa57DgbRw"
STRETCH_START, CURVE_END = 76.5, 85.0

WATCH = {"neuromancer": "3qpXPehJ99E2efDjsWC2KpUwquXH2pePWm3AW4e9jHr4"}
for _item in os.environ.get("WATCH_WALLETS", "").split(","):  # name:address,name:address
    if ":" in _item:
        _n, _a = _item.split(":", 1)
        WATCH[_n.strip()] = _a.strip()

WATCH_STATE = {"last_check": None, "last_event": None, "error": None}


def _amount(b: dict | None) -> float:
    if not b:
        return 0.0
    u = b["uiTokenAmount"]
    return float(u["amount"]) / (10 ** int(u["decimals"]))


def analyze_tx(tx: dict, wallet: str) -> dict | None:
    """Return an event dict if this tx is a final-stretch buy/sell by `wallet`."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = {
        k["pubkey"] if isinstance(k, dict) else k
        for k in tx["transaction"]["message"]["accountKeys"]
    }
    if DBC_PROGRAM not in keys or CORIUM_CONFIG not in keys:
        return None  # not a Corium curve trade

    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}

    coin_delta: dict[str, float] = {}  # tokens the wallet gained/lost, per mint
    vault = None  # (before, after) of the curve's SOL vault
    for idx in set(pre) | set(post):
        b = post.get(idx) or pre.get(idx)
        before, after = _amount(pre.get(idx)), _amount(post.get(idx))
        if b["mint"] == SOL_MINT:
            if b.get("owner") != wallet and (
                vault is None or abs(after - before) > abs(vault[1] - vault[0])
            ):
                vault = (before, after)
        elif b.get("owner") == wallet:
            coin_delta[b["mint"]] = coin_delta.get(b["mint"], 0.0) + (after - before)
    if vault is None:
        return None

    v0, v1 = vault
    for mint, delta in coin_delta.items():
        if delta > 0 and v1 > v0:  # buy
            credit = min(v1, CURVE_END) - max(v0, STRETCH_START)
            if credit > 0:
                return {"side": "buy", "mint": mint, "pre": v0, "post": v1, "credit": credit}
        elif delta < 0 and v1 < v0:  # sell
            removed = min(v0, CURVE_END) - max(v1, STRETCH_START)
            if removed > 0:
                return {"side": "sell", "mint": mint, "pre": v0, "post": v1, "credit": removed}
    return None


def safe_err(e: Exception) -> str:
    """Error text without the RPC URL (it may contain an API key)."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from RPC"
    return f"{type(e).__name__}: {str(e)[:150]}"


async def rpc(client: httpx.AsyncClient, method: str, params: list):
    r = await client.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


async def notify_wallet_event(bot, name: str, wallet: str, sig: str, ev: dict) -> None:
    if ev["side"] == "buy":
        head = f"🌠 {name} BOUGHT the final stretch"
        credit_line = f"Stretch credit gained: ~{ev['credit']:.2f} SOL"
    else:
        head = f"⚠️ {name} SOLD in the final stretch"
        credit_line = f"Stretch credit removed: ~{ev['credit']:.2f} SOL"
    text = (
        f"{head}\n"
        f"Coin: {ev['mint']}\n"
        f"Curve: {ev['pre']:.2f} → {ev['post']:.2f} SOL (stretch is 76.5 → 85)\n"
        f"{credit_line}\n\n"
        f"Tx: https://solscan.io/tx/{sig}\n"
        f"Coin: https://solscan.io/token/{ev['mint']}\n"
        f"Wallet: https://corium.so/wallet/{wallet}\n"
        f"(curve figures are read from the pool vault, so treat them as approximate)"
    )
    WATCH_STATE["last_event"] = f"{datetime.now():%Y-%m-%d %H:%M} {head}"
    for cid in SUBSCRIBERS:
        try:
            await bot.send_message(cid, text, disable_web_page_preview=True)
        except Exception:
            log.exception("could not notify %s", cid)


async def watch_loop(bot, targets: dict, analyze, notify, state: dict):
    """Poll each address for new transactions; run `analyze` on every new one."""
    last_sig: dict[str, str] = {}
    seen: set[str] = set()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                for name, addr in targets.items():
                    params = {"limit": 50, "commitment": "confirmed"}
                    first_run = addr not in last_sig
                    if first_run:
                        params["limit"] = 1  # baseline only, no alerts for old history
                    else:
                        params["until"] = last_sig[addr]
                    sigs = await rpc(client, "getSignaturesForAddress", [addr, params])
                    if not sigs:
                        continue
                    if not first_run:
                        for s_ in reversed(sigs):  # oldest first
                            sig = s_["signature"]
                            if s_.get("err") or sig in seen:
                                continue
                            opts = {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                                "commitment": "confirmed",
                            }
                            tx = await rpc(client, "getTransaction", [sig, opts])
                            if not tx:
                                await asyncio.sleep(2)
                                tx = await rpc(client, "getTransaction", [sig, opts])
                            if tx:
                                ev = analyze(tx, addr)
                                if ev:
                                    await notify(bot, name, addr, sig, ev)
                            seen.add(sig)
                            await asyncio.sleep(0.3)
                    last_sig[addr] = sigs[0]["signature"]
                state["last_check"] = datetime.now().isoformat(timespec="seconds")
                state["error"] = None
            except Exception as e:
                state["error"] = safe_err(e)
                log.warning("watcher: %s", state["error"])
            await asyncio.sleep(WALLET_POLL_SECONDS)


async def wallet_watcher(bot):
    await watch_loop(bot, WATCH, analyze_tx, notify_wallet_event, WATCH_STATE)


# ---------------------------------------------------------------------------
# pump.fun launch tracker: alerts when a watched wallet launches a coin.
# ---------------------------------------------------------------------------
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Anchor instruction ids for pump.fun `create` and `create_v2` (Token-2022)
CREATE_DISCS = {bytes([24, 30, 200, 40, 5, 28, 7, 119]), bytes([214, 144, 76, 236, 95, 139, 49, 180])}

CREATORS = {"launcher": "bwamJzztZsepfkteWRChggmXuiiCQvpLqPietdNfSXa"}
for _item in os.environ.get("WATCH_CREATORS", "").split(","):  # name:address,name:address
    if ":" in _item:
        _n, _a = _item.split(":", 1)
        CREATORS[_n.strip()] = _a.strip()

CREATE_STATE = {"last_check": None, "last_event": None, "error": None}
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def read_strings(b: bytes, count: int = 3) -> list[str]:
    """Borsh strings: u32 little-endian length + utf-8 bytes."""
    out, i = [], 0
    for _ in range(count):
        ln = int.from_bytes(b[i : i + 4], "little")
        i += 4
        if ln > len(b) - i:
            raise ValueError("bad string length")
        out.append(b[i : i + ln].decode("utf-8", "replace"))
        i += ln
    return out


def analyze_create(tx: dict, wallet: str) -> dict | None:
    """Return {'mint','name','symbol'} if `wallet` signed a pump.fun coin launch."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    msg = tx["transaction"]["message"]
    keys = msg["accountKeys"]
    pubkeys = {k["pubkey"] if isinstance(k, dict) else k for k in keys}
    if PUMP_PROGRAM not in pubkeys:
        return None
    signers = {k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")}
    if wallet not in signers:
        return None

    instrs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instrs += inner.get("instructions") or []

    # 1) the pump.fun create instruction itself (mint is its first account)
    for ins in instrs:
        if ins.get("programId") == PUMP_PROGRAM and ins.get("data") and ins.get("accounts"):
            try:
                raw = b58decode(ins["data"])
            except Exception:
                continue
            if raw[:8] in CREATE_DISCS:
                name = symbol = None
                try:
                    name, symbol, _uri = read_strings(raw[8:])
                except Exception:
                    pass
                return {"mint": ins["accounts"][0], "name": name, "symbol": symbol}

    # 2) fallback: a pump.fun tx that also initialises a brand-new mint
    for ins in instrs:
        parsed = ins.get("parsed")
        if isinstance(parsed, dict) and str(parsed.get("type", "")).startswith("initializeMint"):
            mint = (parsed.get("info") or {}).get("mint")
            if mint:
                return {"mint": mint, "name": None, "symbol": None}
    return None


async def notify_create_event(bot, name: str, wallet: str, sig: str, ev: dict) -> None:
    label = ""
    if ev.get("name") or ev.get("symbol"):
        label = f"\n{ev.get('name') or ''} (${ev.get('symbol') or '?'})"
    text = (
        f"🚀 {name} just launched a coin on pump.fun{label}\n\n"
        f"https://pump.fun/coin/{ev['mint']}\n\n"
        f"Mint: {ev['mint']}\n"
        f"Tx: https://solscan.io/tx/{sig}"
    )
    CREATE_STATE["last_event"] = f"{datetime.now():%Y-%m-%d %H:%M} {ev.get('symbol') or ev['mint'][:6]}"
    for cid in SUBSCRIBERS:
        try:
            await bot.send_message(cid, text, disable_web_page_preview=False)
        except Exception:
            log.exception("could not notify %s", cid)


async def creator_watcher(bot):
    await watch_loop(bot, CREATORS, analyze_create, notify_create_event, CREATE_STATE)


async def wallets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    lines = ["👀 Final-stretch watch:"]
    for n, a in WATCH.items():
        lines.append(f"  • {n}: {a[:4]}…{a[-4:]}")
    lines.append(f"Last check: {WATCH_STATE['last_check'] or 'not yet'}")
    lines.append(f"Last alert: {WATCH_STATE['last_event'] or 'none yet'}")
    if WATCH_STATE["error"]:
        lines.append(f"⚠️ Last error: {WATCH_STATE['error']}")
    lines.append("")
    lines.append("🚀 pump.fun launch watch:")
    for n, a in CREATORS.items():
        lines.append(f"  • {n}: {a[:4]}…{a[-4:]}")
    lines.append(f"Last check: {CREATE_STATE['last_check'] or 'not yet'}")
    lines.append(f"Last launch: {CREATE_STATE['last_event'] or 'none yet'}")
    if CREATE_STATE["error"]:
        lines.append(f"⚠️ Last error: {CREATE_STATE['error']}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Bounty watcher: alerts when a new coin bounty shows up on corium.so/bounties.
# The page is JavaScript-rendered and I could not see its layout, so this first
# looks for the JSON list the page loads, then falls back to links showing SOL.
# ---------------------------------------------------------------------------
BOUNTY_URL = "https://corium.so/bounties"
BOUNTY_MINUTES = int(os.environ.get("BOUNTY_MINUTES", "10"))
BOUNTY_STATE = {"last_check": None, "count": None, "error": None}
ID_KEYS = ["id", "bountyId", "bounty_id", "pubkey", "address", "mint", "tokenMint", "coinMint", "coin_mint", "coin"]
NAME_KEYS = ["symbol", "ticker", "name", "coinName", "title"]
AMT_HINT = re.compile(r"amount|lamport|sol|total|pot|escrow|bounty", re.IGNORECASE)
ANCHOR_JS = r"""
() => Array.from(document.querySelectorAll('a[href]'))
  .map(a => ({href: a.href, text: (a.innerText || '').replace(/\s+/g, ' ').trim()}))
  .filter(x => /\d+(\.\d+)?\s*(SOL|◎)|◎\s*\d/i.test(x.text))
"""


def find_lists(obj, out: list) -> None:
    if isinstance(obj, list):
        if obj and all(isinstance(i, dict) for i in obj):
            out.append(obj)
        for i in obj:
            find_lists(i, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            find_lists(v, out)


DEAD_STATES = {"closed", "expired", "refunded", "paid", "settled", "graduated", "cancelled",
               "canceled", "ended", "completed", "won", "finished", "claimed", "failed"}
NAME_KEYS += ["coinSymbol", "tokenSymbol", "coinName", "tokenName"]


def to_ts(v) -> float | None:
    """Epoch seconds, epoch milliseconds or an ISO date -> seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def flat_pairs(it: dict) -> list:
    """The item's own fields plus one level of nested objects (e.g. a 'coin' object)."""
    out = []
    for k, v in it.items():
        out.append((k, v))
        if isinstance(v, dict):
            out.extend(v.items())
    return out


def normalize_bounty(it: dict) -> dict:
    pairs = flat_pairs(it)
    ident = next((f"{k}:{it[k]}" for k in ID_KEYS if isinstance(it.get(k), (str, int))), None)
    name = next((str(v) for k, v in pairs if k in NAME_KEYS and isinstance(v, str) and v), None)
    amount = None
    for k, v in it.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and AMT_HINT.search(k):
            amount = float(v) / 1e9 if v >= 1e6 else float(v)  # lamports -> SOL
            break
    own_id = str(it.get("id", ""))
    mint = next(
        (v for k, v in pairs
         if isinstance(v, str) and 32 <= len(v) <= 44 and v != own_id
         and re.search(r"mint|coin|token|pool", k, re.IGNORECASE)),
        None,
    ) or next(
        (v for _k, v in pairs if isinstance(v, str) and 32 <= len(v) <= 44 and v.endswith("pump")),
        None,
    )
    deadline = next(
        (to_ts(v) for k, v in pairs if re.search(r"deadline|expir|ends?_?at|end_?time", k, re.IGNORECASE) and to_ts(v)),
        None,
    )
    status = next(
        (str(v) for k, v in pairs if re.fullmatch(r"status|state", k, re.IGNORECASE) and isinstance(v, str)),
        "",
    )
    live = status.lower() not in DEAD_STATES and not (deadline and deadline < datetime.now().timestamp())
    return {
        "id": ident or json.dumps(it, sort_keys=True)[:200],
        "name": name,
        "kind": str(it.get("kind") or ""),
        "amount": amount,
        "mint": mint,
        "deadline": deadline,
        "live": live,
        "text": json.dumps(it)[:400],
    }


def pick_bounty_items(payloads: list, dom: list) -> list[dict]:
    lists: list = []
    for pl in payloads:
        find_lists(pl, lists)
    best, best_score = None, 0
    for lst in lists:
        hits = sum(1 for k in lst[0] if AMT_HINT.search(k))
        score = hits * 100 + min(len(lst), 99)
        if hits and score > best_score:
            best, best_score = lst, score
    if best:
        return [normalize_bounty(i) for i in best]
    items = []
    for a in dom:
        m = DEC_RE.search(a["text"]) or re.search(r"(\d[\d,]*)", a["text"])
        items.append(
            {
                "id": a["href"],
                "name": a["text"][:60],
                "amount": float(m.group(1).replace(",", "")) if m else None,
                "mint": None,
                "kind": "",
                "deadline": None,
                "live": True,
                "text": a["text"],
            }
        )
    return items


async def scrape_bounties() -> list[dict]:
    payloads: list = []
    tasks: list = []

    async def grab(resp):
        try:
            if "json" in resp.headers.get("content-type", ""):
                payloads.append(await resp.json())
        except Exception:
            pass

    async with _scrape_lock:
        try:
            page = await new_page()
            try:
                page.on("response", lambda r: tasks.append(asyncio.create_task(grab(r))))
                await page.goto(BOUNTY_URL, wait_until="domcontentloaded", timeout=45000)
                await wait_js(page, "/sol|◎/i.test(document.body.innerText)", 15000)
                await page.wait_for_timeout(1500)
                dom = await page.evaluate(ANCHOR_JS)
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await page.close()
        except Exception:
            await reset_browser()
            raise
    return pick_bounty_items(payloads, dom)


def remaining(ts: float) -> str:
    secs = int(ts - datetime.now().timestamp())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    return f"in {d}d {h}h" if d else (f"in {h}h {rem // 60}m" if h else f"in {rem // 60}m")


def bounty_line(i: dict) -> str:
    label = i["name"] or (i["mint"][:6] + "…" if i["mint"] else i["id"].split(":")[-1][:6] + "…")
    parts = [label]
    if i["amount"] is not None:
        parts.append(f"{i['amount']:.2f} SOL")
    if i.get("deadline"):
        parts.append(f"ends {remaining(i['deadline'])}")
    return " - ".join(parts)


def bounty_link(i: dict) -> str:
    if not i["mint"]:
        return ""
    if i["mint"].endswith("pump"):
        return f"\nhttps://pump.fun/coin/{i['mint']}"
    return f"\nhttps://solscan.io/token/{i['mint']}"


def live_bounties(items: list[dict]) -> list[dict]:
    """Open bounties only: not expired/paid, and not 'requests' (those are a different thing)."""
    return [i for i in items if i["live"] and "request" not in i.get("kind", "").lower()]


async def send_all(bot, text: str) -> None:
    for cid in SUBSCRIBERS:
        try:
            await bot.send_message(cid, text, disable_web_page_preview=True)
        except Exception:
            log.exception("could not notify %s", cid)


async def bounty_watcher(bot):
    known = None
    while True:
        try:
            all_items = await scrape_bounties()
            items = live_bounties(all_items)
            BOUNTY_STATE.update(count=len(items), last_check=datetime.now().isoformat(timespec="seconds"), error=None)
            if all_items:
                ids = {i["id"] for i in items}
                if known is None:
                    known = ids
                    if items:  # one summary of what is live right now, then only new ones
                        top = sorted(items, key=lambda i: i["amount"] or 0, reverse=True)[:5]
                        await send_all(
                            bot,
                            f"💰 {len(items)} live bounties right now:\n"
                            + "\n".join(f"• {bounty_line(i)}" for i in top)
                            + f"\n\n{BOUNTY_URL}",
                        )
                else:
                    new = [i for i in items if i["id"] not in known]
                    known |= ids
                    for i in new[:5]:
                        await send_all(bot, f"💰 New bounty live\n{bounty_line(i)}{bounty_link(i)}\n{BOUNTY_URL}")
                    if len(new) > 5:
                        await send_all(bot, f"...and {len(new) - 5} more new bounties: {BOUNTY_URL}")
        except Exception as e:
            BOUNTY_STATE["error"] = safe_err(e)
            log.warning("bounty watcher: %s", BOUNTY_STATE["error"])
        await asyncio.sleep(BOUNTY_MINUTES * 60)


async def bounties_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("⏳ Reading the bounty board...")
    try:
        all_items = await scrape_bounties()
    except Exception as e:
        await msg.edit_text(f"❌ {safe_err(e)}")
        return
    items = live_bounties(all_items)
    if not items:
        await msg.edit_text(f"No live bounties found ({len(all_items)} entries on the board).")
        return
    top = sorted(items, key=lambda i: i["amount"] or 0, reverse=True)[:15]
    lines = [f"💰 {len(items)} live bounties ({len(all_items)} entries on the board):"]
    lines += [f"• {bounty_line(i)}" for i in top]
    if not any(i["name"] for i in items):  # couldn't read coin names - show the raw shape so it can be fixed
        lines.append(f"\n(no coin names found, raw sample: {items[0]['text'][:350]})")
    await msg.edit_text("\n".join(lines))


async def background_refresh(bot):
    """Keeps the readings fresh and sends alerts; silently skips when offline."""
    while True:
        try:
            results, _ = await scrape()
            await process_results(bot, results)
            log.info("checked")
        except Exception as e:
            log.warning("refresh skipped (offline?): %s", safe_err(e))
        # a change was just seen once: re-check in 40s to confirm it, don't wait a full interval
        await asyncio.sleep(40 if _pending is not None else REFRESH_MINUTES * 60)


async def post_init(app: Application):
    app.create_task(background_refresh(app.bot))
    app.create_task(wallet_watcher(app.bot))
    app.create_task(creator_watcher(app.bot))
    app.create_task(bounty_watcher(app.bot))


def start_health_server(port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    threading.Thread(
        target=HTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True
    ).start()


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("wallets", wallets_cmd))
    app.add_handler(CommandHandler("bounties", bounties_cmd))

    # Render web services must listen on $PORT. Answer 200 there so uptime
    # pingers see "healthy" and the free service stays awake.
    if os.environ.get("RENDER_EXTERNAL_URL"):
        start_health_server(int(os.environ.get("PORT", "10000")))
    app.run_polling()


if __name__ == "__main__":
    main()
