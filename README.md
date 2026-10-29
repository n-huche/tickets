# tickets

A flight price monitor for one specific trip: Recife (REC) to Rio de Janeiro (GIG or SDU) and back, with flexible dates inside fixed windows.

## How this was built

All of the code in this repository was written by an AI agent. None of it was written by hand.

My part was the problem framing and the architecture. The agent's first plan was to check prices itself, manually, twice a day. I rejected that. I asked for a deterministic system that runs on its own, applies every rule in code, and fires a webhook only when an offer satisfies all of them. That way the AI is only woken up on events, not on a schedule.

Once it was running, I noticed that even the AI in the middle is unnecessary. The rules are already enforced in code, so the system could send the notification directly, for example through a Slack incoming webhook, with no agent involved. Today a match still goes through an AI agent that double-checks the offer and posts it to Slack.

I chose to freeze the project as it is for now. It works and costs almost nothing to run, so removing that last layer isn't worth the time yet.

It was built to solve a real problem of mine: finding the cheapest flights for a trip whose dates are flexible but have to fall inside fixed windows. No price alert service I know of handles that combination of rules.

## What it does

Every 30 minutes, with some jitter, it sweeps prices for each outbound and return date separately and stores every observed fare in SQLite. It then combines outbound and return fares locally, which may be on different airlines, filters them through the rules, and calls a webhook when the round trip total is at or below the target. Each combination alerts once and only alerts again if its price drops further.

## Rules

Configured in `tickets_app/config.py`:

| Rule | Value |
|---|---|
| Route | REC to GIG/SDU, then GIG/SDU to REC |
| Flights | Nonstop only |
| Time of day | Depart at or after 04:00 and land at or before 23:00, both legs |
| Date windows (2027) | Jan 1 to 15, Jan 24 to Feb 5, Feb 11 to 28. The whole stay must fit in one window |
| Stay | 8 to 15 nights |
| Alert | Round trip total at or below R$ 1,100 |
| Freshness | Only fares seen in the last 6 hours are combined |

## Sources

- Google Flights, over plain HTTP. This is the main source.
- Kayak and Momondo, through headless Chrome. They also expose sellers such as online travel agencies and the airlines.
- Kiwi.com, 123milhas and MaxMilhas, through headless Chrome. The last two resell airline miles, and alerts flag that.
- Promo feeds from Melhores Destinos, Passagens Imperdíveis and Voopter. These are reference only.

A failing source is isolated from the others. When a source is blocked, it backs off for 30 to 120 minutes, doubling on each consecutive block up to 12 hours.

## Running it

Requires Python 3 and Google Chrome installed as `google-chrome`. Without Chrome, the browser sources are disabled and only Google Flights and the feeds run.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # set the webhook URL and auth
./tickets service start
```

CLI:

```bash
./tickets status          # service health, last sweep, best price, sources, errors
./tickets best            # best current combinations (--links for booking links)
./tickets sources         # status of each source
./tickets history         # price history (--ida / --volta to filter by date)
./tickets sweeps          # list of sweeps
./tickets logs -f         # follow the log (--errors, --json)
./tickets test-alert      # send a test webhook
./tickets service start|stop|restart|status|ensure
```

The service also serves a dashboard on `127.0.0.1:8787`, local only, with a `/health` endpoint. Use an SSH tunnel to view it from another machine. Logs are written to `logs/` as plain text and JSON lines, and the database lives in `data/tickets.db`.

The CLI output and the more detailed operations notes in `docs/operations.pt-BR.md` are in Portuguese.

## Limitations

- The Azul, GOL and LATAM websites block it, and so does Skyscanner. Airline fares only arrive indirectly, through Google Flights and the other search engines.
- Promo feeds are only shown for reference. A promotion found there never triggers an alert.
- Agency prices from Kiwi or Kayak sellers can be lower than what the airline shows and may change at checkout.
- There is no systemd or cron on the machine it runs on. A supervisor restarts the daemon if it dies, but after a reboot it only comes back if something calls `./tickets service ensure`, which in my setup is an external startup hook.
- Scrapers break when sites change. The project is frozen, so expect some sources to stop working over time.
