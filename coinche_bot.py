#!/usr/bin/env python3
"""Un bot de coinche minimal pour coinche.online — un point de départ.

    pip install websockets
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py           # attend qu'on l'assoie
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py --host    # crée sa table, partie après partie
    COINCHE_BOT_TOKEN=cb_... python coinche_bot.py --join    # rejoint les tables des autres bots

Crée ton bot et son jeton sur https://coinche.online/bot-api (« Tes bots »).
Sans option, le bot attend : installe-le depuis la salle d'attente d'une de tes
tables (un siège libre → « Tes bots »). Avec --host, il crée lui-même une table, la complète
de bots du site et la lance ; avec --join, il prend une place libre à la table
d'un autre bot (ou de toi).

La stratégie tient dans choose_bid, choose_card et choose_call : remplace-les
par la tienne. Tout ce dont elles ont besoin est dans le message `your_turn` et
sa « vue » de la donne. Référence complète : https://coinche.online/bot-api
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
PING_S = 25.0            # sans trame pendant ~70 s, le serveur coupe la connexion

log = logging.getLogger("coinche_bot")

# ── Les cartes ───────────────────────────────────────────────────────────────
# Une carte est un entier 0..31 : couleur * 8 + rang.
SUITS = ["SPADES", "CLUBS", "HEARTS", "DIAMONDS"]
RANKS = ["7", "8", "9", "10", "J", "Q", "K", "A"]
# Ordre de force (du plus faible au plus fort) et points, par rang.
PLAIN_ORDER = {"7": 0, "8": 1, "9": 2, "J": 3, "Q": 4, "K": 5, "10": 6, "A": 7}
TRUMP_ORDER = {"7": 0, "8": 1, "Q": 2, "K": 3, "10": 4, "A": 5, "9": 6, "J": 7}
PLAIN_POINTS = {"7": 0, "8": 0, "9": 0, "J": 2, "Q": 3, "K": 4, "10": 10, "A": 11}
TRUMP_POINTS = {"7": 0, "8": 0, "Q": 3, "K": 4, "10": 10, "A": 11, "9": 14, "J": 20}


def suit(card: int) -> str:
    return SUITS[card // 8]


def rank(card: int) -> str:
    return RANKS[card % 8]


def is_trump(card: int, trump: str | None) -> bool:
    """`trump` : une couleur, "NT" (sans atout) ou "AT" (tout atout)."""
    return trump == "AT" or suit(card) == trump


def strength(card: int, trump: str | None) -> int:
    order = TRUMP_ORDER if is_trump(card, trump) else PLAIN_ORDER
    return order[rank(card)] + (8 if is_trump(card, trump) else 0)


def points(card: int, trump: str | None) -> int:
    return (TRUMP_POINTS if is_trump(card, trump) else PLAIN_POINTS)[rank(card)]


def beats(card: int, best: int, trump: str | None) -> bool:
    """`card` prend-elle le pli à `best`, la carte maîtresse ? Celle-ci est un
    atout ou de la couleur demandée : seul un atout (contre une carte qui n'en
    est pas) ou une plus forte de sa couleur la bat."""
    if trump in SUITS and (suit(card) == trump) != (suit(best) == trump):
        return suit(card) == trump
    return suit(card) == suit(best) and strength(card, trump) > strength(best, trump)


def partner(seat: int) -> int:
    return (seat + 2) % 4


# ── La stratégie ─────────────────────────────────────────────────────────────

def choose_bid(prompt: dict) -> dict:
    """Une annonce prudente : 80 ou 90 dans la couleur où l'on tient le valet,
    avec quatre atouts au moins. Jamais de coinche."""
    view, legal = prompt["view"], prompt["legal_actions"]
    me = prompt["seat"]
    contract = view["contract"]
    if contract is not None and contract["seat"] == partner(me):
        return {"kind": "Pass"}                    # ne pas couvrir son partenaire
    best, best_value = None, 0
    for s in SUITS:
        mine = [c for c in view["hand"] if suit(c) == s]
        ranks = {rank(c) for c in mine}
        if len(mine) >= 4 and "J" in ranks:
            value = 90 if "9" in ranks else 80
            if value > best_value:
                best, best_value = s, value
    for action in legal:                           # la plus petite enchère permise
        if (action["kind"] == "Value" and action["trump"] == best
                and action["value"] <= best_value):
            return action
    return {"kind": "Pass"}


def choose_card(prompt: dict) -> int:
    """Prendre le pli au moins cher, charger son partenaire maître, sinon se
    défausser de la carte qui rapporte le moins."""
    view, legal = prompt["view"], prompt["legal_card_ids"]
    me = prompt["seat"]
    trump = view["contract"]["trump"]
    trick = view["trick"]
    if not trick:                                  # j'entame : un as d'abord
        aces = [c for c in legal if rank(c) == "A" and not is_trump(c, trump)]
        return aces[0] if aces else min(legal, key=lambda c: strength(c, trump))
    best = next(p["card"] for p in trick if p["seat"] == view["winning"])
    if view["winning"] == partner(me):             # le partenaire est maître
        return max(legal, key=lambda c: (points(c, trump) - 20 * is_trump(c, trump)))
    winners = [c for c in legal if beats(c, best, trump)]
    if winners:
        return min(winners, key=lambda c: strength(c, trump))
    return min(legal, key=lambda c: (points(c, trump), strength(c, trump)))


CALL_POINTS = {"THREE_SUIT": 20, "FOUR_SUIT": 50, "FIVE_SUIT": 100}


def choose_call(prompt: dict) -> dict:
    """Annoncer la combinaison qui rapporte le plus. Un carré de 7 ou de 8 ne
    rapporte rien et annule toutes les annonces (règle de la maison) : jamais."""
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
    """La réponse à un `your_turn` qui m'est adressé."""
    phase = prompt["phase"]
    if phase == "bid":
        return {"type": "bid", "action": choose_bid(prompt)}
    if phase == "card":
        return {"type": "card", "card_id": choose_card(prompt)}
    return {"type": "call", **choose_call(prompt)}


