#!/usr/bin/env python3
"""
FEST STREET EXCHANGE - a live stock-market game for college fests
=================================================================

Run it:      python fest_market.py
Big screen:  http://localhost:8000/          (put this on the projector, press F for fullscreen)
Players:     http://<your-ip>:8000/play      (phones or laptops on the same wifi - the URL is printed below)
Organiser:   http://localhost:8000/admin     (PIN is ADMIN_PIN below)

No installs needed - it only uses Python's standard library (Python 3.8+).

How the game works
------------------
* Every team starts with STARTING_CASH. Each round lasts ROUND_SECONDS (5 minutes).
* During a round, teams can BUY shares at the price on the board - or choose not to buy.
  (There is no selling: whatever you hold is valued at the latest price.)
* When the round timer hits zero the closing bell rings and every price moves randomly
  using Python's `random` module (drift + volatility + a market-wide mood).
* After round NUM_ROUNDS (24) the game ends. Highest total value (cash + shares) wins.
* Everything is saved to market_data.json after every change.
"""

import argparse
import hmac
import json
import math
import os
import random
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# =====================================================================
#  1. SETTINGS  -  the part you are meant to edit
# =====================================================================
MARKET_NAME = "The Quant Gambit Exchange"
INDEX_NAME = "FEST-20"          # the average of all 20 stocks, starts at 1000
CURRENCY = "₹"

NUM_ROUNDS = 24
ROUND_SECONDS = 5 * 60          # 5 minutes per round
STARTING_CASH = 100_000         # cash each team starts with

ADMIN_PIN = "fest123"           # needed to open the organiser page - CHANGE THIS
PORT = 8000
DATA_FILE = "market_data.json"  # all stock info is saved here
RANDOM_SEED = None              # put a number (e.g. 42) to get the same "random" game every time

# --- how prices move (all numbers are per ROUND, 0.01 = 1%) ---------
MARKET_VOL = 0.012              # size of the market-wide mood swing each round
JUMP_CHANCE = 0.04              # chance a stock gets a surprise news event in a round
JUMP_RANGE = (0.05, 0.12)       # how big a news-event jump is (5% to 12%)
MIN_PRICE = 1.00                # prices never go below this

# --- the 20 stocks ---------------------------------------------------
#  ticker, name,               start price, sector,         drift/round, volatility/round, beta
#  drift      = average pull per round (0.003 = +0.3% a round on average)
#  volatility = how wild the stock is (0.03 = typically moves about 3% a round)
#  beta       = how strongly it follows the market-wide mood (1.0 = normally)
STOCKS = [
    ("ZNTH", "Zenith Motors",     100, "Automobiles",   0.002, 0.035, 1.1),
    ("NOVA", "Nova Pharma",       250, "Healthcare",    0.003, 0.030, 0.6),
    ("BLRV", "BlueRiver Energy",   80, "Energy",        0.001, 0.040, 1.2),
    ("QNTB", "Quanta Bank",       150, "Banking",       0.002, 0.025, 1.3),
    ("LUMN", "Lumen Telecom",      60, "Telecom",       0.001, 0.020, 0.6),
    ("IRPK", "IronPeak Steel",    300, "Materials",     0.001, 0.035, 1.2),
    ("ORBT", "Orbit Airlines",     45, "Travel",        0.000, 0.060, 1.5),
    ("VEDA", "Veda Foods",        120, "Consumer",      0.002, 0.018, 0.5),
    ("PXLG", "Pixel Games",       200, "Gaming",        0.004, 0.055, 1.3),
    ("ATLS", "Atlas Logistics",    90, "Logistics",     0.002, 0.030, 1.0),
    ("SLRG", "SolarGrid",         175, "Clean energy",  0.005, 0.050, 1.2),
    ("HLXB", "Helix Biotech",      70, "Biotech",       0.004, 0.065, 0.9),
    ("CRWN", "Crown Hotels",      400, "Hospitality",   0.002, 0.028, 1.1),
    ("SWFT", "Swift Retail",       55, "Retail",        0.001, 0.035, 1.0),
    ("TERA", "Terra Mining",      130, "Mining",        0.001, 0.045, 1.3),
    ("ECHO", "Echo Media",        220, "Media",         0.002, 0.040, 1.0),
    ("SPRK", "Spark Fintech",      35, "Fintech",       0.005, 0.070, 1.4),
    ("VRTX", "Vertex Software",   160, "Software",      0.004, 0.040, 1.2),
    ("HRBR", "Harbor Shipping",   110, "Shipping",      0.001, 0.035, 1.1),
    ("MSCT", "Mosaic Textiles",   280, "Textiles",      0.001, 0.022, 0.7),
]

# --- MANUAL VOLATILITY OVERRIDES -------------------------------------
# Format:  (round, "stock name or ticker", volatility)
# In that round, that stock uses YOUR volatility instead of its normal one.
# 0.20 means it can swing roughly 20% in that round. Examples:
#     (5,  "Spark Fintech", 0.20),
#     (12, "Orbit Airlines", 0.15),
# You can ALSO add overrides live from the organiser page while the game runs.
VOLATILITY_OVERRIDES = [
    # (5, "Spark Fintech", 0.20),
]

# Headlines for random news events (shown only AFTER the move happens)
UP_NEWS = [
    "{name} beats earnings expectations",
    "{name} wins a blockbuster contract",
    "Analysts upgrade {name} to strong buy",
    "{name} announces a surprise dividend",
    "A big-name investor takes a large stake in {name}",
    "{name} unveils a record-breaking product launch",
]
DOWN_NEWS = [
    "{name} misses earnings estimates",
    "{name} hit by a regulatory investigation",
    "Analysts downgrade {name} to sell",
    "{name} CEO resigns unexpectedly",
    "Supply-chain trouble rattles {name}",
    "{name} recalls its flagship product",
]

market = None  # the Market object, created in main()


# =====================================================================
#  2. THE MARKET ENGINE
# =====================================================================
def find_ticker(text, stocks):
    """Match 'spark fintech' or 'SPRK' to a ticker; None if not found."""
    t = str(text).strip().lower()
    for tk, s in stocks.items():
        if t == tk.lower() or t == s["name"].lower():
            return tk
    return None


def clean_name(name):
    return " ".join(str(name).split())[:24]


def fail(msg):
    return {"ok": False, "error": msg}


