# coinche-bot

Un bot de coinche (belote contrée) prêt à jouer sur [coinche.online](https://coinche.online/bot-api), à prendre comme point de départ pour écrire le tien. *[English below.](#english)*

coinche.online ouvre une API aux programmes des joueurs : ton bot s'assoit à une table, annonce, joue ses cartes, et affronte les bots du site, d'autres bots ou des joueurs. À chaque tour, il reçoit sa main, les enchères, les plis déjà joués et la liste des coups permis : pas besoin de coder les règles.

## Démarrer

1. Crée ton bot sur [coinche.online/bot-api](https://coinche.online/bot-api) (il faut un compte, gratuit). Tu reçois son jeton secret, `cb_…`.
2. Lance ce bot (Python 3.10 ou plus) :

   ```sh
   pip install websockets
   export COINCHE_BOT_TOKEN=cb_...
   python coinche_bot.py --host
   ```

   Avec `--host`, il crée sa table, la complète de bots du site et joue, partie après partie. Sans option, il attend que tu l'installes à une de tes tables ; avec `--join`, il s'assoit aux tables des autres bots. `--site-bot athena` le fait jouer contre le bot le plus fort du site.
3. Remplace sa stratégie : `choose_bid` pour les enchères, `choose_card` pour les cartes, `choose_call` pour les annonces. Le reste — connexions, reprise après une coupure, hébergement des tables — est déjà écrit.

## La référence

Le déroulé d'une partie, chaque message et chaque champ : [coinche.online/bot-api](https://coinche.online/bot-api). Les mêmes en documents standard, à ouvrir dans Swagger Editor ou AsyncAPI Studio, ou à donner à un générateur de client : [OpenAPI](https://coinche.online/api/bot/openapi.json) pour les routes, [AsyncAPI](https://coinche.online/api/bot/asyncapi.json) pour les WebSockets.

Un bot joue en parties non classées, une à la fois, 50 au plus par 24 heures, au rythme des bots du site.

## Licence

MIT : fais-en ce que tu veux.

---

<a id="english"></a>
## English

A coinche (belote contrée) bot ready to play on [coinche.online](https://coinche.online/bot-api), to start from when writing your own.

coinche.online opens an API to players' programs: your bot sits at a table, bids, plays its cards, and takes on the site's bots, other bots or people. On each turn it gets its hand, the bids, the tricks played so far and the list of legal moves: no need to code the rules.

1. Create your bot on [coinche.online/bot-api](https://coinche.online/bot-api) (you need an account, free). You get its secret token, `cb_…`.
2. Run this bot (Python 3.10 or later): `pip install websockets`, `export COINCHE_BOT_TOKEN=cb_...`, `python coinche_bot.py --host`. With `--host` it creates its table, fills it with site bots and plays, game after game; without options it waits for you to seat it; with `--join` it sits at other bots' tables.
3. Replace its strategy: `choose_bid`, `choose_card`, `choose_call`. Everything else is already written.

Full reference: [coinche.online/bot-api](https://coinche.online/bot-api), also as [OpenAPI](https://coinche.online/api/bot/openapi.json) and [AsyncAPI](https://coinche.online/api/bot/asyncapi.json) documents. The code comments are in French.
