#!/usr/bin/env python3
"""Live market watchlist for a color terminal. No third-party packages required."""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import datetime as dt
import json
import locale
import os
import queue
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


YAHOO_ENDPOINT = "https://query2.finance.yahoo.com/v8/finance/chart"
DEFAULT_ASSETS = [
    ("BTC-USD", "Bitcoin USD", "USD"),
    ("0992.HK", "Lenovo Group", "HKD"),
    ("VOO", "Vanguard S&P 500 ETF", "USD"),
    ("VTI", "Vanguard Total Stock Market ETF", "USD"),
    ("IAU", "iShares Gold Trust", "USD"),
    ("VGT", "Vanguard Information Technology ETF", "USD"),
    ("SPCX", "SPAC and New Issue ETF", "USD"),
    ("IBIT", "iShares Bitcoin Trust ETF", "USD"),
]
RANGES = {
    "1D": ("1d", "5m"), "1W": ("5d", "30m"), "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"), "6M": ("6mo", "1d"), "1Y": ("1y", "1d"),
    "5Y": ("5y", "1wk"), "10Y": ("10y", "1wk"),
}
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "market-watchlist" / "data.json"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "market-watchlist" / "config.json"
SPARKS = "▁▂▃▄▅▆▇█"


def load_config() -> list[tuple[str, str, str]]:
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        assets = raw.get("assets")
        if isinstance(assets, list) and assets:
            return [(a["symbol"], a["name"], a["currency"]) for a in assets if all(k in a for k in ("symbol", "name", "currency"))]
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_ASSETS


def save_config(assets: list[tuple[str, str, str]]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"assets": [{"symbol": s, "name": n, "currency": c} for s, n, c in assets]}
        temp = CONFIG_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(CONFIG_PATH)
    except OSError:
        pass


@dataclass
class Quote:
    symbol: str
    name: str
    currency: str
    exchange: str
    source: str
    updated: str
    price: float
    previous: float
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    points: list[tuple[int, float]]
    stale: bool = False

    @property
    def percent(self) -> float:
        return (self.price - self.previous) / self.previous * 100 if self.previous else 0.0

    @property
    def period_start(self) -> float:
        return self.points[0][1] if self.points else self.price

    @property
    def period_change(self) -> float:
        return self.price - self.period_start

    @property
    def period_percent(self) -> float:
        return self.period_change / self.period_start * 100 if self.period_start else 0.0


def parse_quote(symbol: str, name: str, currency: str, payload: dict[str, Any], headers: Any) -> Quote:
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    raw_quote = result["indicators"]["quote"][0]
    adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
    points = []
    for index, stamp in enumerate(result["timestamp"]):
        close = adjusted[index] if index < len(adjusted) else raw_quote["close"][index]
        if close is not None:
            points.append((int(stamp), float(close)))
    if len(points) < 2:
        raise ValueError("market response contains fewer than two prices")
    last = lambda key: next((v for v in reversed(raw_quote.get(key, [])) if v is not None), None)
    value = lambda key, fallback=None: meta.get(key) if meta.get(key) is not None else fallback
    current_price = float(value("regularMarketPrice", points[-1][1]))
    daily_percent = number(meta.get("regularMarketChangePercent"))
    daily_previous = current_price / (1 + daily_percent / 100) if daily_percent is not None and daily_percent != -100 else None
    return Quote(
        symbol=symbol, name=name, currency=meta.get("currency", currency),
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName") or "Market",
        source=headers.get("X-Market-Source", "Market data"),
        updated=headers.get("X-Market-Time", dt.datetime.now(dt.UTC).isoformat()),
        price=current_price,
        previous=float(daily_previous if daily_previous is not None else value("regularMarketPreviousClose", value("previousClose", points[-2][1]))),
        open=number(value("regularMarketOpen", last("open"))),
        high=number(value("regularMarketDayHigh", last("high"))),
        low=number(value("regularMarketDayLow", last("low"))),
        volume=number(value("regularMarketVolume", last("volume"))), points=points,
    )