class Market:
    def __init__(self, resume=False):
        self.lock = threading.RLock()
        self.join_url = ""
        if resume and os.path.exists(DATA_FILE):
            self.data = self._load()
            print(f"  Resumed the game saved in {DATA_FILE} (it is paused - press Resume on the organiser page).")
        else:
            self.data = self._fresh_state()
        self.save()
        threading.Thread(target=self._clock_loop, daemon=True).start()

    # ---------- setup / saving -------------------------------------
    def _fresh_state(self):
        stocks = {}
        for ticker, name, price, sector, drift, vol, beta in STOCKS:
            stocks[ticker] = {
                "name": name,
                "sector": sector,
                "start_price": float(price),
                "price": float(price),
                "drift": drift,
                "base_volatility": vol,
                "beta": beta,
                "history": [{
                    "round": 0, "price": float(price), "change_pct": 0.0,
                    "volatility_used": None, "overridden": False, "event": None,
                }],
            }
        overrides = []
        for rnd, who, vol in VOLATILITY_OVERRIDES:
            tk = find_ticker(who, stocks)
            if tk is None or not (1 <= rnd <= NUM_ROUNDS) or vol <= 0:
                print(f"  ! Ignoring a bad entry in VOLATILITY_OVERRIDES: {(rnd, who, vol)}")
                continue
            overrides.append({"round": rnd, "stock": tk, "volatility": vol, "source": "settings"})
        return {
            "meta": {
                "market_name": MARKET_NAME, "num_rounds": NUM_ROUNDS,
                "round_seconds": ROUND_SECONDS, "starting_cash": STARTING_CASH,
                "currency": CURRENCY, "created_at": time.time(),
            },
            "game": {
                "status": "waiting",      # waiting -> running <-> paused -> finished
                "current_round": 1,
                "rounds_closed": 0,
                "round_ends_at": None,
                "paused_remaining": ROUND_SECONDS,
                "last_close": None,
                "last_close_at": None,
            },
            "index_history": [{"round": 0, "index": 1000.0, "change_pct": 0.0, "market_shock_pct": 0.0}],
            "stocks": stocks,
            "overrides": overrides,
            "news": [],
            "teams": {},
        }

    def _load(self):
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        g = d["game"]
        if g["status"] == "running":
            saved = d["meta"].get("saved_at", time.time())
            g["paused_remaining"] = max(30, (g["round_ends_at"] or saved) - saved)
            g["status"] = "paused"
            g["round_ends_at"] = None
        return d

    def save(self):
        with self.lock:
            self.data["meta"]["saved_at"] = time.time()
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)

    def _news(self, rnd, text, kind="info"):
        self.data["news"].append({"round": rnd, "text": text, "kind": kind})
        del self.data["news"][:-60]

    # ---------- the clock ------------------------------------------
    def _clock_loop(self):
        while True:
            time.sleep(0.25)
            with self.lock:
                g = self.data["game"]
                if g["status"] == "running" and time.time() >= g["round_ends_at"]:
                    self.close_round()

    # ---------- the heart of the game: moving the prices -----------
    def close_round(self):
        """Ring the closing bell: every price moves, teams get re-valued, next round opens."""
        with self.lock:
            d, g = self.data, self.data["game"]
            if g["status"] not in ("running", "paused"):
                return False
            rnd = g["current_round"]

            # one shared "market mood" for the round - makes stocks move together a bit
            market_shock = random.gauss(0, MARKET_VOL)
            forced = {o["stock"]: o["volatility"] for o in d["overrides"] if o["round"] == rnd}

            moves = []
            for tk, s in d["stocks"].items():
                overridden = tk in forced
                vol = forced[tk] if overridden else s["base_volatility"]

                # return = beta * market mood + random draw around the stock's drift
                ret = s["beta"] * market_shock + random.gauss(s["drift"], vol)

                jump = 0.0
                if not overridden and random.random() < JUMP_CHANCE:
                    jump = random.choice((-1, 1)) * random.uniform(*JUMP_RANGE)
                    ret += jump

                ret = max(-0.5, min(0.5, ret))          # safety cap: max +-50% in one round
                old = s["price"]
                new = round(max(MIN_PRICE, old * math.exp(ret)), 2)
                change = round((new / old - 1) * 100, 2)

                event, kind = None, "info"
                if overridden:
                    verb = "surges" if change >= 0 else "plunges"
                    event, kind = f"{s['name']} {verb} {abs(change):.1f}% on wild trading", "alert"
                elif jump and (jump > 0) == (change > 0):
                    event = random.choice(UP_NEWS if jump > 0 else DOWN_NEWS).format(name=s["name"])
                    kind = "up" if jump > 0 else "down"
                if event:
                    self._news(rnd, event, kind)

                s["price"] = new
                s["history"].append({
                    "round": rnd, "price": new, "change_pct": change,
                    "volatility_used": vol, "overridden": overridden, "event": event,
                })
                moves.append((tk, change))

            # the FEST-20 index = average of (price / start price) * 1000
            stocks = d["stocks"].values()
            idx = round(1000 * sum(s["price"] / s["start_price"] for s in stocks) / len(stocks), 2)
            prev_idx = d["index_history"][-1]["index"]
            idx_change = round((idx / prev_idx - 1) * 100, 2)
            d["index_history"].append({
                "round": rnd, "index": idx, "change_pct": idx_change,
                "market_shock_pct": round(market_shock * 100, 3),
            })

            # snapshot every team's value
            for t in d["teams"].values():
                t["value_history"].append({"round": rnd, "value": self._team_value(t)})

            ordered = sorted(moves, key=lambda m: m[1])
            pack = lambda lst: [{"ticker": tk, "name": d["stocks"][tk]["name"], "change_pct": c} for tk, c in lst]
            g["last_close"] = {
                "round": rnd,
                "index_change_pct": idx_change,
                "gainers": pack([m for m in reversed(ordered) if m[1] > 0][:3]),
                "losers": pack([m for m in ordered if m[1] < 0][:3]),
            }
            g["last_close_at"] = time.time()
            g["rounds_closed"] = rnd
            direction = "up" if idx_change >= 0 else "down"
            self._news(rnd, f"Round {rnd} closed: {INDEX_NAME} {direction} {abs(idx_change):.2f}%", "info")

            if rnd >= NUM_ROUNDS:
                g["status"] = "finished"
                g["round_ends_at"] = None
                self._news(rnd, "The final bell has rung. Results are in.", "info")
            else:
                g["current_round"] = rnd + 1
                if g["status"] == "running":
                    g["round_ends_at"] = time.time() + ROUND_SECONDS
                else:
                    g["paused_remaining"] = ROUND_SECONDS
            self.save()
            return True

    # ---------- teams & trading ------------------------------------
    def _team_value(self, team):
        stocks = self.data["stocks"]
        held = sum(h["shares"] * stocks[tk]["price"] for tk, h in team["holdings"].items())
        return round(team["cash"] + held, 2)

    def _leaderboard(self):
        rows = [{"name": t["name"], "value": self._team_value(t), "cash": t["cash"], "joined": t["joined_at"]}
                for t in self.data["teams"].values()]
        rows.sort(key=lambda r: (-r["value"], r["joined"]))
        return rows

    def join(self, name):
        name = clean_name(name)
        if len(name) < 2:
            return fail("Team name needs at least 2 characters")
        with self.lock:
            key = name.lower()
            if key not in self.data["teams"]:
                self.data["teams"][key] = {
                    "name": name, "cash": float(STARTING_CASH), "holdings": {}, "trades": [],
                    "value_history": [{"round": self.data["game"]["rounds_closed"], "value": float(STARTING_CASH)}],
                    "joined_at": time.time(),
                }
                self.save()
            return {"ok": True, "name": self.data["teams"][key]["name"]}

    def buy(self, name, ticker, shares, rnd):
        with self.lock:
            d, g = self.data, self.data["game"]
            team = d["teams"].get(clean_name(name).lower())
            if team is None:
                return fail("Team not found - please join again")
            if g["status"] != "running":
                return fail("Trading is closed right now")
            if rnd != g["current_round"]:
                return fail("The round just changed - prices have updated. Try again.")
            ticker = str(ticker).upper()
            stock = d["stocks"].get(ticker)
            if stock is None:
                return fail("Unknown stock")
            try:
                shares = int(shares)
            except (TypeError, ValueError):
                return fail("Enter a whole number of shares")
            if shares < 1 or shares > 1_000_000:
                return fail("Enter at least 1 share")
            price = stock["price"]
            cost = round(price * shares, 2)
            if cost > team["cash"] + 0.001:
                return fail("Not enough cash for that many shares")
            team["cash"] = round(team["cash"] - cost, 2)
            h = team["holdings"].setdefault(ticker, {"shares": 0, "cost": 0.0})
            h["shares"] += shares
            h["cost"] = round(h["cost"] + cost, 2)
            team["trades"].append({"round": rnd, "ticker": ticker, "shares": shares, "price": price, "total": cost})
            self.save()
            return {"ok": True, "message": f"Bought {shares} x {ticker} at {price:.2f}"}

    def _me(self, team, board):
        stocks = self.data["stocks"]
        holdings = []
        for tk, h in team["holdings"].items():
            price = stocks[tk]["price"]
            value = round(h["shares"] * price, 2)
            holdings.append({
                "ticker": tk, "name": stocks[tk]["name"], "shares": h["shares"],
                "avg_cost": round(h["cost"] / h["shares"], 2), "price": price, "value": value,
                "pl": round(value - h["cost"], 2),
                "pl_pct": round((value / h["cost"] - 1) * 100, 2) if h["cost"] else 0.0,
            })
        rank = next((i + 1 for i, r in enumerate(board) if r["name"] == team["name"]), None)
        return {
            "name": team["name"], "cash": team["cash"], "value": self._team_value(team),
            "rank": rank, "holdings": holdings, "trades": team["trades"][-15:][::-1],
        }

    # ---------- what the web pages see -----------------------------
    def public_state(self, team_name=None):
        with self.lock:
            d, g = self.data, self.data["game"]
            if g["status"] == "running":
                left = max(0.0, g["round_ends_at"] - time.time())
            elif g["status"] == "paused":
                left = g["paused_remaining"]
            elif g["status"] == "waiting":
                left = ROUND_SECONDS
            else:
                left = 0
            stocks = []
            for tk, s in d["stocks"].items():
                last = s["history"][-1]
                stocks.append({
                    "ticker": tk, "name": s["name"], "sector": s["sector"], "price": s["price"],
                    "change_pct": last["change_pct"], "overridden": last["overridden"],
                    "history": [h["price"] for h in s["history"]],
                })
            idx = d["index_history"][-1]
            board = self._leaderboard()
            state = {
                "meta": {
                    "market_name": MARKET_NAME, "index_name": INDEX_NAME, "currency": CURRENCY,
                    "round_seconds": ROUND_SECONDS, "starting_cash": STARTING_CASH, "join_url": self.join_url,
                },
                "game": {
                    "status": g["status"], "current_round": g["current_round"], "num_rounds": NUM_ROUNDS,
                    "rounds_closed": g["rounds_closed"], "seconds_left": round(left, 2),
                    "last_close": g["last_close"],
                },
                "stocks": stocks,
                "index": {"value": idx["index"], "change_pct": idx["change_pct"]},
                "leaderboard": [{"rank": i + 1, "name": r["name"], "value": r["value"]}
                                for i, r in enumerate(board[:20])],
                "team_count": len(board),
                "news": d["news"][-12:][::-1],
                "me": None,
            }
            if team_name:
                team = d["teams"].get(clean_name(team_name).lower())
                if team:
                    state["me"] = self._me(team, board)
            return state

    def admin_state(self):
        with self.lock:
            s = self.public_state()
            d = self.data
            s["overrides"] = sorted(d["overrides"], key=lambda o: (o["round"], o["stock"]))
            s["stock_info"] = [{"ticker": tk, "name": st["name"], "base_volatility": st["base_volatility"],
                                "drift": st["drift"]} for tk, st in d["stocks"].items()]
            return s

    # ---------- organiser actions ----------------------------------
    def admin_action(self, action, body):
        with self.lock:
            d, g = self.data, self.data["game"]

            if action == "start":
                if g["status"] != "waiting":
                    return fail("The game has already started")
                g["status"] = "running"
                g["current_round"] = 1
                g["round_ends_at"] = time.time() + ROUND_SECONDS
                self._news(0, "The opening bell has rung. Trading is open.", "info")
                self.save()
                return {"ok": True, "message": "Game started - Round 1 is open"}

            if action == "pause":
                if g["status"] != "running":
                    return fail("The game is not running")
                g["paused_remaining"] = max(0.0, g["round_ends_at"] - time.time())
                g["status"] = "paused"
                g["round_ends_at"] = None
                self.save()
                return {"ok": True, "message": "Trading halted"}

            if action == "resume":
                if g["status"] != "paused":
                    return fail("The game is not paused")
                g["status"] = "running"
                g["round_ends_at"] = time.time() + g["paused_remaining"]
                self.save()
                return {"ok": True, "message": "Trading resumed"}

            if action == "next":
                if not self.close_round():
                    return fail("Nothing to close right now")
                return {"ok": True, "message": "Round closed - prices updated"}

            if action == "extend":
                secs = 60
                if g["status"] == "running":
                    g["round_ends_at"] += secs
                elif g["status"] in ("paused", "waiting"):
                    g["paused_remaining"] += secs
                else:
                    return fail("The game is over")
                self.save()
                return {"ok": True, "message": "Added 1 minute"}

            if action == "reset":
                self.data = self._fresh_state()
                self.save()
                return {"ok": True, "message": "Game reset (all teams removed)"}

            if action == "override_add":
                if g["status"] == "finished":
                    return fail("The game is over")
                try:
                    rnd = int(body.get("round"))
                    vol = float(body.get("volatility"))
                except (TypeError, ValueError):
                    return fail("Enter a round number and a volatility")
                tk = find_ticker(body.get("stock", ""), d["stocks"])
                if tk is None:
                    return fail("Unknown stock")
                if not (1 <= rnd <= NUM_ROUNDS):
                    return fail(f"Round must be between 1 and {NUM_ROUNDS}")
                if rnd <= g["rounds_closed"]:
                    return fail(f"Round {rnd} has already been played")
                if not (0 < vol <= 1):
                    return fail("Volatility must be between 0% and 100%")
                d["overrides"] = [o for o in d["overrides"] if not (o["round"] == rnd and o["stock"] == tk)]
                d["overrides"].append({"round": rnd, "stock": tk, "volatility": vol, "source": "organiser"})
                self.save()
                return {"ok": True, "message": f"Round {rnd}: {tk} volatility set to {vol * 100:.1f}%"}

            if action == "override_delete":
                try:
                    rnd = int(body.get("round"))
                except (TypeError, ValueError):
                    return fail("Bad request")
                tk = str(body.get("stock", "")).upper()
                if rnd <= g["rounds_closed"]:
                    return fail("That round has already been played")
                before = len(d["overrides"])
                d["overrides"] = [o for o in d["overrides"] if not (o["round"] == rnd and o["stock"] == tk)]
                if len(d["overrides"]) == before:
                    return fail("Override not found")
                self.save()
                return {"ok": True, "message": "Override removed"}

            return fail("Unknown action")


