# Tickets

A flight price monitor for a Recife (REC) to Rio de Janeiro (GIG/SDU) round trip with flexible dates inside fixed windows. It sweeps prices every 20 minutes on a fixed schedule (:00, :20, :40) and fires a webhook when a round trip meets every rule.

## Why / how it was built

- All of the code was written by an AI agent, not by hand.
- My part was the architecture. The agent first proposed checking prices itself, manually, twice a day. I rejected that and required a deterministic monitor that runs on its own and fires a webhook only on a match, so the AI is only woken on events.
- Then I realized even the AI in the middle is unnecessary. The rules are already enforced in code, so the monitor could notify directly, e.g. through a Slack incoming webhook.
- It's frozen as is for now: it works and costs almost nothing to run.
- It was built to solve a real problem of mine.

## Rules

- REC to GIG/SDU and back, nonstop only
- Depart at or after 04:00, land at or before 23:00
- 2027 windows: Jan 1–15, Jan 24–Feb 5, Feb 11–28; the whole stay inside one window
- Stay of 11 to 15 nights
- Alert when the round trip total is at or below R$ 1,100

Sources: Google Flights, Kayak, Momondo, Kiwi, 123milhas and MaxMilhas, plus promo feeds as reference only.

## Run

Requires Python 3 and Google Chrome (`google-chrome`) for the browser sources.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # webhook URL and auth
./tickets service start
./tickets status
./tickets best
```

Dashboard on `127.0.0.1:8787`. Details, all CLI commands and source status in [docs/operations.pt-BR.md](docs/operations.pt-BR.md).