def number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def read_json(url: str, timeout: float, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], Any]:
    defaults = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) market-watchlist-tui/1.1"}
    request = urllib.request.Request(url, headers={**defaults, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response), response.headers


def fetch_quote(asset: tuple[str, str, str], range_label: str, timeout: float = 12) -> Quote:
    symbol, name, currency = asset
    market_range, interval = RANGES[range_label]
    query = urllib.parse.urlencode({"range": market_range, "interval": interval, "includePrePost": "false", "events": "div,splits"})
    errors = []
    try:
        payload, _ = read_json(f"{YAHOO_ENDPOINT}/{urllib.parse.quote(symbol)}?{query}", timeout)
        return parse_quote(symbol, name, currency, payload, {"X-Market-Source": "Yahoo Finance"})
    except Exception as error:
        errors.append(f"Yahoo: {error}")
    fallbacks = [coin_gecko_quote] if symbol == "BTC-USD" else [nasdaq_quote] if not symbol.endswith(".HK") else []
    if os.environ.get("TWELVE_DATA_API_KEY"):
        fallbacks.append(twelve_data_quote)
    for fallback in fallbacks:
        try:
            return fallback(asset, range_label, timeout)
        except Exception as error:
            errors.append(f"{fallback.__name__}: {error}")
    raise RuntimeError("; ".join(errors))


def quote_from_series(asset: tuple[str, str, str], source: str, exchange: str,
                      timestamps: list[int], close: list[float], opens: list[float] | None = None,
                      highs: list[float] | None = None, lows: list[float] | None = None,
                      volumes: list[float | None] | None = None, previous: float | None = None) -> Quote:
    symbol, name, currency = asset
    if len(close) < 2:
        raise ValueError("source returned fewer than two prices")
    opens = opens or close; highs = highs or close; lows = lows or close; volumes = volumes or [None] * len(close)
    return Quote(symbol, name, currency, exchange, source, dt.datetime.now(dt.UTC).isoformat(),
                 close[-1], previous or close[-2], opens[-1], highs[-1], lows[-1], volumes[-1],
                 list(zip(timestamps, close)))


def coin_gecko_quote(asset: tuple[str, str, str], range_label: str, timeout: float) -> Quote:
    days = {"1D": 1, "1W": 5, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, "5Y": 1825, "10Y": 3650}[range_label]
    query = urllib.parse.urlencode({"vs_currency": "usd", "days": days, "precision": "full"})
    payload, _ = read_json(f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?{query}", timeout)
    prices = payload["prices"]
    timestamps = [int(point[0] / 1000) for point in prices]
    close = [float(point[1]) for point in prices]
    volumes = [float(point[1]) for point in payload.get("total_volumes", [])]
    if len(volumes) != len(close):
        volumes = [None] * len(close)
    return quote_from_series(asset, "CoinGecko", "Crypto", timestamps, close, volumes=volumes)


def nasdaq_quote(asset: tuple[str, str, str], range_label: str, timeout: float) -> Quote:
    symbol = asset[0]
    headers = {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    if range_label == "1D":
        payload, _ = read_json(f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/chart?assetclass=etf", timeout, headers)
        data = payload["data"]; chart = data["chart"]
        timestamps = [int(point["x"] / 1000) for point in chart]
        close = [float(point["y"]) for point in chart]
        previous = number(str(data.get("previousClose", "")).replace("$", "").replace(",", ""))
        return quote_from_series(asset, "Nasdaq", data.get("exchange", "US Market"), timestamps, close, previous=previous)
    days = {"1W": 10, "1M": 40, "3M": 110, "6M": 210, "1Y": 380, "5Y": 1900, "10Y": 3650}[range_label]
    end = dt.date.today(); start = end - dt.timedelta(days=days)
    query = urllib.parse.urlencode({"assetclass": "etf", "fromdate": start.isoformat(), "todate": end.isoformat(), "limit": 1400 if range_label in ("5Y", "10Y") else 500})
    payload, _ = read_json(f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/historical?{query}", timeout, headers)
    rows = list(reversed(payload["data"]["tradesTable"]["rows"]))
    if range_label in ("5Y", "10Y"):
        rows = [row for index, row in enumerate(rows) if index % 5 == 0 or index == len(rows) - 1]
    numeric = lambda value: float(str(value).replace("$", "").replace(",", ""))
    timestamps = [int(dt.datetime.strptime(row["date"], "%m/%d/%Y").replace(tzinfo=dt.timezone(dt.timedelta(hours=-4))).timestamp()) for row in rows]
    return quote_from_series(asset, "Nasdaq", "US Market", timestamps,
                             [numeric(r["close"]) for r in rows], [numeric(r["open"]) for r in rows],
                             [numeric(r["high"]) for r in rows], [numeric(r["low"]) for r in rows],
                             [numeric(r["volume"]) for r in rows])


def twelve_data_quote(asset: tuple[str, str, str], range_label: str, timeout: float) -> Quote:
    symbol = "BTC/USD" if asset[0] == "BTC-USD" else asset[0]
    interval = {"5m": "5min", "30m": "30min", "1d": "1day", "1wk": "1week"}[RANGES[range_label][1]]
    count = {"1D": 96, "1W": 240, "1M": 31, "3M": 93, "6M": 186, "1Y": 366, "5Y": 262, "10Y": 520}[range_label]
    query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "outputsize": count, "order": "asc", "timezone": "UTC", "apikey": os.environ["TWELVE_DATA_API_KEY"]})
    payload, _ = read_json(f"https://api.twelvedata.com/time_series?{query}", timeout)
    rows = payload["values"]
    timestamps = [int(dt.datetime.fromisoformat(row["datetime"]).replace(tzinfo=dt.UTC).timestamp()) for row in rows]
    values = lambda key: [float(row[key]) for row in rows]
    volumes = [number(row.get("volume")) for row in rows]
    return quote_from_series(asset, "Twelve Data", payload.get("meta", {}).get("exchange", "Market"),
                             timestamps, values("close"), values("open"), values("high"), values("low"), volumes)


def money(value: float | None, currency: str, compact: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "HK$" if currency == "HKD" else "$" if currency == "USD" else f"{currency} "
    if compact and abs(value) >= 1_000_000_000:
        return f"{prefix}{value / 1_000_000_000:.2f}B"
    if compact and abs(value) >= 1_000_000:
        return f"{prefix}{value / 1_000_000:.2f}M"
    decimals = 2 if abs(value) < 1000 else 0
    return f"{prefix}{value:,.{decimals}f}"


def volume(value: float | None) -> str:
    if value is None:
        return "—"
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:.2f}{suffix}"
    return f"{value:,.0f}"


def sparkline(values: list[float], width: int) -> str:
    if not values or width <= 0:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[min(int(i * step), len(values) - 1)] for i in range(width)]
    lo, hi = min(values), max(values)
    scale = hi - lo or 1
    return "".join(SPARKS[min(7, int((value - lo) / scale * 7))] for value in values)


def braille_chart(values: list[float], width: int, height: int) -> list[str]:
    """Render a line onto a 2x4-dot Braille grid."""
    if width < 2 or height < 1 or len(values) < 2:
        return ["" for _ in range(max(1, height))]
    dots_w, dots_h = width * 2, height * 4
    lo, hi = min(values), max(values)
    scale = hi - lo or 1
    samples = []
    for x in range(dots_w):
        pos = x * (len(values) - 1) / max(1, dots_w - 1)
        left = int(pos); fraction = pos - left
        value = values[left] if left == len(values) - 1 else values[left] * (1 - fraction) + values[left + 1] * fraction
        samples.append(round((hi - value) / scale * (dots_h - 1)))
    grid = [[0] * width for _ in range(height)]
    dot_map = {(0, 0): 1, (0, 1): 2, (0, 2): 4, (1, 0): 8, (1, 1): 16, (1, 2): 32, (0, 3): 64, (1, 3): 128}
    previous = samples[0]
    for x, y in enumerate(samples):
        for plot_y in range(min(previous, y), max(previous, y) + 1):
            cell_x, sub_x = divmod(x, 2); cell_y, sub_y = divmod(plot_y, 4)
            if cell_y < height:
                grid[cell_y][cell_x] |= dot_map[(sub_x, sub_y)]
        previous = y
    return ["".join(chr(0x2800 + dots) for dots in row) for row in grid]


class App:
    def __init__(self, screen: Any):
        self.screen = screen
        self.assets = load_config()
        self.selected = 0
        self.range_index = list(RANGES).index("1Y")
        self.quotes: dict[tuple[str, str], Quote] = {}
        self.errors: dict[tuple[str, str], str] = {}
        self.pending: set[tuple[str, str]] = set()
        self.results: queue.Queue = queue.Queue()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="quotes")
        self.message = "Connecting to live markets…"
        self.detail_only = False
        self.last_refresh = 0.0
        self.running = True
        self.load_cache()

    @property
    def range_label(self) -> str:
        return list(RANGES)[self.range_index]

    def setup(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color(); curses.use_default_colors()
            curses.init_pair(1, 46, -1)   # green
            curses.init_pair(2, 203, -1)  # red
            curses.init_pair(3, 75, -1)   # blue
            curses.init_pair(4, 244, -1)  # muted
            curses.init_pair(5, 231, 24)  # selected
            curses.init_pair(6, 220, -1)  # warning
        self.refresh_all()

    def submit(self, asset: tuple[str, str, str], label: str) -> None:
        key = (asset[0], label)
        if key in self.pending:
            return
        self.pending.add(key)
        future = self.executor.submit(fetch_quote, asset, label)
        future.add_done_callback(lambda done, k=key: self.results.put((k, done)))

    def refresh_all(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_refresh < 10:
            return
        self.last_refresh = time.monotonic()
        for asset in self.assets:
            self.submit(asset, self.range_label)
        self.message = "Refreshing live prices…"

    def drain_results(self) -> None:
        changed = False
        while True:
            try:
                key, future = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(key)
            try:
                self.quotes[key] = future.result()
                self.errors.pop(key, None)
            except Exception as error:
                self.errors[key] = str(error)
            changed = True
        if changed:
            available = sum((asset[0], self.range_label) in self.quotes for asset in self.assets)
            self.message = f"Live · {available}/{len(self.assets)} quotes · auto-refresh 5m"
            self.save_cache()

    def load_cache(self) -> None:
        try:
            raw = json.loads(CACHE_PATH.read_text())
            for item in raw.get("quotes", []):
                saved = item.copy()
                range_label = saved.pop("range_label", "1Y")
                quote = Quote(**saved); quote.stale = True
                self.quotes[(quote.symbol, range_label)] = quote
        except (OSError, ValueError, TypeError):
            pass

    def save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            items = []
            for (symbol, label), quote in self.quotes.items():
                data = vars(quote).copy(); data["range_label"] = label; data["stale"] = False
                items.append(data)
            temp = CACHE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps({"quotes": items}, separators=(",", ":")))
            temp.replace(CACHE_PATH)
        except OSError:
            pass

    def run(self) -> None:
        self.setup()
        while self.running:
            self.drain_results()
            if time.monotonic() - self.last_refresh >= 300:
                self.refresh_all(True)
            self.draw()
            try:
                key = self.screen.getch()
            except KeyboardInterrupt:
                break
            if key != -1:
                self.handle_key(key)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def handle_key(self, key: int) -> None:
        height, width = self.screen.getmaxyx()
        if key in (ord("q"), ord("Q"), 3):
            self.running = False
        elif key in (curses.KEY_UP, ord("k")):
            if key == curses.KEY_UP and curses.keyname(key) == b'KEY_UP':
                pass
            self.selected = (self.selected - 1) % len(self.assets); self.ensure_detail()
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = (self.selected + 1) % len(self.assets); self.ensure_detail()
        elif key in (curses.KEY_LEFT, ord("h")):
            if width < 90 and self.detail_only:
                self.detail_only = False
            else:
                self.range_index = (self.range_index - 1) % len(RANGES); self.ensure_detail()
        elif key in (curses.KEY_RIGHT, ord("l")):
            if width < 90 and not self.detail_only:
                self.detail_only = True
            else:
                self.range_index = (self.range_index + 1) % len(RANGES); self.ensure_detail()
        elif key in (10, 13):
            if width < 90:
                self.detail_only = True
        elif key == 27:
            if width < 90 and self.detail_only:
                self.detail_only = False
            else:
                self.running = False
        elif key in (ord("r"), ord("R")):
            self.refresh_all(True)
        elif key in (ord("a"), ord("A")):
            self.add_asset()
        elif key in (ord("d"), ord("D")):
            self.remove_asset()
        elif key in (ord("J"),):  # Shift+J to move down
            self.move_asset(1)
        elif key in (ord("K"),):  # Shift+K to move up
            self.move_asset(-1)
        elif key == curses.KEY_MOUSE:
            self.handle_mouse()

    def handle_mouse(self) -> None:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return
        height, width = self.screen.getmaxyx()
        if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            if (width >= 90 or not self.detail_only) and 4 <= y < 4 + len(self.assets) * 3:
                index = (y - 4) // 3
                if 0 <= index < len(self.assets):
                    self.selected = index; self.ensure_detail()
                    if width < 90:
                        self.detail_only = True

    def add_asset(self) -> None:
        height, width = self.screen.getmaxyx()
        prompt = "Add symbol (e.g. AAPL): "
        input_x = 2 + len(prompt)
        max_len = 16
        # Switch to blocking input so getstr waits for the user (timeout(100) would otherwise make it blink/cancel in ~100ms)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        curses.echo()
        self.screen.keypad(False)
        self.screen.timeout(-1)
        # Clear and draw prompt on the line above the footer
        try:
            self.screen.move(height - 2, 0)
            self.screen.clrtoeol()
        except curses.error:
            pass
        self.text(height - 2, 2, prompt, self.color(3) | curses.A_BOLD)
        self.text(height - 2, input_x, " " * max_len, curses.A_NORMAL)
        self.screen.refresh()
        try:
            raw = self.screen.getstr(height - 2, input_x, max_len)
            symbol = raw.decode().strip().upper() if raw else ""
        except Exception:
            symbol = ""
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.screen.keypad(True)
            self.screen.timeout(100)
            # Clear prompt line so it doesn't linger after input
            try:
                self.screen.move(height - 2, 0)
                self.screen.clrtoeol()
            except curses.error:
                pass
        if not symbol:
            self.message = "Cancelled"
            return
        # Basic ticker validation: allow A-Z, 0-9, ., -, ^, = (covers US, HK, etc.) and length 1..16
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-^=")
        if len(symbol) > max_len or any(ch not in allowed for ch in symbol):
            self.message = f"Invalid symbol: {symbol}"
            return
        if any(s == symbol for s, _, _ in self.assets):
            self.message = f"{symbol} already in watchlist"
            return
        name = symbol
        # Infer HKD for .HK suffix, otherwise USD
        currency = "HKD" if symbol.endswith(".HK") else "USD"
        self.assets.insert(self.selected + 1, (symbol, name, currency))
        self.selected += 1
        save_config(self.assets)
        self.ensure_detail()
        self.refresh_all(True)
        self.message = f"Added {symbol}"

    def remove_asset(self) -> None:
        if len(self.assets) <= 1:
            self.message = "Cannot remove last asset"
            return
        symbol = self.assets[self.selected][0]
        del self.assets[self.selected]
        if self.selected >= len(self.assets):
            self.selected = len(self.assets) - 1
        save_config(self.assets)
        self.ensure_detail()
        self.refresh_all(True)
        self.message = f"Removed {symbol}"

    def move_asset(self, direction: int) -> None:
        new_index = self.selected + direction
        if 0 <= new_index < len(self.assets):
            self.assets[self.selected], self.assets[new_index] = self.assets[new_index], self.assets[self.selected]
            self.selected = new_index
            save_config(self.assets)
            self.ensure_detail()

    def ensure_detail(self) -> None:
        self.submit(self.assets[self.selected], self.range_label)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 18 or width < 52:
            self.text(1, 2, "Terminal too small", curses.A_BOLD)
            self.text(3, 2, "Resize to at least 52 × 18. Press q to quit.", self.color(4))
            self.screen.refresh(); return
        if width >= 90:
            left = min(42, max(34, width // 3))
            self.draw_watchlist(0, 0, height - 1, left)
            self.vline(0, left, height - 1)
            self.draw_detail(0, left + 1, height - 1, width - left - 1)
        elif self.detail_only:
            self.draw_detail(0, 0, height - 1, width)
        else:
            self.draw_watchlist(0, 0, height - 1, width)
        self.draw_footer(height - 1, width)
        self.screen.refresh()

    def draw_watchlist(self, top: int, left: int, height: int, width: int) -> None:
        self.text(top + 1, left + 2, "MARKETS", self.color(3) | curses.A_BOLD)
        self.text(top + 2, left + 2, "Watchlist", curses.A_BOLD)
        for index, (symbol, name, currency) in enumerate(self.assets):
            y = top + 4 + index * 3
            if y + 1 >= top + height:
                break
            selected = index == self.selected
            attr = self.color(5) if selected else curses.A_NORMAL
            quote = self.quotes.get((symbol, self.range_label))
            self.text(y, left + 2, symbol, attr | curses.A_BOLD)
            if quote:
                price = money(quote.price, quote.currency)
                change = f"{quote.period_percent:+.2f}%"
                self.right(y, left + width - 2, price, attr | curses.A_BOLD)
                change_attr = attr | self.color(1 if quote.period_percent >= 0 else 2)
                spark_width = max(4, min(12, width - len(name) - len(change) - 8))
                spark = sparkline([p[1] for p in quote.points[-80:]], spark_width)
                if selected:
                    self.fill(y, left + 1, width - 2, 1, attr)
                self.text(y + 1, left + 2, name[: max(1, width - spark_width - len(change) - 8)], attr | self.color(4))
                self.right(y + 1, left + width - 2, f"{spark} {change}", change_attr)
            else:
                label = "loading…" if (symbol, self.range_label) in self.pending else "unavailable"
                self.right(y, left + width - 2, label, attr | self.color(6))
                if selected:
                    self.fill(y, left + 1, width - 2, 1, attr)
                self.text(y + 1, left + 2, name[: width - 4], attr | self.color(4))

    def draw_detail(self, top: int, left: int, height: int, width: int) -> None:
        symbol, name, currency = self.assets[self.selected]
        quote = self.quotes.get((symbol, self.range_label))
        fallback = self.quotes.get((symbol, "1Y"))
        if quote is None and self.range_label == "1Y":
            quote = fallback
        self.text(top + 1, left + 2, f"{symbol}  {name}", curses.A_BOLD)
        if width < 90:
            self.right(top + 1, left + width - 2, "← back", self.color(3))
        if not quote:
            self.text(top + 4, left + 2, "Loading chart…" if (symbol, self.range_label) in self.pending else "Market data unavailable", self.color(6))
            return
        move = quote.period_change
        period = quote.period_percent
        self.text(top + 3, left + 2, money(quote.price, quote.currency), curses.A_BOLD)
        self.text(top + 4, left + 2, f"{move:+,.2f}  {period:+.2f}%", self.color(1 if move >= 0 else 2) | curses.A_BOLD)
        self.right(top + 3, left + width - 2, quote.exchange, self.color(4))
        values = [point[1] for point in quote.points]
        chart_top = top + 7
        stats_height = 5
        chart_height = max(3, height - chart_top - stats_height - 3)
        chart_width = max(10, width - 15)
        chart = braille_chart(values, chart_width, chart_height)
        high, low = max(values), min(values)
        self.text(chart_top, left + 2, money(high, quote.currency), self.color(4))
        self.text(chart_top + chart_height - 1, left + 2, money(low, quote.currency), self.color(4))
        chart_attr = self.color(1 if period >= 0 else 2)
        for row, line in enumerate(chart):
            self.text(chart_top + row, left + 13, line, chart_attr)
        date_fmt = "%b %d" if self.range_label not in ("5Y", "10Y") else "%b %Y" if self.range_label == "5Y" else "%Y"
        self.text(chart_top + chart_height, left + 13, dt.datetime.fromtimestamp(quote.points[0][0]).strftime(date_fmt), self.color(4))
        self.right(chart_top + chart_height, left + width - 2, dt.datetime.fromtimestamp(quote.points[-1][0]).strftime(date_fmt), self.color(4))
        range_y = chart_top + chart_height + 2
        range_text = "  ".join(f"[{label}]" if label == self.range_label else label for label in RANGES)
        self.text(range_y, left + 2, range_text[: width - 4], self.color(3) | curses.A_BOLD)
        stat_y = range_y + 2
        columns = [("OPEN", money(quote.open, quote.currency)), ("HIGH", money(quote.high, quote.currency)), ("LOW", money(quote.low, quote.currency)), ("VOLUME", volume(quote.volume))]
        column_width = max(12, (width - 4) // 4)
        for index, (label, value) in enumerate(columns):
            x = left + 2 + index * column_width
            if x + 8 < left + width:
                self.text(stat_y, x, label, self.color(4)); self.text(stat_y + 1, x, value, curses.A_BOLD)
        timestamp = parse_time(quote.updated)
        status = f"{quote.source} · {timestamp}{' · cached' if quote.stale else ''} · {self.range_label} {period:+.2f}%"
        self.text(min(top + height - 1, stat_y + 3), left + 2, status[: width - 4], self.color(4))

    def draw_footer(self, y: int, width: int) -> None:
        help_text = " ↑/↓ select   ←/→ range   a add   d remove   J/K reorder   r refresh   q quit "
        status = f" {self.message} "
        try:
            self.screen.addstr(y, 0, " " * (width - 1), curses.A_REVERSE)
            self.screen.addnstr(y, 1, help_text, width - 2, curses.A_REVERSE)
            if len(status) + len(help_text) < width - 2:
                self.screen.addnstr(y, width - len(status) - 1, status, len(status), curses.A_REVERSE)
        except curses.error:
            pass

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else curses.A_NORMAL

    def text(self, y: int, x: int, value: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if not (0 <= y < height and 0 <= x < width - 1):
            return
        try:
            self.screen.addnstr(y, x, value, width - x - 1, attr)
        except curses.error:
            pass

    def right(self, y: int, right: int, value: str, attr: int = 0) -> None:
        self.text(y, max(0, right - len(value)), value, attr)

    def fill(self, y: int, x: int, width: int, height: int, attr: int) -> None:
        for row in range(height):
            self.text(y + row, x, " " * max(0, width), attr)

    def vline(self, y: int, x: int, height: int) -> None:
        for row in range(height):
            self.text(y + row, x, "│", self.color(4))


def parse_time(value: str) -> str:
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        return stamp.strftime("updated %-I:%M %p")
    except (ValueError, TypeError):
        return "updated recently"


def smoke_test() -> int:
    print("Fetching independent market sources …")
    failures = 0
    assets = load_config()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_quote, asset, "1Y"): asset[0] for asset in assets}
        for future in concurrent.futures.as_completed(futures):
            symbol = futures[future]
            try:
                quote = future.result()
                print(f"{symbol:<8} {money(quote.price, quote.currency):>12} {quote.percent:+7.2f}%  {quote.source}  {len(quote.points)} points")
            except Exception as error:
                failures += 1; print(f"{symbol:<8} ERROR: {error}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live terminal market watchlist")
    parser.add_argument("--check", action="store_true", help="fetch all quotes without opening the TUI")
    args = parser.parse_args()
    if args.check:
        return smoke_test()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("the TUI requires an interactive terminal (use --check for a data test)")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda screen: App(screen).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