# =====================================================================
#  3. THE WEB PAGES  (HTML / CSS / JavaScript kept inside Python strings)
#     The look: an old railway-station split-flap board, but for stocks.
# =====================================================================
BASE_CSS = r"""
:root{
  --wall:#0f2a2d; --wall2:#0b2023; --flap:#1c1f22; --flap2:#2a2e32; --line:#27494c;
  --cream:#f2e8cf; --mute:#8fb0ac; --up:#5fe08a; --down:#ff6a55; --amber:#ffb02e; --r:6px;
  --font:"Bahnschrift","DIN Alternate","Roboto Condensed","Arial Narrow","Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%}
body{background:var(--wall);color:var(--cream);font-family:var(--font);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--mute)} .amber{color:var(--amber)}
.hidden{display:none!important}
button{font:inherit;font-weight:700;cursor:pointer;border:0;border-radius:var(--r);padding:11px 16px;background:var(--flap2);color:var(--cream)}
button:hover{background:#353a3f}
button.go{background:var(--up);color:#06230f} button.go:hover{background:#7aeaa0}
button.warn{background:var(--amber);color:#2b1a00} button.warn:hover{background:#ffc15a}
button.stop{background:var(--down);color:#2c0803} button.stop:hover{background:#ff8672}
button:disabled{opacity:.35;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
input,select{font:inherit;color:var(--cream);background:var(--wall2);border:1px solid var(--line);border-radius:var(--r);padding:11px 12px;width:100%}
.panel{background:var(--wall2);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}
.panel h3{font-size:1.05rem;font-weight:800;margin-bottom:10px}

/* split-flap tiles */
.flaps{display:inline-flex;gap:3px;align-items:center;font-weight:800}
.fl{display:inline-block;position:relative;width:.7em;height:1.2em;line-height:1.2em;text-align:center;background:var(--flap);color:var(--cream);border-radius:3px;overflow:hidden;transform-origin:50% 50%;animation:flip .55s cubic-bezier(.25,.9,.3,1) both;animation-delay:var(--d,0ms)}
.fl::after{content:"";position:absolute;left:0;right:0;top:50%;height:2px;margin-top:-1px;background:#000b;box-shadow:0 1px 0 #ffffff14}
.sep{display:inline-block;padding:0 1px;color:var(--cream);font-weight:800}
.fl.tick{animation:flip2 .4s cubic-bezier(.25,.9,.3,1) both}
@keyframes flip{0%{transform:scaleY(.05);filter:brightness(1.8)}60%{filter:brightness(1.2)}100%{transform:scaleY(1);filter:none}}
@keyframes flip2{0%{transform:scaleY(.05);filter:brightness(1.8)}60%{filter:brightness(1.2)}100%{transform:scaleY(1);filter:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

.spark{width:100%;height:34px;display:block}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--cream);color:#10282b;padding:12px 18px;border-radius:var(--r);font-weight:700;z-index:99;max-width:92vw;text-align:center}
.toast.bad{background:var(--down);color:#2c0803}
"""