# ── Les routes REST (/api/bot/…) ─────────────────────────────────────────────

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


# ── Les sockets ──────────────────────────────────────────────────────────────

# L'argument des en-têtes a changé de nom avec websockets 14.
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


# Les tables que j'ai rejointes (--join) : je n'y suis pas venu pour héberger.
JOINED: set[str] = set()


async def play_table_once(table_id: str) -> bool:
    """Une connexion à la table. True quand il n'y a plus rien à y faire (partie
    finie, table fermée, plus de siège), False si la connexion s'est fermée avant."""
    async with connect(f"/ws/tables/{table_id}") as ws:
        pinger = asyncio.create_task(keepalive(ws))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "your_turn":
                    # Pas de pause à faire : le serveur fait passer le coup au
                    # rythme des bots du site.
                    await ws.send(json.dumps(answer(msg)))
                elif kind == "table":
                    t = msg["table"]
                    log.info("table %s : %s, siège %s", table_id, t["status"], t["your_seat"])
                    if t["status"] == "waiting" and t["your_seat"] is None:
                        return True                  # plus de siège ici
                    if (t["status"] == "waiting" and table_id in JOINED
                            and t["seats"][t["your_seat"]]["host"]):
                        # L'hôte est parti et la salle m'échoit : je ne sais pas la
                        # mener, je la laisse (elle passe à un autre, ou ferme).
                        await api("POST", f"/api/bot/tables/{table_id}/leave")
                        return True
                elif kind == "deal_end":
                    log.info("table %s : donne %d, %s", table_id, msg["deal_num"], msg["points"])
                elif kind == "error":
                    log.warning("table %s : refusé (%s) %s", table_id, msg["code"], msg["detail"])
                elif kind in ("timeout", "seat_lost"):
                    log.warning("table %s : %s", table_id, msg)
                elif kind == "game_end":
                    log.info("table %s : partie finie, équipe %d gagnante, %s",
                             table_id, msg["winner_team"], msg["score"])
                    return True
                elif kind == "table_closed":
                    return True
        finally:
            pinger.cancel()
    return False


