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

from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

URL = "https://corium.so/points"
NAMES = ["cosmi", "hnnicalls"]
BOARDS = {
    "Hunters": {"tab_text": "Hunters", "hash": "#hunters"},
    "Community": {"tab_text": "Community", "hash": "#community"},
}

# Matches e.g. "12.34 SOL", "1,234 SOL", "◎ 5.2", "5.2◎"
SOL_RE = re.compile(
    r"(?:(\d[\d,]*\.?\d*)\s*(?:SOL|◎))|(?:◎\s*(\d[\d,]*\.?\d*))", re.IGNORECASE
)

CACHE_FILE = Path(__file__).with_name("corium_cache.json")
REFRESH_MINUTES = int(os.environ.get("CHECK_MINUTES", "10"))  # auto-check interval

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
    // climb to the row: first ancestor whose text also has a SOL/number figure
    let row = el;
    for (let i = 0; i < 5 && row.parentElement; i++) {
      row = row.parentElement;
      if (/(sol|◎)/i.test(row.innerText || '')) break;
    }
    out.add((row.innerText || '').replace(/\\s+/g, ' ').trim());
  }
  return Array.from(out);
}
"""


def parse_sol(text: str) -> float | None:
    """First SOL amount found in a row of text."""
    m = SOL_RE.search(text)
    if not m:
        return None
    return float((m.group(1) or m.group(2)).replace(",", ""))


_scrape_lock = asyncio.Lock()


def parse_dust(text: str) -> float | None:
    m = DUST_RE.search(text or "")
    if not m:
        return None
    return float((m.group(1) or m.group(2)).replace(",", ""))


async def scrape() -> tuple[dict, list[str]]:
    """One browser at a time (Render instances have little RAM)."""
    async with _scrape_lock:
        return await _scrape_impl()


async def _scrape_impl() -> tuple[dict, list[str]]:
    """Return ({board: {name: {'sol': float|None, 'row': str|None}}}, json_urls)."""
    results = {b: {n: {"sol": None, "dust": None, "row": None} for n in NAMES} for b in BOARDS}
    json_urls: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = await browser.new_page(viewport={"width": 1400, "height": 1000})

        def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                json_urls.append(resp.url)

        page.on("response", on_response)

        for board, cfg in BOARDS.items():
            await page.goto(URL + cfg["hash"], wait_until="networkidle")
            # try clicking the tab in case the hash alone doesn't switch it
            try:
                await page.get_by_text(cfg["tab_text"], exact=False).first.click(
                    timeout=3000
                )
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # scroll a few times in case the list is lazy/virtualised
            seen: dict[str, str] = {}
            for _ in range(8):
                for name in NAMES:
                    rows = await page.evaluate(FIND_ROWS_JS, name)
                    if rows:
                        # shortest row is usually the tightest match
                        seen[name] = min(rows, key=len)
                await page.mouse.wheel(0, 1500)
                await page.wait_for_timeout(600)

            for name, row in seen.items():
                results[board][name] = {"sol": parse_sol(row), "dust": parse_dust(row), "row": row}

        await browser.close()
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


def build_message(results: dict, offline_since: str | None = None) -> str:
    lines = ["📊 *Corium update*"]
    if offline_since:
        lines.append(f"📴 _Offline, showing saved data from {offline_since}_")
    lines.append("")
    per_name = {n: 0.0 for n in NAMES}
    per_board = {b: 0.0 for b in BOARDS}

    for board, names in results.items():
        lines.append(f"*{board}*")
        for name, d in names.items():
            dust = f"  |  dust {d['dust']:,.4f}" if d.get("dust") is not None else ""
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
                    + (f", dust {d['dust']:,.4f}" if d.get("dust") is not None else "")
                )
                continue
            for field, label in (("sol", "SOL"), ("dust", "dust")):
                a, b = o.get(field), d.get(field)
                if b is None:
                    continue
                if a is None:
                    out.append(f"➕ {name} / {board}: {label} {b:,.4f} (new)")
                elif abs(b - a) > 1e-9:
                    arrow = "🟢 ▲" if b > a else "🔴 ▼"
                    out.append(
                        f"{arrow} {name} / {board}: {label} {a:,.4f} → {b:,.4f} ({b - a:+,.4f})"
                    )
    return out


async def process_results(bot, results: dict) -> None:
    """Compare with the last saved snapshot, alert subscribers, then save."""
    if not has_data(results):  # parse failure - don't overwrite a good snapshot
        return
    cached = load_cache()
    save_cache(results)
    if not cached:  # first run: just record a baseline
        return
    changes = diff_results(cached[0], results)
    if not changes or not SUBSCRIBERS:
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


async def update_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("⏳ Checking Corium...")
    try:
        results, _ = await scrape()
        await process_results(ctx.bot, results)
        await msg.edit_text(build_message(results), parse_mode="Markdown")
    except Exception as e:
        log.exception("scrape failed")
        cached = load_cache()
        if cached:
            results, saved_at = cached
            await msg.edit_text(
                build_message(results, offline_since=saved_at), parse_mode="Markdown"
            )
        else:
            await msg.edit_text(f"❌ Failed and no saved data yet: {e}")


async def debug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shows the raw rows matched and JSON endpoints the page calls."""
    if not allowed(update):
        return
    msg = await update.message.reply_text("⏳ Debugging...")
    try:
        results, urls = await scrape()
        out = []
        for b, ns in results.items():
            for n, d in ns.items():
                out.append(f"{b}/{n}: {d['row']}")
        out.append("\nJSON endpoints:\n" + "\n".join(sorted(set(urls))[:20]))
        await msg.edit_text("\n".join(out)[:4000])
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


async def background_refresh(bot):
    """Keeps the cache fresh; silently skips when offline."""
    while True:
        try:
            results, _ = await scrape()
            await process_results(bot, results)
            log.info("checked")
        except Exception as e:
            log.warning("refresh skipped (offline?): %s", e)
        await asyncio.sleep(REFRESH_MINUTES * 60)


async def post_init(app: Application):
    app.create_task(background_refresh(app.bot))


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
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))

    # Render web services must listen on $PORT. Answer 200 there so uptime
    # pingers see "healthy" and the free service stays awake.
    if os.environ.get("RENDER_EXTERNAL_URL"):
        start_health_server(int(os.environ.get("PORT", "10000")))
    app.run_polling()


if __name__ == "__main__":
    main()