COMMON_JS = r"""
const $ = (s, r=document) => r.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let CUR = '₹';
const num = (n, d=2) => Number(n).toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d});
const money = (n, d=2) => CUR + num(n, d);
const pct = n => (n>0?'+':'') + Number(n).toFixed(2) + '%';
const arrow = n => n>0 ? '▲' : n<0 ? '▼' : '–';
const cls = n => n>0 ? 'up' : n<0 ? 'down' : 'flat';
const mmss = sec => { sec = Math.max(0, Math.ceil(sec)); return String(Math.floor(sec/60)).padStart(2,'0') + ':' + String(sec%60).padStart(2,'0'); };
function setHTML(el, html){ if(el._h !== html){ el.innerHTML = html; el._h = html; } }
function flaps(str, d0=0, step=35){
  let i = 0;
  return [...String(str)].map(ch => {
    if(/[0-9A-Za-z]/.test(ch)){ const t = `<span class="fl" style="--d:${d0 + i*step}ms">${ch}</span>`; i++; return t; }
    return `<span class="sep">${ch === ' ' ? '&nbsp;' : ch}</span>`;
  }).join('');
}
function spark(vals){
  const w=120, h=36, p=3;
  if(vals.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="0" y1="${h/2}" x2="${w}" y2="${h/2}" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3 4" vector-effect="non-scaling-stroke" opacity=".5"/></svg>`;
  const mn = Math.min(...vals), mx = Math.max(...vals), rng = (mx-mn) || 1, step = w/(vals.length-1);
  const line = vals.map((v,i) => (i*step).toFixed(1) + ',' + (h-p-((v-mn)/rng)*(h-2*p)).toFixed(1)).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polygon points="0,${h} ${line} ${w},${h}" fill="currentColor" opacity=".13"/><polyline points="${line}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`;
}
function setTile(id, ch){
  const t = $('#'+id); if(t && t.textContent !== ch){ t.textContent = ch; t.classList.remove('tick'); void t.offsetWidth; t.classList.add('tick'); }
}
let _toastT;
function toast(msg, bad){
  let t = $('#toast'); if(!t){ t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.className = 'toast' + (bad ? ' bad' : ''); t.textContent = msg; t.style.display = 'block';
  clearTimeout(_toastT); _toastT = setTimeout(() => t.style.display = 'none', 2600);
}
async function post(path, body, headers){
  const r = await fetch(path, {method:'POST', headers:Object.assign({'Content-Type':'application/json'}, headers||{}), body:JSON.stringify(body||{})});
  return r.json();
}
"""


def page(title, css, body, js):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            "<title>" + title + "</title><style>" + BASE_CSS + css + "</style></head><body>"
            + body + "<script>" + COMMON_JS + js + "</script></body></html>")


# ---------------------------------------------------------------------
#  BIG-SCREEN DISPLAY  ( / )
# ---------------------------------------------------------------------
DISPLAY_CSS = r"""
.app{min-height:100vh;display:flex;flex-direction:column;padding:18px 24px 14px;gap:16px}
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid var(--line)}
.name{font-size:clamp(26px,2.6vw,48px);font-weight:800;line-height:1;letter-spacing:.01em}
.statusline{margin-top:8px;font-size:clamp(14px,1.15vw,21px);color:var(--mute)}
.statusline.live{color:var(--up)} .statusline.halt{color:var(--amber)}
.grp{display:flex;flex-direction:column;gap:6px}
.gl{font-size:clamp(13px,.95vw,17px);color:var(--mute)}
.grp .flaps{font-size:clamp(28px,3.1vw,56px)}
.grp .flaps.small{font-size:clamp(22px,2.2vw,40px)}
.of{font-size:clamp(16px,1.5vw,28px);color:var(--mute);font-weight:700;margin:0 8px}
.timer.urgent .flaps{color:var(--down)} .timer.urgent .fl{color:var(--down)}
.timer .bar{height:5px;background:var(--flap);border-radius:3px;overflow:hidden;margin-top:2px}
.timer .bar i{display:block;height:100%;background:var(--amber);width:100%;transition:width .5s linear}
.timer.urgent .bar i{background:var(--down)}
.idxrow{display:flex;align-items:center;gap:12px}
.idxrow .chg{font-size:clamp(15px,1.3vw,24px);font-weight:800}

.main{display:grid;grid-template-columns:minmax(0,1fr) clamp(290px,21vw,400px);gap:20px;flex:1}
.board{display:grid;grid-template-columns:1fr 1fr;grid-auto-flow:column;grid-template-rows:repeat(10,minmax(58px,1fr));gap:9px 22px}
.row{display:grid;grid-template-columns:6px minmax(0,1fr) 96px auto 118px;align-items:center;gap:14px;background:var(--wall2);border:1px solid var(--line);border-radius:var(--r);padding:6px 14px 6px 0;overflow:hidden}
.row .bar{align-self:stretch;background:var(--mute);opacity:.5}
.row.up .bar{background:var(--up);opacity:1} .row.down .bar{background:var(--down);opacity:1}
.who{min-width:0;display:flex;flex-direction:column;gap:2px}
.tk{font-size:clamp(20px,1.7vw,32px);font-weight:800;line-height:1;letter-spacing:.02em}
.nm{font-size:clamp(12px,.9vw,16px);color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sp{height:34px}
.row .flaps{font-size:clamp(20px,1.8vw,34px)}
.cur{font-size:.6em;color:var(--mute);margin-right:4px;font-weight:700}
.chgcell{text-align:right;font-weight:800;font-size:clamp(15px,1.25vw,23px);white-space:nowrap}
.bolt{color:var(--amber)}

.side{display:flex;flex-direction:column;gap:16px;min-width:0}
.lb{display:grid;grid-template-columns:30px 1fr auto;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line);font-size:clamp(15px,1.15vw,21px)}
.lb:last-child{border-bottom:0}
.lb .rk{color:var(--mute);font-weight:800} .lb.top3 .rk{color:var(--amber)}
.lb .tn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700}
.lb .tv{font-weight:800}
.empty{color:var(--mute);font-size:clamp(14px,1vw,18px);line-height:1.4}
.nws{padding:8px 0;border-bottom:1px solid var(--line);font-size:clamp(14px,1.05vw,19px);line-height:1.3}
.nws:last-child{border-bottom:0}
.nws small{display:block;color:var(--mute);font-size:.8em;margin-bottom:2px}
.nws.up{color:var(--cream);box-shadow:inset 3px 0 0 var(--up);padding-left:10px}
.nws.down{color:var(--cream);box-shadow:inset 3px 0 0 var(--down);padding-left:10px}
.nws.alert{color:var(--cream);box-shadow:inset 3px 0 0 var(--amber);padding-left:10px}
.foot{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;color:var(--mute);font-size:clamp(13px,.95vw,17px)}
.conn{position:fixed;top:0;left:0;right:0;background:var(--down);color:#2c0803;text-align:center;padding:6px;font-weight:800;z-index:60}

.overlay{position:fixed;inset:0;background:rgba(6,20,22,.9);display:grid;place-items:center;z-index:50;padding:24px;cursor:pointer}
.ov{background:var(--flap);border:1px solid var(--line);border-radius:var(--r);padding:clamp(24px,3vw,52px);width:min(980px,94vw);text-align:center}
.ov h1{font-size:clamp(34px,4.6vw,84px);line-height:1;font-weight:800;margin-bottom:10px}
.ov .sub{font-size:clamp(16px,1.6vw,28px);color:var(--mute);margin-bottom:26px}
.ov .cols{display:grid;grid-template-columns:1fr 1fr;gap:28px;text-align:left}
.ov h4{font-size:clamp(16px,1.4vw,24px);margin-bottom:8px}
.ov .r{display:grid;grid-template-columns:auto 1fr auto;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);font-size:clamp(15px,1.4vw,26px)}
.ov .r b{font-weight:800} .ov .r span{color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov .next{margin-top:24px;font-size:clamp(16px,1.5vw,26px);color:var(--amber);font-weight:700}
.pod{display:grid;grid-template-columns:70px 1fr auto;align-items:center;gap:14px;padding:12px 4px;border-bottom:1px solid var(--line);font-size:clamp(20px,2.4vw,44px);text-align:left}
.pod:last-of-type{border-bottom:0} .pod b{font-weight:800} .pod i{font-style:normal;color:var(--amber);font-weight:800}
.hint{margin-top:18px;color:var(--mute);font-size:clamp(13px,1vw,17px)}

@media (max-width:1500px){ .row{grid-template-columns:6px minmax(0,1fr) auto 112px} .row .sp{display:none} }
@media (max-width:1100px){ .main{grid-template-columns:1fr} .board{grid-template-columns:1fr;grid-auto-flow:row;grid-template-rows:none} }
"""

DISPLAY_BODY = r"""
<div id="conn" class="conn hidden">Lost connection to the market. Trying again...</div>
<div class="app">
  <header class="top">
    <div>
      <div id="mname" class="name">The Quant Gambit Exchange</div>
      <div id="status" class="statusline">Connecting...</div>
    </div>
    <div class="grp"><div class="gl">Round</div>
      <div class="idxrow"><span id="rnum" class="flaps"></span><span class="of" id="rtot"></span></div></div>
    <div class="grp timer" id="timer"><div class="gl" id="tlabel">Closing bell in</div>
      <div class="flaps" id="clock"><span class="fl" id="c0">0</span><span class="fl" id="c1">5</span><span class="sep">:</span><span class="fl" id="c2">0</span><span class="fl" id="c3">0</span></div>
      <div class="bar"><i id="bar"></i></div></div>
    <div class="grp"><div class="gl" id="iname">FEST-20</div>
      <div class="idxrow"><span id="ival" class="flaps small"></span><span id="ichg" class="chg"></span></div></div>
  </header>
  <main class="main">
    <section id="board" class="board"></section>
    <aside class="side">
      <div class="panel"><h3>Leaderboard</h3><div id="lb"></div></div>
      <div class="panel"><h3>Market news</h3><div id="news"></div></div>
    </aside>
  </main>
  <footer class="foot"><span id="joinline"></span><span id="snd">Click once anywhere to turn on the bell sound. Press F for fullscreen.</span></footer>
</div>
<div id="overlay" class="overlay hidden"></div>
"""

