# Slate

Phone scoreboard for a table of people. No account. The owner link is the only key.

Not public yet. Local: http://127.0.0.1:8882

Create a slate, add rounds, fix or undo the last one, rename players, add a late player. The board link cannot edit. Table view is the one to prop up. It polls for new rounds.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8882
```

Health: `GET /health`

MIT. Built on holy-hermes.