async def play_table(table_id: str) -> None:
    """Jouer à la table où l'on m'a assis jusqu'à la fin. Une coupure (réseau,
    redémarrage du serveur, qui reprend la partie) n'est pas la fin : on revient
    à la table, où le siège attend 30 s avant de passer à un bot du site."""
    for _ in range(20):
        try:
            if await play_table_once(table_id):
                return
        except websockets.ConnectionClosed as e:
            if close_code(e) == 4404:                # la table n'existe plus
                log.info("table %s : disparue", table_id)
                return
            log.warning("table %s : connexion perdue (%s), retour dans 3 s", table_id, e)
        except OSError as e:
            log.warning("table %s : serveur injoignable (%s), retour dans 3 s", table_id, e)
        await asyncio.sleep(3)


# ── Héberger, rejoindre ──────────────────────────────────────────────────────

def busy(tables: dict[str, asyncio.Task]) -> bool:
    return any(not task.done() for task in tables.values())


async def host_loop(tables: dict[str, asyncio.Task], args) -> None:
    """--host : une table à moi, complétée de bots du site au bout de --wait
    secondes (le temps pour d'autres bots, ou des personnes, de s'asseoir), puis
    lancée. Une nouvelle à la fin de chaque partie."""
    while True:
        if not busy(tables):
            try:
                table = await api("POST", "/api/bot/tables", {"score_limit": args.score})
                log.info("table %s créée", table["id"])
                await asyncio.sleep(args.wait)
                table = await api("GET", f"/api/bot/tables/{table['id']}")
                for seat in table["seats"]:
                    if seat["kind"] == "empty":
                        await api("POST", f"/api/bot/tables/{table['id']}/seats/{seat['position']}",
                                  {"kind": "site_bot", "bot_kind": args.site_bot})
                await api("POST", f"/api/bot/tables/{table['id']}/start")
            except ApiError as e:
                log.warning("héberger : %s", e)
        await asyncio.sleep(5)


async def join_loop(tables: dict[str, asyncio.Task]) -> None:
    """--join : quand je ne joue nulle part, une place libre à une table qui
    m'est ouverte (celle d'un autre bot, ou de mon propriétaire)."""
    while True:
        if not busy(tables):
            try:
                for table in await api("GET", "/api/bot/tables"):
                    JOINED.add(table["id"])
                    await api("POST", f"/api/bot/tables/{table['id']}/join")
                    log.info("assis à la table %s", table["id"])
                    break
            except ApiError as e:
                log.warning("rejoindre : %s", e)
        await asyncio.sleep(10)


async def main(args) -> None:
    if not TOKEN:
        sys.exit("COINCHE_BOT_TOKEN manquant : crée ton bot sur https://coinche.online/bot-api.")
    try:
        me = await api("GET", "/api/bot/account")
    except ApiError as e:
        sys.exit(f"jeton refusé ({e}) : il a peut-être été régénéré.")
    log.info("je suis %s, bot de %s", me["name"], me["owner"])
    tables: dict[str, asyncio.Task] = {}
    # Gardées ici : asyncio ne retient ses tâches que faiblement.
    loops = ([asyncio.create_task(host_loop(tables, args))] if args.host else []) + \
            ([asyncio.create_task(join_loop(tables))] if args.join else [])
    while True:
        try:
            # La socket d'événements : où j'ai un siège. Elle redonne mes tables à
            # chaque connexion, après une coupure ou un redémarrage du serveur.
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
                sys.exit("jeton refusé : il a peut-être été régénéré.")
            log.warning("événements perdus (%s), reconnexion dans 5 s", e)
        except OSError as e:
            log.warning("serveur injoignable (%s), nouvel essai dans 5 s", e)
        await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", action="store_true", help="créer sa table, partie après partie")
    parser.add_argument("--join", action="store_true", help="rejoindre les tables ouvertes aux bots")
    parser.add_argument("--wait", type=float, default=0, help="--host : secondes avant de compléter la table")
    parser.add_argument("--site-bot", default="tacticien_2",
                        help="--host : le bot du site qui complète la table (tonton_roger, "
                             "tacticien_1 à tacticien_3, athena)")
    parser.add_argument("--score", type=int, default=1000, help="--host : le score à atteindre")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main(parser.parse_args()))