DISPLAY_JS = r"""
let S = null, fetchedAt = 0, lastClosed = null, AC = null, ovTimer = null;
const clockTiles = {};

async function poll(){
  try{
    const r = await fetch('/api/state', {cache:'no-store'});
    S = await r.json(); fetchedAt = performance.now();
    $('#conn').classList.add('hidden');
    render();
  }catch(e){ $('#conn').classList.remove('hidden'); }
}

function row(s, i){
  const c = cls(s.change_pct), trend = cls(s.price - s.history[0]);
  const price = num(s.price);
  return `<div class="row ${c}"><i class="bar"></i>
    <div class="who"><span class="tk">${s.ticker}</span><span class="nm">${esc(s.name)}</span></div>
    <div class="sp ${trend}">${spark(s.history)}</div>
    <div class="flaps"><span class="cur">${CUR}</span>${flaps(price, (i%10)*45)}</div>
    <div class="chgcell ${c}">${arrow(s.change_pct)} ${Math.abs(s.change_pct).toFixed(2)}%${s.overridden ? ' <span class="bolt">⚡</span>' : ''}</div></div>`;
}

function render(){
  const g = S.game, m = S.meta; CUR = m.currency;
  document.title = m.market_name;
  $('#mname').textContent = m.market_name;
  $('#iname').textContent = m.index_name;
  $('#rtot').textContent = 'of ' + g.num_rounds;
  setHTML($('#rnum'), flaps(String(g.status === 'finished' ? g.num_rounds : g.current_round).padStart(2,'0')));
  const st = {
    waiting:['The market opens soon','' ],
    running:['Trading is open. Buy before the bell.','live'],
    paused:['Trading halted','halt'],
    finished:['The market is closed. Final results are in.','']
  }[g.status];
  const sl = $('#status'); sl.textContent = st[0]; sl.className = 'statusline ' + st[1];
  $('#tlabel').textContent = {waiting:'Opens with a round of', running:'Closing bell in', paused:'Paused at', finished:'Game over'}[g.status];
  setHTML($('#ival'), flaps(num(S.index.value)));
  const ic = S.index.change_pct, el = $('#ichg'); el.textContent = arrow(ic) + ' ' + Math.abs(ic).toFixed(2) + '%'; el.className = 'chg ' + cls(ic);
  setHTML($('#board'), S.stocks.map(row).join(''));
  setHTML($('#lb'), S.leaderboard.length
    ? S.leaderboard.slice(0,8).map((t,i) => `<div class="lb ${i<3?'top3':''}"><span class="rk">${i+1}</span><span class="tn">${esc(t.name)}</span><span class="tv">${money(t.value,0)}</span></div>`).join('')
    : '<div class="empty">No teams yet. Join on your phone to appear here.</div>');
  setHTML($('#news'), S.news.length
    ? S.news.slice(0,7).map(n => `<div class="nws ${n.kind}"><small>${n.round ? 'Round ' + n.round : 'Opening'}</small>${esc(n.text)}</div>`).join('')
    : '<div class="empty">Headlines appear here after each round closes.</div>');
  $('#joinline').textContent = m.join_url ? 'Play on your phone at ' + m.join_url : '';
  watchRounds();
}

function tick(){
  if(!S) return;
  const g = S.game; let left = g.seconds_left;
  if(g.status === 'running') left -= (performance.now() - fetchedAt)/1000;
  left = Math.max(0, left);
  const t = mmss(left);
  setTile('c0', t[0]); setTile('c1', t[1]); setTile('c2', t[3]); setTile('c3', t[4]);
  $('#bar').style.width = (g.status === 'finished' ? 0 : 100*left/S.meta.round_seconds) + '%';
  $('#timer').classList.toggle('urgent', g.status === 'running' && left <= 30);
}

function ov(html, sticky){
  const o = $('#overlay'); o.innerHTML = html; o.classList.remove('hidden');
  clearTimeout(ovTimer); if(!sticky) ovTimer = setTimeout(hideOv, 10000);
}
function hideOv(){ $('#overlay').classList.add('hidden'); clearTimeout(ovTimer); }
$('#overlay').addEventListener('click', hideOv);

function showBell(){
  const lc = S.game.last_close; if(!lc) return;
  const list = a => a.length ? a.map(x => `<div class="r"><b>${x.ticker}</b><span>${esc(x.name)}</span><b class="${cls(x.change_pct)}">${pct(x.change_pct)}</b></div>`).join('') : '<div class="r"><span>None this round</span></div>';
  const more = lc.round < S.game.num_rounds;
  ov(`<div class="ov"><h1>Round ${lc.round} is closed</h1>
    <div class="sub">${esc(S.meta.index_name)} <b class="${cls(lc.index_change_pct)}">${arrow(lc.index_change_pct)} ${Math.abs(lc.index_change_pct).toFixed(2)}%</b></div>
    <div class="cols"><div><h4 class="up">Biggest gainers</h4>${list(lc.gainers)}</div><div><h4 class="down">Biggest losers</h4>${list(lc.losers)}</div></div>
    ${more ? `<div class="next">Round ${lc.round + 1} is open. Place your buys.</div>` : ''}</div>`);
}
function showFinal(){
  const top = S.leaderboard.slice(0,3), medals = ['1st','2nd','3rd'];
  ov(`<div class="ov"><h1>Final results</h1><div class="sub">Cash plus shares at closing prices</div>
    ${top.length ? top.map((t,i) => `<div class="pod"><span class="amber">${medals[i]}</span><b>${esc(t.name)}</b><i>${money(t.value,0)}</i></div>`).join('') : '<div class="sub">No teams joined this game.</div>'}
    <div class="hint">Click to close</div></div>`, true);
}
function watchRounds(){
  const g = S.game;
  if(lastClosed === null){ lastClosed = g.rounds_closed; if(g.status === 'finished') showFinal(); return; }
  if(g.rounds_closed > lastClosed){
    lastClosed = g.rounds_closed; bell();
    g.status === 'finished' ? showFinal() : showBell();
  } else if(g.rounds_closed < lastClosed){ lastClosed = g.rounds_closed; hideOv(); }
}

function bell(){
  if(!AC) return;
  [880, 1320, 1760].forEach((f,i) => {
    const o = AC.createOscillator(), gn = AC.createGain(), t = AC.currentTime + i*0.18;
    o.type = 'sine'; o.frequency.value = f; o.connect(gn); gn.connect(AC.destination);
    gn.gain.setValueAtTime(0.0001, t); gn.gain.exponentialRampToValueAtTime(0.25, t+0.02); gn.gain.exponentialRampToValueAtTime(0.0001, t+1.4);
    o.start(t); o.stop(t+1.5);
  });
}
addEventListener('pointerdown', () => {
  if(!AC){ try{ AC = new (window.AudioContext || window.webkitAudioContext)(); $('#snd').textContent = 'Bell sound is on. Press F for fullscreen.'; }catch(e){} }
});
addEventListener('keydown', e => {
  if(e.key === 'f' || e.key === 'F'){ document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen(); }
});

setInterval(poll, 1000); setInterval(tick, 250); poll();
"""

DISPLAY_HTML = page("Fest Street Exchange", DISPLAY_CSS, DISPLAY_BODY, DISPLAY_JS)

