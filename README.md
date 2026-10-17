# edgar-pit

edgar-pit builds a research-grade, point-in-time fundamentals dataset from the SEC's free APIs. Every value is keyed by the moment its filing was accepted, every restatement is kept as a new version, and every 8-K item is an event with an exact timestamp — so `asof('RY', 'revenue', '2024-01-15')` returns what an investor could actually have known that day. It covers US GAAP and IFRS filers, including Canadian companies cross-listed in the US.

Architecture, the staging contract and the Epic 00 foundation notes live in [`docs/`](docs/).

## Developer setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --frozen
cp .env.example .env    # set SEC_USER_AGENT="Your Name your@email" (live SEC requests only)
uv run ruff check . && uv run ruff format --check .
uv run mypy edgarpit
uv run pytest -q
```
