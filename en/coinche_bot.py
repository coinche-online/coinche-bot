#!/usr/bin/env python3
"""A minimal coinche bot for coinche.online — a starting point.

    pip install websockets
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py           # waits to be seated
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py --host    # creates its table, game after game
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py --join    # joins other bots' tables

Create your bot and its token on https://coinche.online/bot-api ("Your bots").
Without options, the bot waits: seat it from the waiting room of one of your
Casual tables (a free seat → "Your bots"; a ranked game takes no bots). With
--host, it creates a table itself, fills it with site bots and starts it; with
--join, it takes a free seat at another bot's table (or yours).

The strategy lives in choose_bid, choose_card and choose_call: replace them with
your own. Everything they need is in the `your_turn` message and its "view" of
the deal. Full reference: https://coinche.online/bot-api

The server is wss://coinche.online; COINCHE_SERVER points to another one (for
example COINCHE_SERVER=ws://localhost:8000).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

import websockets

SERVER = os.environ.get("COINCHE_SERVER", "wss://coinche.online").rstrip("/")
API = SERVER.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
TOKEN = os.environ.get("COINCHE_BOT_TOKEN", "")
PING_S = 25.0            # with no frame for ~70 s, the server drops the connection

log = logging.getLogger("coinche_bot")

# ── The cards ────────────────────────────────────────────────────────────────
# A card is an integer 0..31: suit * 8 + rank.
SUITS = ["SPADES", "CLUBS", "HEARTS", "DIAMONDS"]
RANKS = ["7", "8", "9", "10", "J", "Q", "K", "A"]
# Strength order (weakest to strongest) and points, by rank.
PLAIN_ORDER = {"7": 0, "8": 1, "9": 2, "J": 3, "Q": 4, "K": 5, "10": 6, "A": 7}
TRUMP_ORDER = {"7": 0, "8": 1, "Q": 2, "K": 3, "10": 4, "A": 5, "9": 6, "J": 7}
PLAIN_POINTS = {"7": 0, "8": 0, "9": 0, "J": 2, "Q": 3, "K": 4, "10": 10, "A": 11}
TRUMP_POINTS = {"7": 0, "8": 0, "Q": 3, "K": 4, "10": 10, "A": 11, "9": 14, "J": 20}


def suit(card: int) -> str:
    return SUITS[card // 8]


def rank(card: int) -> str:
    return RANKS[card % 8]


def is_trump(card: int, trump: str | None) -> bool:
    """`trump`: a suit, "NT" (no trump) or "AT" (all trump)."""
    return trump == "AT" or suit(card) == trump


def strength(card: int, trump: str | None) -> int:
    order = TRUMP_ORDER if is_trump(card, trump) else PLAIN_ORDER
    return order[rank(card)] + (8 if is_trump(card, trump) else 0)


def points(card: int, trump: str | None) -> int:
    return (TRUMP_POINTS if is_trump(card, trump) else PLAIN_POINTS)[rank(card)]


def beats(card: int, best: int, trump: str | None) -> bool:
    """Does `card` take the trick from `best`, the card winning it so far? That
    one is a trump or of the suit led: only a trump (against a card that isn't
    one) or a stronger card of its suit beats it."""
    if trump in SUITS and (suit(card) == trump) != (suit(best) == trump):
        return suit(card) == trump
    return suit(card) == suit(best) and strength(card, trump) > strength(best, trump)


def partner(seat: int) -> int:
    return (seat + 2) % 4


# ── The strategy ─────────────────────────────────────────────────────────────

def choose_bid(prompt: dict) -> dict:
    """A cautious bid: 80 or 90 in the suit where we hold the jack, with at
    least four trumps. Never a coinche."""
    view, legal = prompt["view"], prompt["legal_actions"]
    me = prompt["seat"]
    contract = view["contract"]
    if contract is not None and contract["seat"] == partner(me):
        return {"kind": "Pass"}                    # don't outbid your partner
    best, best_value = None, 0
    for s in SUITS:
        mine = [c for c in view["hand"] if suit(c) == s]
        ranks = {rank(c) for c in mine}
        if len(mine) >= 4 and "J" in ranks:
            value = 90 if "9" in ranks else 80
            if value > best_value:
                best, best_value = s, value
    for action in legal:                           # the lowest legal bid
        if (action["kind"] == "Value" and action["trump"] == best
                and action["value"] <= best_value):
            return action
    return {"kind": "Pass"}


def choose_card(prompt: dict) -> int:
    """Take the trick as cheaply as possible, give points to a partner who is
    winning it, else throw away the card worth least."""
    view, legal = prompt["view"], prompt["legal_card_ids"]
    me = prompt["seat"]
    trump = view["contract"]["trump"]
    trick = view["trick"]
    if not trick:                                  # I lead: an ace first
        aces = [c for c in legal if rank(c) == "A" and not is_trump(c, trump)]
        return aces[0] if aces else min(legal, key=lambda c: strength(c, trump))
    best = next(p["card"] for p in trick if p["seat"] == view["winning"])
    if view["winning"] == partner(me):             # my partner is winning the trick
        return max(legal, key=lambda c: (points(c, trump) - 20 * is_trump(c, trump)))
    winners = [c for c in legal if beats(c, best, trump)]
    if winners:
        return min(winners, key=lambda c: strength(c, trump))
    return min(legal, key=lambda c: (points(c, trump), strength(c, trump)))


CALL_POINTS = {"THREE_SUIT": 20, "FOUR_SUIT": 50, "FIVE_SUIT": 100}


def choose_call(prompt: dict) -> dict:
    """Declare the combination worth the most. Four 7s or four 8s are worth
    nothing and cancel every announcement (house rule): never."""
    def value(combo: list[dict]) -> int:
        total = 0
        for call in combo:
            if call["type"] == "FOUR_OF_A_KIND":
                r = rank(call["cards"][0])
                total += {"J": 200, "9": 150}.get(r, 0 if r in ("7", "8") else 100)
            else:
                total += CALL_POINTS[call["type"]]
        return total
    combos = prompt["possible_calls"]
    best = max(range(len(combos)), key=lambda i: value(combos[i]))
    return {"index": best} if value(combos[best]) > 0 else {"skip": True}


def answer(prompt: dict) -> dict:
    """The answer to a `your_turn` addressed to me."""
    phase = prompt["phase"]
    if phase == "bid":
        return {"type": "bid", "action": choose_bid(prompt)}
    if phase == "card":
        return {"type": "card", "card_id": choose_card(prompt)}
    return {"type": "call", **choose_call(prompt)}


# ── The REST routes (/api/bot/…) ─────────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"{status} {detail}")
        self.status, self.detail = status, detail


def _http(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail")
        except ValueError:
            detail = None
        raise ApiError(e.code, detail) from None


async def api(method: str, path: str, body: dict | None = None):
    return await asyncio.to_thread(_http, method, path, body)


# ── The sockets ──────────────────────────────────────────────────────────────

# The headers argument was renamed in websockets 14.
_HEADERS_KW = "additional_headers" if int(websockets.__version__.split(".")[0]) >= 14 else "extra_headers"


def connect(path: str):
    return websockets.connect(SERVER + path, **{_HEADERS_KW: {"Authorization": f"Bearer {TOKEN}"}})


async def keepalive(ws) -> None:
    try:
        while True:
            await asyncio.sleep(PING_S)
            await ws.send(json.dumps({"type": "ping"}))
    except websockets.ConnectionClosed:
        pass


def close_code(e: websockets.ConnectionClosed) -> int | None:
    rcvd = getattr(e, "rcvd", None)                 # websockets ≥ 10
    return rcvd.code if rcvd is not None else getattr(e, "code", None)


# The tables I joined (--join): I didn't come to host them.
JOINED: set[str] = set()


async def play_table_once(table_id: str) -> bool:
    """One connection to the table. True when there's nothing left to do there
    (game over, table closed, no seat any more), False if the connection closed first."""
    async with connect(f"/ws/tables/{table_id}") as ws:
        pinger = asyncio.create_task(keepalive(ws))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "your_turn":
                    # No need to pause: the server plays the move at the
                    # site bots' pace.
                    await ws.send(json.dumps(answer(msg)))
                elif kind == "table":
                    t = msg["table"]
                    log.info("table %s: %s, seat %s", table_id, t["status"], t["your_seat"])
                    if t["status"] == "waiting" and t["your_seat"] is None:
                        return True                  # no seat here any more
                    if (t["status"] == "waiting" and table_id in JOINED
                            and t["seats"][t["your_seat"]]["host"]):
                        # The host left and the room falls to me: I can't run it,
                        # so I leave it (it passes to someone else, or closes).
                        await api("POST", f"/api/bot/tables/{table_id}/leave")
                        return True
                elif kind == "deal_end":
                    log.info("table %s: deal %d, %s", table_id, msg["deal_num"], msg["points"])
                elif kind == "error":
                    log.warning("table %s: refused (%s) %s", table_id, msg["code"], msg["detail"])
                elif kind in ("timeout", "seat_lost"):
                    log.warning("table %s: %s", table_id, msg)
                elif kind == "game_end":
                    log.info("table %s: game over, team %d wins, %s",
                             table_id, msg["winner_team"], msg["score"])
                    return True
                elif kind == "table_closed":
                    return True
        finally:
            pinger.cancel()
    return False


async def play_table(table_id: str) -> None:
    """Play at the table I was seated at until the end. A drop (network, a
    server restart, which resumes the game) is not the end: we go back to the
    table, where the seat waits 30 s before passing to a site bot."""
    for _ in range(20):
        try:
            if await play_table_once(table_id):
                return
        except websockets.ConnectionClosed as e:
            if close_code(e) == 4404:                # the table no longer exists
                log.info("table %s: gone", table_id)
                return
            log.warning("table %s: connection lost (%s), back in 3 s", table_id, e)
        except OSError as e:
            log.warning("table %s: server unreachable (%s), back in 3 s", table_id, e)
        await asyncio.sleep(3)


# ── Hosting, joining ────────────────────────────────────────────────────────

def busy(tables: dict[str, asyncio.Task]) -> bool:
    return any(not task.done() for task in tables.values())


async def host_loop(tables: dict[str, asyncio.Task], args) -> None:
    """--host: a table of my own, filled with site bots after --wait seconds
    (time for other bots, or people, to sit down), then started. A new one at
    the end of each game."""
    while True:
        if not busy(tables):
            try:
                table = await api("POST", "/api/bot/tables", {"score_limit": args.score})
                log.info("table %s created", table["id"])
                await asyncio.sleep(args.wait)
                table = await api("GET", f"/api/bot/tables/{table['id']}")
                for seat in table["seats"]:
                    if seat["kind"] == "empty":
                        await api("POST", f"/api/bot/tables/{table['id']}/seats/{seat['position']}",
                                  {"kind": "site_bot", "bot_kind": args.site_bot})
                await api("POST", f"/api/bot/tables/{table['id']}/start")
            except ApiError as e:
                log.warning("hosting: %s", e)
        await asyncio.sleep(5)


async def join_loop(tables: dict[str, asyncio.Task]) -> None:
    """--join: when I'm not playing anywhere, a free seat at a table open to me
    (another bot's, or my owner's)."""
    while True:
        if not busy(tables):
            try:
                for table in await api("GET", "/api/bot/tables"):
                    JOINED.add(table["id"])
                    await api("POST", f"/api/bot/tables/{table['id']}/join")
                    log.info("seated at table %s", table["id"])
                    break
            except ApiError as e:
                log.warning("joining: %s", e)
        await asyncio.sleep(10)


async def main(args) -> None:
    if not TOKEN:
        sys.exit("COINCHE_BOT_TOKEN missing: create your bot on https://coinche.online/bot-api.")
    try:
        me = await api("GET", "/api/bot/account")
    except ApiError as e:
        sys.exit(f"token refused ({e}): it may have been regenerated.")
    log.info("I am %s, %s's bot", me["name"], me["owner"])
    tables: dict[str, asyncio.Task] = {}
    # Kept here: asyncio only holds weak references to its tasks.
    loops = ([asyncio.create_task(host_loop(tables, args))] if args.host else []) + \
            ([asyncio.create_task(join_loop(tables))] if args.join else [])
    while True:
        try:
            # The event socket: where I have a seat. It gives my tables back on
            # each connection, after a drop or a server restart.
            async with connect("/ws/lobby") as events:
                pinger = asyncio.create_task(keepalive(events))
                try:
                    async for raw in events:
                        msg = json.loads(raw)
                        tid = msg.get("table_id")
                        if msg.get("type") == "seated" and (tid not in tables or tables[tid].done()):
                            tables[tid] = asyncio.create_task(play_table(tid))
                        elif msg.get("type") == "released" and tid in tables:
                            tables.pop(tid).cancel()
                finally:
                    pinger.cancel()
        except websockets.ConnectionClosed as e:
            if close_code(e) == 4401:
                sys.exit("token refused: it may have been regenerated.")
            log.warning("events lost (%s), reconnecting in 5 s", e)
        except OSError as e:
            log.warning("server unreachable (%s), retrying in 5 s", e)
        await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", action="store_true", help="create its own table, game after game")
    parser.add_argument("--join", action="store_true", help="join the tables open to bots")
    parser.add_argument("--wait", type=float, default=0, help="--host: seconds before filling the table")
    parser.add_argument("--site-bot", default="tacticien_2",
                        help="--host: the site bot that fills the table (tonton_roger, "
                             "tacticien_1 to tacticien_3, athena)")
    parser.add_argument("--score", type=int, default=1000, help="--host: the score to reach")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:                      # Ctrl-C: stop, without a traceback
        pass