# ---------------------------------------------------------------------
#  PLAYER PAGE  ( /play )  - made for phones
# ---------------------------------------------------------------------
PLAY_CSS = r"""
.wrap{max-width:560px;margin:0 auto;padding:0 14px 60px}
.joinbox{max-width:420px;margin:0 auto;padding:12vh 18px 30px}
.joinbox h1{font-size:2rem;line-height:1.1;font-weight:800;margin-bottom:8px}
.joinbox p{color:var(--mute);line-height:1.45;margin-bottom:18px}
.joinbox input{font-size:1.15rem;margin-bottom:12px}
.joinbox button{width:100%;font-size:1.1rem;padding:14px}
.joinbox small{display:block;margin-top:12px;color:var(--mute);line-height:1.4}

.hd{position:sticky;top:0;z-index:20;background:var(--wall);padding:12px 0 10px;border-bottom:1px solid var(--line)}
.hdtop{display:flex;justify-content:space-between;align-items:center;gap:12px}
.tm{font-weight:800;font-size:1.3rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--mute);font-size:.9rem;margin-top:2px}
.clk{text-align:right}
.clk .flaps{font-size:26px}
.clk.urgent .fl{color:var(--down)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.stat{background:var(--wall2);border:1px solid var(--line);border-radius:var(--r);padding:9px 10px;min-width:0}
.stat small{display:block;color:var(--mute);font-size:.78rem;margin-bottom:2px}
.stat b{font-size:1.02rem;font-weight:800;white-space:nowrap}
.banner{background:var(--flap2);border-left:4px solid var(--amber);border-radius:var(--r);padding:10px 12px;margin-bottom:10px;line-height:1.35}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{flex:1;background:transparent;border:1px solid var(--line);color:var(--cream)}
.tabs button.on{background:var(--cream);color:#10282b;border-color:var(--cream)}

.prow{display:grid;grid-template-columns:minmax(0,1fr) 58px auto 66px;align-items:center;gap:10px;background:var(--wall2);border:1px solid var(--line);border-left:5px solid var(--mute);border-radius:var(--r);padding:9px 10px;margin-bottom:7px}
.prow.up{border-left-color:var(--up)} .prow.down{border-left-color:var(--down)}
.prow .tk{font-size:1.25rem;font-weight:800;line-height:1.05}
.prow .nm{font-size:.82rem;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prow .px{text-align:right;font-weight:800;font-size:1.08rem}
.prow .ch{text-align:right;font-size:.82rem;font-weight:700}
.prow button{padding:10px 0}
.hrow{display:flex;justify-content:space-between;gap:12px;background:var(--wall2);border:1px solid var(--line);border-radius:var(--r);padding:10px 12px;margin-bottom:7px}
.hrow .tk{font-size:1.2rem;font-weight:800}
.hrow .r{text-align:right;font-weight:800}
.hrow .r div{font-size:.85rem;font-weight:700}
.hh{font-weight:800;margin:16px 0 8px}
.trd{color:var(--mute);font-size:.9rem;padding:6px 0;border-bottom:1px solid var(--line)}
.lrow{display:grid;grid-template-columns:34px 1fr auto;gap:10px;padding:11px 12px;border-bottom:1px solid var(--line);align-items:baseline}
.lrow.me{background:var(--flap2);border-radius:var(--r);border-bottom-color:transparent}
.lrow .rk{font-weight:800;color:var(--mute)} .lrow.top3 .rk{color:var(--amber)}
.lrow .tn{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .lrow .tv{font-weight:800}
.empty{color:var(--mute);line-height:1.45;padding:14px 4px}

.sheet{position:fixed;inset:0;background:rgba(6,20,22,.75);z-index:40;display:flex;align-items:flex-end}
.sheet-in{background:var(--flap);width:100%;max-width:560px;margin:0 auto;border-radius:var(--r) var(--r) 0 0;padding:18px 16px calc(22px + env(safe-area-inset-bottom))}
.shtop{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:4px}
.shtop .tk{font-size:1.5rem;font-weight:800} .shtop .px{font-size:1.5rem;font-weight:800}
.shsub{color:var(--mute);margin-bottom:14px;line-height:1.4}
.stepper{display:grid;grid-template-columns:54px 1fr 54px;gap:8px;margin-bottom:8px}
.stepper input{text-align:center;font-size:1.4rem;font-weight:800}
.stepper button{font-size:1.5rem;padding:0}
.quick{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:14px}
.quick button{padding:9px 0;font-size:.95rem}
.sum{display:flex;justify-content:space-between;padding:6px 0;font-size:1.05rem} .sum b{font-weight:800}
.shact{display:grid;grid-template-columns:1fr 2fr;gap:8px;margin-top:12px}
.shact button{padding:14px;font-size:1.05rem}
"""

PLAY_BODY = r"""
<div id="join" class="joinbox hidden">
  <h1>Join the market</h1>
  <p>Pick a team name. Each round you can buy shares or sit it out. The team with the most cash and shares at the final bell wins.</p>
  <input id="teamname" maxlength="24" placeholder="Team name" autocomplete="off">
  <button class="go" id="joinbtn">Join the game</button>
  <small>If your team name already exists, you will be logged back in as that team.</small>
</div>

<div id="game" class="wrap hidden">
  <div class="hd">
    <div class="hdtop">
      <div style="min-width:0"><div class="tm" id="tm"></div><div class="sub"><span id="rd"></span> &nbsp;<span id="rank"></span></div></div>
      <div class="clk" id="clk"><div class="flaps"><span class="fl" id="p0">0</span><span class="fl" id="p1">5</span><span class="sep">:</span><span class="fl" id="p2">0</span><span class="fl" id="p3">0</span></div></div>
    </div>
  </div>
  <div class="stats" id="stats"></div>
  <div id="banner"></div>
  <div class="tabs" id="tabs">
    <button data-tab="market" class="on">Market</button>
    <button data-tab="portfolio">Portfolio</button>
    <button data-tab="board">Leaderboard</button>
  </div>
  <div id="view"></div>
</div>
<div id="sheet" class="sheet hidden"></div>
"""

