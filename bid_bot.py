"""
ServiceLink Auction — Playwright Bid Bot
=========================================
Automates bid submission at the optimal T-2:05 window to avoid triggering
the auto-extend, while using ServiceLink's built-in proxy max system.

Usage:
    # Discover selectors (run once to map the UI — opens visible browser)
    python3 bid_bot.py --discover --url "https://www.servicelinkauction.com/listing/XXXXX"

    # Dry run (watch only, don't actually bid)
    python3 bid_bot.py --url "https://www.servicelinkauction.com/listing/XXXXX" \
                       --bid 85001 --dry-run

    # Live bid
    python3 bid_bot.py --url "https://www.servicelinkauction.com/listing/XXXXX" \
                       --bid 85001

Credentials:
    Set SERVICELINK_EMAIL and SERVICELINK_PASSWORD in .env
"""

import argparse
import asyncio
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv(Path(__file__).parent / ".env")

# ── Configuration ──────────────────────────────────────────────────────────────

LOGIN_URL   = "https://www.servicelinkauction.com/login"
BASE_URL    = "https://www.servicelinkauction.com"

# Bid timing — submit at this many seconds before auction end
# 125s = 2 min 05 sec → just outside the 2-min auto-extend window
BID_WINDOW_SECS = 125

# Poll interval while watching (seconds)
POLL_INTERVAL = 3

# Human-like delays (randomized to avoid bot detection)
TYPING_DELAY_MS  = (80, 180)   # ms per character
ACTION_DELAY_MS  = (400, 900)  # ms between actions