PLAY_JS = r"""
let team = localStorage.getItem('fest_team') || '', S = null, fetchedAt = 0, tab = 'market', sel = null, qty = 1, lastClosed = null;

async function poll(){
  try{
    const r = await fetch('/api/state' + (team ? '?team=' + encodeURIComponent(team) : ''), {cache:'no-store'});
    S = await r.json(); fetchedAt = performance.now(); CUR = S.meta.currency;
    if(team && !S.me){ team = ''; localStorage.removeItem('fest_team'); }
    ui();
  }catch(e){}
}

async function joinGame(){
  const name = $('#teamname').value.trim();
  if(name.length < 2){ toast('Team name needs at least 2 characters', true); return; }
  const res = await post('/api/join', {name});
  if(!res.ok){ toast(res.error || 'Could not join', true); return; }
  team = res.name; localStorage.setItem('fest_team', team); lastClosed = null; poll();
}
$('#joinbtn').addEventListener('click', joinGame);
$('#teamname').addEventListener('keydown', e => { if(e.key === 'Enter') joinGame(); });

function marketView(){
  const open = S.game.status === 'running';
  return S.stocks.map(s => {
    const c = cls(s.change_pct), trend = cls(s.price - s.history[0]);
    return `<div class="prow ${c}">
      <div style="min-width:0"><div class="tk">${s.ticker}</div><div class="nm">${esc(s.name)}</div></div>
      <div class="${trend}">${spark(s.history)}</div>
      <div><div class="px">${money(s.price)}</div><div class="ch ${c}">${arrow(s.change_pct)} ${Math.abs(s.change_pct).toFixed(2)}%</div></div>
      <button class="go" ${open ? '' : 'disabled'} onclick="openSheet('${s.ticker}')">Buy</button></div>`;
  }).join('');
}
function portfolioView(){
  const me = S.me;
  const hs = me.holdings.length ? me.holdings.map(h => `<div class="hrow">
      <div><div class="tk">${h.ticker}</div><div class="nm" style="color:var(--mute);font-size:.85rem">${h.shares} shares, average ${money(h.avg_cost)}</div></div>
      <div class="r">${money(h.value)}<div class="${cls(h.pl)}">${h.pl < 0 ? '-' : '+'}${money(Math.abs(h.pl))} (${pct(h.pl_pct)})</div></div></div>`).join('')
    : '<div class="empty">You do not hold any shares yet. Open the Market tab and buy one.</div>';
  const tr = me.trades.length ? '<div class="hh">Your buys</div>' + me.trades.map(t => `<div class="trd">Round ${t.round}: ${t.shares} x ${t.ticker} at ${money(t.price)} for ${money(t.total)}</div>`).join('') : '';
  return hs + tr;
}
function boardView(){
  const me = S.me;
  if(!S.leaderboard.length) return '<div class="empty">No teams yet.</div>';
  let html = S.leaderboard.map((t,i) => `<div class="lrow ${i<3?'top3':''} ${t.name===me.name?'me':''}"><span class="rk">${t.rank}</span><span class="tn">${esc(t.name)}</span><span class="tv">${money(t.value,0)}</span></div>`).join('');
  if(me.rank > S.leaderboard.length) html += `<div class="lrow me"><span class="rk">${me.rank}</span><span class="tn">${esc(me.name)}</span><span class="tv">${money(me.value,0)}</span></div>`;
  return html;
}

function ui(){
  const inGame = !!team && S && S.me;
  $('#join').classList.toggle('hidden', !!inGame);
  $('#game').classList.toggle('hidden', !inGame);
  if(!inGame) return;
  const g = S.game, me = S.me;
  if(lastClosed !== null && g.rounds_closed !== lastClosed){
    closeSheet();
    toast(g.status === 'finished' ? 'The final bell has rung. Check the leaderboard.' : 'Round closed. Prices have updated.');
  }
  lastClosed = g.rounds_closed;
  $('#tm').textContent = me.name;
  $('#rd').textContent = g.status === 'finished' ? 'Game over' : 'Round ' + g.current_round + ' of ' + g.num_rounds;
  $('#rank').textContent = 'Rank ' + me.rank + ' of ' + S.team_count;
  setHTML($('#stats'), `<div class="stat"><small>Cash</small><b>${money(me.cash,0)}</b></div>
    <div class="stat"><small>Shares</small><b>${money(me.value - me.cash,0)}</b></div>
    <div class="stat"><small>Total</small><b>${money(me.value,0)}</b></div>`);
  const banner = {
    waiting: 'Trading opens when the organiser starts the game. Have a look at the stocks while you wait.',
    paused: 'Trading is halted. You can buy again when the organiser resumes.',
    finished: 'The game is over. You finished ' + me.rank + ' of ' + S.team_count + ' with ' + money(me.value,0) + '.',
    running: ''
  }[g.status];
  setHTML($('#banner'), banner ? `<div class="banner">${esc(banner)}</div>` : '');
  setHTML($('#view'), tab === 'market' ? marketView() : tab === 'portfolio' ? portfolioView() : boardView());
}

$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]'); if(!b) return;
  tab = b.dataset.tab;
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  if(S) ui();
});

/* ---- buy sheet ---- */
const stockOf = t => S.stocks.find(x => x.ticker === t);
const maxQty = () => Math.floor(S.me.cash / stockOf(sel).price + 1e-9);
function openSheet(t){ if(S.game.status !== 'running') return; sel = t; qty = 1; drawSheet(); $('#sheet').classList.remove('hidden'); }
function closeSheet(){ $('#sheet').classList.add('hidden'); sel = null; }
function drawSheet(){
  const s = stockOf(sel); if(!s) return;
  $('#sheet').innerHTML = `<div class="sheet-in">
    <div class="shtop"><span class="tk">${s.ticker}</span><span class="px">${money(s.price)}</span></div>
    <div class="shsub">${esc(s.name)}. You have ${money(S.me.cash)} in cash, enough for up to ${num(maxQty(),0)} shares.</div>
    <div class="stepper"><button onclick="bump(-1)" aria-label="One less">-</button><input id="qty" type="number" inputmode="numeric" min="1" step="1" value="${qty}" oninput="setQty(this.value)"><button onclick="bump(1)" aria-label="One more">+</button></div>
    <div class="quick"><button onclick="quick(1)">1</button><button onclick="quick(5)">5</button><button onclick="quick(10)">10</button><button onclick="quickPct(.5)">Half</button><button onclick="quickPct(1)">Max</button></div>
    <div class="sum"><span>Total cost</span><b id="cost"></b></div>
    <div class="sum"><span>Cash left</span><b id="left"></b></div>
    <div class="shact"><button onclick="closeSheet()">Cancel</button><button id="go" class="go" onclick="confirmBuy()">Confirm buy</button></div></div>`;
  updCost();
}
function setQty(v){ qty = Math.max(0, Math.floor(Number(v) || 0)); updCost(); }
function bump(d){ qty = Math.max(1, qty + d); $('#qty').value = qty; updCost(); }
function quick(n){ qty = n; $('#qty').value = qty; updCost(); }
function quickPct(f){ qty = Math.max(0, Math.floor(maxQty() * f)); $('#qty').value = qty; updCost(); }
function updCost(){
  const s = stockOf(sel); if(!s) return;
  const cost = qty * s.price, left = S.me.cash - cost;
  $('#cost').textContent = money(cost);
  const el = $('#left'); el.textContent = money(left); el.className = left < -0.005 ? 'down' : '';
  $('#go').disabled = !(qty >= 1 && left >= -0.005);
}
async function confirmBuy(){
  const btn = $('#go'); btn.disabled = true;
  const res = await post('/api/buy', {name: team, ticker: sel, shares: qty, round: S.game.current_round});
  if(res.ok){ toast(res.message); closeSheet(); }
  else { toast(res.error || 'Could not complete the buy', true); btn.disabled = false; }
  poll();
}
$('#sheet').addEventListener('click', e => { if(e.target.id === 'sheet') closeSheet(); });

function tick(){
  if(!S) return;
  const g = S.game; let left = g.seconds_left;
  if(g.status === 'running') left -= (performance.now() - fetchedAt)/1000;
  left = Math.max(0, left);
  const t = mmss(left);
  setTile('p0', t[0]); setTile('p1', t[1]); setTile('p2', t[3]); setTile('p3', t[4]);
  $('#clk').classList.toggle('urgent', g.status === 'running' && left <= 30);
}
setInterval(poll, 2000); setInterval(tick, 250); poll();
"""

PLAY_HTML = page("Play - The Quant Gambit Exchange", PLAY_CSS, PLAY_BODY, PLAY_JS)

# ---------------------------------------------------------------------
#  ORGANISER PAGE  ( /admin )
# ---------------------------------------------------------------------
ADMIN_CSS = r"""
.wrap{max-width:980px;margin:0 auto;padding:22px 16px 60px;display:grid;gap:16px}
.wrap h1{font-size:1.9rem;font-weight:800}
.login{max-width:380px;margin:16vh auto 0;padding:0 16px}
.login h1{font-size:1.8rem;font-weight:800;margin-bottom:12px}
.login input{margin-bottom:10px} .login button{width:100%}
.err{color:var(--down);margin-top:10px;min-height:1.2em}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.fact small{display:block;color:var(--mute);margin-bottom:2px} .fact b{font-size:1.35rem;font-weight:800}
.btns{display:flex;flex-wrap:wrap;gap:10px}
.form{display:grid;grid-template-columns:110px 1fr 150px auto;gap:10px;align-items:end}
.form label{display:block;color:var(--mute);font-size:.85rem;margin-bottom:4px}
.note{color:var(--mute);margin-top:10px;line-height:1.4}
table{width:100%;border-collapse:collapse}
th{font-weight:700;color:var(--mute);text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-size:.9rem}
td{padding:8px;border-bottom:1px solid var(--line)}
td.r,th.r{text-align:right}
tr.played td{color:var(--mute)}
button.small{padding:6px 10px;font-size:.9rem}
@media (max-width:720px){.form{grid-template-columns:1fr 1fr}.form .full{grid-column:1/-1}}
"""

ADMIN_BODY = r"""
<div id="login" class="login hidden">
  <h1>Organiser desk</h1>
  <input id="pin" type="password" placeholder="Organiser PIN" autocomplete="off">
  <button class="warn" id="pinbtn">Open organiser desk</button>
  <div class="err" id="loginerr"></div>
</div>

<div id="app" class="wrap hidden">
  <h1>Organiser desk</h1>

  <div class="panel">
    <div class="facts" id="facts"></div>
  </div>

  <div class="panel">
    <h3>Game controls</h3>
    <div class="btns">
      <button class="go" id="b-start">Start game</button>
      <button class="warn" id="b-pause">Pause trading</button>
      <button class="go" id="b-resume">Resume trading</button>
      <button class="stop" id="b-next">Close this round now</button>
      <button id="b-extend">Add 1 minute</button>
      <button id="b-reset">Reset game</button>
    </div>
    <div class="note">Prices move when a round closes, either when the timer hits zero or when you press Close this round now. Resetting removes all teams.</div>
  </div>

  <div class="panel">
    <h3>Change a stock's volatility for one round</h3>
    <div class="form">
      <div><label for="o-round">Round</label><input id="o-round" type="number" min="1" step="1" value="1"></div>
      <div><label for="o-stock">Stock</label><select id="o-stock"></select></div>
      <div><label for="o-vol">Volatility (%)</label><input id="o-vol" type="number" min="0.5" max="100" step="0.5" value="15"></div>
      <div><button class="warn" id="o-add">Set volatility</button></div>
    </div>
    <div class="note" id="o-help"></div>
    <table style="margin-top:12px"><thead><tr><th>Round</th><th>Stock</th><th class="r">Volatility</th><th>Added from</th><th></th></tr></thead><tbody id="o-list"></tbody></table>
    <div class="note">The override applies to the price move at the end of that round. Players cannot see this list.</div>
  </div>

  <div class="panel">
    <h3>Stocks</h3>
    <table><thead><tr><th>Ticker</th><th>Name</th><th class="r">Price</th><th class="r">Normal volatility</th><th class="r">Drift per round</th></tr></thead><tbody id="stocks"></tbody></table>
  </div>
</div>
"""