# ── Selector config — UPDATE THESE after running --discover ─────────────────────
# These are best-guess selectors. Run --discover to find the actual ones.
SELECTORS = {
    "login_email":      "input[type='email'], input[name='email'], #email",
    "login_password":   "input[type='password'], input[name='password'], #password",
    "login_submit":     "button[type='submit'], button:has-text('Sign In'), button:has-text('Login')",
    "countdown_timer":  "[class*='countdown'], [class*='timer'], [data-testid*='timer']",
    "current_bid":      "[class*='current-bid'], [class*='currentBid'], [class*='current_bid']",
    "bid_input":        "input[placeholder*='bid' i], input[name*='bid' i], [class*='bid-input'] input",
    "bid_submit":       "button:has-text('Place Bid'), button:has-text('Submit Bid'), button[class*='bid']",
    "proxy_bid_option": "label:has-text('Proxy'), [class*='proxy']",
    "bid_confirmation": "[class*='success'], [class*='confirm'], :has-text('bid accepted')",
    "outbid_notice":    "[class*='outbid'], :has-text('outbid')",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def human_delay(lo_ms: int = 400, hi_ms: int = 900):
    time.sleep(random.uniform(lo_ms, hi_ms) / 1000)


def parse_countdown(text: str) -> int:
    """Parse a countdown string like '2:05', '1:23:45', '45' into total seconds."""
    text = text.strip()
    # HH:MM:SS
    m = re.match(r"(\d+):(\d+):(\d+)", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    # MM:SS
    m = re.match(r"(\d+):(\d+)", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # SS only
    m = re.match(r"(\d+)", text)
    if m:
        return int(m.group(1))
    return 9999


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── Core Bot ───────────────────────────────────────────────────────────────────

class ServiceLinkBidBot:
    def __init__(self, email: str, password: str, headless: bool = False):
        self.email    = email
        self.password = password
        self.headless = headless
        self.page     = None
        self.browser  = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        # Use a realistic browser context
        context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
        )
        # Mask automation signals
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        self.page = await context.new_page()

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def login(self) -> bool:
        log(f"Navigating to login page...")
        await self.page.goto(LOGIN_URL, wait_until="networkidle")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        try:
            # Fill email
            email_field = await self.page.wait_for_selector(
                SELECTORS["login_email"], timeout=10000
            )
            await email_field.click()
            await asyncio.sleep(random.uniform(0.3, 0.7))
            await email_field.type(self.email, delay=random.randint(*TYPING_DELAY_MS))

            # Fill password
            pwd_field = await self.page.wait_for_selector(
                SELECTORS["login_password"], timeout=5000
            )
            await pwd_field.click()
            await asyncio.sleep(random.uniform(0.3, 0.7))
            await pwd_field.type(self.password, delay=random.randint(*TYPING_DELAY_MS))

            await asyncio.sleep(random.uniform(0.5, 1.2))

            # Submit
            submit = await self.page.wait_for_selector(
                SELECTORS["login_submit"], timeout=5000
            )
            await submit.click()
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Verify login succeeded
            if "login" in self.page.url.lower():
                log("❌ Login failed — still on login page. Check credentials.")
                return False

            log(f"✅ Logged in successfully")
            return True

        except PlaywrightTimeout as e:
            log(f"❌ Login timeout: {e}")
            await self.page.screenshot(path="debug_login.png")
            log("   Screenshot saved → debug_login.png")
            return False

    async def navigate_to_listing(self, url: str) -> bool:
        log(f"Navigating to listing: {url}")
        await self.page.goto(url, wait_until="networkidle")
        await asyncio.sleep(random.uniform(2, 4))

        # Scroll down naturally
        await self.page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(random.uniform(0.5, 1.0))

        log(f"  Page title: {await self.page.title()}")
        return True

    async def get_seconds_remaining(self) -> int:
        """Read the countdown timer and return seconds remaining."""
        try:
            el = await self.page.query_selector(SELECTORS["countdown_timer"])
            if el:
                text = (await el.inner_text()).strip()
                secs = parse_countdown(text)
                return secs
        except Exception:
            pass
        return 9999

    async def get_current_bid(self) -> int:
        """Read the current highest bid."""
        try:
            el = await self.page.query_selector(SELECTORS["current_bid"])
            if el:
                text = await el.inner_text()
                digits = re.sub(r"[^\d]", "", text)
                return int(digits) if digits else 0
        except Exception:
            pass
        return 0

    async def watch_auction(self, bid_amount: int, dry_run: bool = False,
                            target_secs: int = BID_WINDOW_SECS):
        """Monitor the countdown and bid at the optimal moment."""
        log(f"\n{'='*55}")
        log(f"  Watching auction | Bid ceiling: ${bid_amount:,}")
        log(f"  Will bid at: T-{target_secs}s ({target_secs//60}m {target_secs%60}s remaining)")
        log(f"  Mode: {'DRY RUN (no actual bid)' if dry_run else 'LIVE BID'}")
        log(f"{'='*55}\n")

        bid_placed    = False
        last_bid_log  = 0

        while True:
            secs_left   = await self.get_seconds_remaining()
            current_bid = await self.get_current_bid()
            now         = time.time()

            # Log every 30 seconds or when approaching the window
            if now - last_bid_log >= 30 or secs_left <= target_secs + 30:
                mins, secs = divmod(secs_left, 60)
                log(f"  ⏱  {mins:02d}:{secs:02d} remaining | current bid: ${current_bid:,}")
                last_bid_log = now

            # Auction ended
            if secs_left <= 0:
                log("  Auction ended.")
                break

            # Already outbid beyond our ceiling
            if current_bid >= bid_amount:
                log(f"  ⚠️  Current bid ${current_bid:,} ≥ our ceiling ${bid_amount:,}. Stopping.")
                break

            # Bid window reached
            if secs_left <= target_secs and not bid_placed:
                log(f"\n  🎯 BID WINDOW REACHED ({secs_left}s remaining)")

                if dry_run:
                    log(f"  [DRY RUN] Would bid ${bid_amount:,} now")
                    bid_placed = True
                else:
                    success = await self.place_bid(bid_amount)
                    if success:
                        bid_placed = True
                        log(f"  ✅ Bid ${bid_amount:,} placed! Monitoring for outbid...")
                    else:
                        log(f"  ❌ Bid failed — check debug_bid.png")
                        break

            # After bid: check if outbid
            if bid_placed and not dry_run:
                try:
                    outbid = await self.page.query_selector(SELECTORS["outbid_notice"])
                    if outbid:
                        log(f"  ⚠️  OUTBID! Current: ${current_bid:,} | Our ceiling: ${bid_amount:,}")
                        log(f"      Auto-extend triggered — proxy will handle up to our ceiling")
                except Exception:
                    pass

            await asyncio.sleep(POLL_INTERVAL)

        log("\n  Done watching auction.")

    async def place_bid(self, amount: int) -> bool:
        """Find the bid input, enter the amount, and submit."""
        log(f"  Placing bid: ${amount:,}")
        try:
            # Find bid input
            bid_input = await self.page.wait_for_selector(
                SELECTORS["bid_input"], timeout=5000
            )
            await bid_input.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # Clear existing value and type new one
            await bid_input.triple_click()
            await bid_input.type(str(amount), delay=random.randint(*TYPING_DELAY_MS))
            await asyncio.sleep(random.uniform(0.3, 0.7))

            # Screenshot before submitting
            await self.page.screenshot(path="debug_before_bid.png")
            log("  Screenshot before bid → debug_before_bid.png")

            # Submit
            submit_btn = await self.page.wait_for_selector(
                SELECTORS["bid_submit"], timeout=5000
            )
            await submit_btn.click()
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # Screenshot after submitting
            await self.page.screenshot(path="debug_after_bid.png")
            log("  Screenshot after bid → debug_after_bid.png")

            # Check for confirmation
            try:
                confirm = await self.page.wait_for_selector(
                    SELECTORS["bid_confirmation"], timeout=5000
                )
                if confirm:
                    log(f"  ✅ Bid confirmation received!")
                    return True
            except PlaywrightTimeout:
                log("  ⚠️  No confirmation dialog found — check screenshots")
                return True  # Assume placed, verify via screenshot

        except PlaywrightTimeout as e:
            log(f"  ❌ Bid placement error: {e}")
            await self.page.screenshot(path="debug_bid_error.png")
            return False

        return True

    async def discover_selectors(self, url: str):
        """
        Opens the listing page and prints all relevant elements to help
        identify the correct CSS selectors for this site.
        Run this once with --discover before using the bot live.
        """
        log("DISCOVERY MODE — identifying page elements...")
        log("Browser will open visibly so you can inspect the page.\n")

        await self.navigate_to_listing(url)
        await asyncio.sleep(3)

        # Print page title and URL
        log(f"  URL:   {self.page.url}")
        log(f"  Title: {await self.page.title()}\n")

        # Look for countdown-like elements
        log("── Countdown/Timer elements ──")
        timer_patterns = ["countdown", "timer", "clock", "remaining", "ends"]
        for pattern in timer_patterns:
            els = await self.page.query_selector_all(
                f"[class*='{pattern}'], [id*='{pattern}'], [data-*='{pattern}']"
            )
            for el in els[:3]:
                cls = await el.get_attribute("class") or ""
                txt = (await el.inner_text())[:60].strip()
                log(f"  [{pattern}] class='{cls[:60]}' | text='{txt}'")

        # Look for bid-related inputs
        log("\n── Bid input elements ──")
        inputs = await self.page.query_selector_all("input")
        for inp in inputs:
            name        = await inp.get_attribute("name") or ""
            placeholder = await inp.get_attribute("placeholder") or ""
            cls         = await inp.get_attribute("class") or ""
            itype       = await inp.get_attribute("type") or ""
            log(f"  input type={itype} name='{name}' placeholder='{placeholder}' class='{cls[:50]}'")

        # Look for buttons
        log("\n── Buttons ──")
        buttons = await self.page.query_selector_all("button")
        for btn in buttons[:15]:
            txt = (await btn.inner_text())[:60].strip()
            cls = await btn.get_attribute("class") or ""
            log(f"  button text='{txt}' class='{cls[:60]}'")

        # Current bid
        log("\n── Price/bid display elements ──")
        price_patterns = ["current-bid", "currentBid", "current_bid", "price", "amount", "bid-amount"]
        for pattern in price_patterns:
            els = await self.page.query_selector_all(f"[class*='{pattern}']")
            for el in els[:3]:
                cls = await el.get_attribute("class") or ""
                txt = (await el.inner_text())[:60].strip()
                if txt:
                    log(f"  [{pattern}] class='{cls[:60]}' | text='{txt}'")

        log("\n── DONE ──")
        log("Update the SELECTORS dict in bid_bot.py based on the above output.")
        log("Browser staying open for 60 seconds for manual inspection...")
        await asyncio.sleep(60)


# ── CLI Entry Point ────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="ServiceLink Auction Bid Bot")
    parser.add_argument("--url",      required=True,  help="Full URL of the listing page")
    parser.add_argument("--bid",      type=int,       help="Your max bid amount in dollars (e.g. 85001)")
    parser.add_argument("--window",   type=int,       default=BID_WINDOW_SECS,
                        help=f"Seconds before end to place bid (default: {BID_WINDOW_SECS})")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Watch only — do NOT actually place a bid")
    parser.add_argument("--discover", action="store_true",
                        help="Discover mode — map page elements to update selectors")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser headless (invisible). Default: visible browser")
    args = parser.parse_args()

    email    = os.getenv("SERVICELINK_EMAIL")
    password = os.getenv("SERVICELINK_PASSWORD")

    if not email or not password:
        print("ERROR: Set SERVICELINK_EMAIL and SERVICELINK_PASSWORD in .env")
        sys.exit(1)

    if not args.discover and not args.bid:
        print("ERROR: --bid is required unless using --discover")
        sys.exit(1)

    bot = ServiceLinkBidBot(email, password, headless=args.headless)

    try:
        await bot.start()

        if not await bot.login():
            sys.exit(1)

        await bot.navigate_to_listing(args.url)

        if args.discover:
            await bot.discover_selectors(args.url)
        else:
            await bot.watch_auction(
                bid_amount=args.bid,
                dry_run=args.dry_run,
                target_secs=args.window,
            )

    except KeyboardInterrupt:
        log("\nInterrupted by user.")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