ADMIN_JS = r"""
let pin = sessionStorage.getItem('fest_pin') || '', S = null, fetchedAt = 0, formReady = false;
const H = () => ({'X-Admin-Pin': pin});

function showLogin(msg){ $('#login').classList.remove('hidden'); $('#app').classList.add('hidden'); $('#loginerr').textContent = msg || ''; }

async function poll(){
  if(!pin){ showLogin(); return; }
  try{
    const r = await fetch('/api/admin/state', {headers:H(), cache:'no-store'});
    const d = await r.json();
    if(d.ok === false){ pin = ''; sessionStorage.removeItem('fest_pin'); showLogin(d.error); return; }
    S = d; fetchedAt = performance.now(); CUR = S.meta.currency;
    $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
    render();
  }catch(e){}
}

async function act(action, body){
  const res = await post('/api/admin/' + action, body || {}, H());
  toast(res.ok ? res.message : res.error, !res.ok);
  poll();
}

function helpText(){
  if(!S) return;
  const tk = $('#o-stock').value, info = S.stock_info.find(x => x.ticker === tk);
  if(info) $('#o-help').textContent = 'Normal volatility for ' + info.name + ' is ' + (info.base_volatility*100).toFixed(1) + '% per round. Try 15% to 30% for a dramatic swing.';
}

function render(){
  const g = S.game, m = S.meta;
  if(!formReady){
    $('#o-stock').innerHTML = S.stock_info.map(x => `<option value="${x.ticker}">${esc(x.name)} (${x.ticker})</option>`).join('');
    $('#o-stock').addEventListener('change', helpText);
    formReady = true; helpText();
  }
  $('#o-round').min = Math.min(g.num_rounds, g.rounds_closed + 1); $('#o-round').max = g.num_rounds;
  const label = {waiting:'Not started', running:'Trading open', paused:'Trading halted', finished:'Game over'}[g.status];
  setHTML($('#facts'), `<div class="fact"><small>Status</small><b>${label}</b></div>
    <div class="fact"><small>Round</small><b>${g.status === 'finished' ? g.num_rounds : g.current_round} of ${g.num_rounds}</b></div>
    <div class="fact"><small>Time left</small><b id="left">--:--</b></div>
    <div class="fact"><small>Teams</small><b>${S.team_count}</b></div>
    <div class="fact"><small>${esc(m.index_name)}</small><b>${num(S.index.value)}</b></div>`);
  $('#b-start').disabled = g.status !== 'waiting';
  $('#b-pause').disabled = g.status !== 'running';
  $('#b-resume').disabled = g.status !== 'paused';
  $('#b-next').disabled = !(g.status === 'running' || g.status === 'paused');
  $('#b-extend').disabled = g.status === 'finished';
  setHTML($('#o-list'), S.overrides.length ? S.overrides.map(o => {
    const info = S.stock_info.find(x => x.ticker === o.stock), played = o.round <= g.rounds_closed;
    return `<tr class="${played ? 'played' : ''}"><td>${o.round}</td><td>${esc(info ? info.name : o.stock)}</td><td class="r">${(o.volatility*100).toFixed(1)}%</td><td>${o.source === 'settings' ? 'Settings in the code' : 'Organiser desk'}</td>
      <td class="r">${played ? 'Played' : `<button class="small stop" onclick="delOv(${o.round},'${o.stock}')">Remove</button>`}</td></tr>`;
  }).join('') : '<tr><td colspan="5" style="color:var(--mute)">No overrides yet. Every stock uses its normal volatility.</td></tr>');
  setHTML($('#stocks'), S.stocks.map(s => {
    const info = S.stock_info.find(x => x.ticker === s.ticker);
    return `<tr><td><b>${s.ticker}</b></td><td>${esc(s.name)}</td><td class="r">${money(s.price)}</td><td class="r">${(info.base_volatility*100).toFixed(1)}%</td><td class="r">${(info.drift*100).toFixed(2)}%</td></tr>`;
  }).join(''));
}
function delOv(round, stock){ act('override_delete', {round, stock}); }

$('#b-start').onclick = () => act('start');
$('#b-pause').onclick = () => act('pause');
$('#b-resume').onclick = () => act('resume');
$('#b-next').onclick = () => { if(confirm('Close this round now? Prices will move immediately.')) act('next'); };
$('#b-extend').onclick = () => act('extend');
$('#b-reset').onclick = () => { if(confirm('Reset the whole game? This removes every team and starts again from the opening prices.')) act('reset'); };
$('#o-add').onclick = () => act('override_add', {round: $('#o-round').value, stock: $('#o-stock').value, volatility: Number($('#o-vol').value) / 100});
$('#pinbtn').onclick = () => { pin = $('#pin').value; sessionStorage.setItem('fest_pin', pin); poll(); };
$('#pin').addEventListener('keydown', e => { if(e.key === 'Enter') $('#pinbtn').click(); });

function tick(){
  if(!S) return;
  const g = S.game; let left = g.seconds_left;
  if(g.status === 'running') left -= (performance.now() - fetchedAt)/1000;
  const el = $('#left'); if(el) el.textContent = g.status === 'finished' ? '00:00' : mmss(Math.max(0, left));
}
setInterval(poll, 2000); setInterval(tick, 250); poll();
"""

ADMIN_HTML = page("Organiser desk - The Quant Gambit Exchange", ADMIN_CSS, ADMIN_BODY, ADMIN_JS)


# =====================================================================
#  4. THE WEB SERVER
# =====================================================================
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # phones disconnect all the time at a busy fest - ignore that noise
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _pin_ok(self):
        given = self.headers.get("X-Admin-Pin", "")
        return hmac.compare_digest(given.encode("utf-8"), ADMIN_PIN.encode("utf-8"))

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        html = "text/html; charset=utf-8"
        if url.path == "/":
            return self._send(200, DISPLAY_HTML, html)
        if url.path == "/play":
            return self._send(200, PLAY_HTML, html)
        if url.path == "/admin":
            return self._send(200, ADMIN_HTML, html)
        if url.path == "/api/state":
            return self._send(200, market.public_state(q.get("team", [None])[0]))
        if url.path == "/api/admin/state":
            if not self._pin_ok():
                return self._send(403, fail("Wrong PIN"))
            return self._send(200, market.admin_state())
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, fail("Not found"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._send(400, fail("Bad request"))
        if path == "/api/join":
            return self._send(200, market.join(body.get("name", "")))
        if path == "/api/buy":
            try:
                rnd = int(body.get("round"))
            except (TypeError, ValueError):
                return self._send(200, fail("Bad request"))
            return self._send(200, market.buy(body.get("name", ""), body.get("ticker", ""), body.get("shares"), rnd))
        if path.startswith("/api/admin/"):
            if not self._pin_ok():
                return self._send(403, fail("Wrong PIN"))
            return self._send(200, market.admin_action(path.rsplit("/", 1)[1], body))
        self._send(404, fail("Not found"))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def main():
    global market
    ap = argparse.ArgumentParser(description=MARKET_NAME)
    ap.add_argument("--resume", action="store_true", help="continue the game saved in market_data.json")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()

    if RANDOM_SEED is not None:
        random.seed(RANDOM_SEED)
    market = Market(resume=args.resume)
    market.join_url = f"http://{lan_ip()}:{args.port}/play"
    server = Server(("0.0.0.0", args.port), Handler)

    print()
    print(f"  {MARKET_NAME} is open")
    print("  " + "-" * 58)
    print(f"  Big screen (projector):  http://localhost:{args.port}/")
    print(f"  Organiser desk:          http://localhost:{args.port}/admin     PIN: {ADMIN_PIN}")
    print(f"  Players (same wifi):     {market.join_url}")
    print(f"  All stock data saved to: {os.path.abspath(DATA_FILE)}")
    print("  Press Ctrl+C to stop.")
    print()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        market.save()
        print("\n  Saved. Bye!")


if __name__ == "__main__":
    main()
