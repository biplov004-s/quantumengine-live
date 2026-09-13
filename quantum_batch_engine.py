"""
Quantum Trading Signal Engine — Batching version
==================================================
EDUCATIONAL / RESEARCH PROTOTYPE. NOT FINANCIAL ADVICE.

Covers the FULL Nifty 500 universe by rotating through small batches, since no
current quantum computer (or simulator, practically) can hold ~500 decision
variables in one QAOA circuit. Each run processes one batch and advances a
saved cursor — call it from a scheduler (cron, GitHub Actions, etc.) every few
minutes in production, and it works through the whole index over time.

Files this reads/writes:
  nifty500_universe.json  — the 501-stock universe (symbol, name, industry)
  batch_state.json        — { cursor, cycle } — where we are in the rotation
  master_signals.json     — accumulated latest signal per stock, across cycles
  signals.json            — just the most recent batch (kept for compatibility
                             with the earlier single-batch dashboard)
"""

import json
import os
import argparse
from datetime import datetime, timezone, timedelta

import requests
import numpy as np


import numpy as np
from qiskit_optimization import QuadraticProgram
from qiskit_optimization.algorithms import MinimumEigenOptimizer
from qiskit_algorithms import QAOA, NumPyMinimumEigensolver
from qiskit_algorithms.optimizers import COBYLA
from qiskit.primitives import StatevectorSampler

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
UNIVERSE_FILE = "nifty500_universe.json"
STATE_FILE = "batch_state.json"
MASTER_FILE = "master_signals.json"
LATEST_BATCH_FILE = "signals.json"

BATCH_SIZE = 6            # qubits per QAOA run — kept small since state-vector
                           # simulation cost grows exponentially with qubit count
BUDGET_PER_BATCH = 2       # how many of each batch's stocks get selected
RISK_FACTOR = 0.5


def load_universe():
    with open(UNIVERSE_FILE) as f:
        return json.load(f)


def load_state(universe_len):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cursor": 0, "cycle": 1, "analyzed_this_cycle": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_master():
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE) as f:
            return json.load(f)
    return {}


def save_master(master):
    with open(MASTER_FILE, "w") as f:
        json.dump(master, f, indent=2)


def fetch_market_prices(symbols):
    """Fetch real daily NSE prices from Yahoo Finance.

    Despite the historical function name, this is now the live-data provider.
    NSE symbols are requested with the ``.NS`` suffix. A 90-calendar-day
    window gives enough observations for the 60 trading-day model.
    """
    tickers = [f"{sym}.NS" for sym in symbols]
    try:
        raw = yf.download(
            tickers, period="90d", interval="1d", auto_adjust=True,
            progress=False, group_by="ticker", threads=True
        )
    except Exception as exc:
        raise RuntimeError(f"Market-data download failed: {exc}") from exc

    data = {}
    for sym in symbols:
        ticker = f"{sym}.NS"
        try:
            if len(symbols) == 1:
                close = raw["Close"] if "Close" in raw else raw["close"]
            else:
                close = raw[ticker]["Close"]
            values = pd.to_numeric(close, errors="coerce").dropna().tolist()
        except Exception:
            values = []
        if len(values) < 20:
            raise RuntimeError(
                f"Not enough market data for {sym}. Yahoo Finance returned "
                f"{len(values)} observations; at least 20 are required."
            )
        data[sym] = values[-60:]
    return data


def run_qaoa_batch(batch, price_history, budget, risk_factor):
    """The real quantum step for one small batch: build a Markowitz-style QUBO
    and solve it with QAOA on Qiskit's simulator, verified against the exact
    classical optimum."""
    n = len(batch)
    returns_matrix = np.array([
        np.diff(price_history[s]) / np.array(price_history[s])[:-1] for s in batch
    ])
    mu = returns_matrix.mean(axis=1)
    sigma = np.cov(returns_matrix) if n > 1 else np.array([[returns_matrix.var()]])

    qp = QuadraticProgram(name="batch_selection")
    for i in range(n):
        qp.binary_var(name=f"x_{i}")
    linear = {f"x_{i}": -float(mu[i]) for i in range(n)}
    quadratic = {
        (f"x_{i}", f"x_{j}"): float(risk_factor * sigma[i][j])
        for i in range(n) for j in range(n)
    }
    qp.minimize(linear=linear, quadratic=quadratic)
    qp.linear_constraint(
        linear={f"x_{i}": 1 for i in range(n)},
        sense="==",
        rhs=min(budget, n),
        name="budget",
    )

    qaoa = QAOA(sampler=StatevectorSampler(), optimizer=COBYLA(maxiter=25), reps=1)
    result = MinimumEigenOptimizer(qaoa).solve(qp)
    exact = MinimumEigenOptimizer(NumPyMinimumEigensolver()).solve(qp)
    matched = bool(np.array_equal(result.x, exact.x))

    out = []
    for i, sym in enumerate(batch):
        picked = bool(round(result.x[i]) == 1)
        out.append({
            "symbol": sym,
            "quantum_selected": picked,
            "signal": "BUY" if picked else "AVOID",
            "expected_return_pct": round(float(mu[i]) * 100, 3),
            "risk_estimate": round(float(sigma[i][i]) ** 0.5, 4),
        })
    return out, matched, round(float(result.fval), 6)


def process_one_batch(universe, state, master):
    """Advance the rotation by exactly one batch."""
    n = len(universe)
    start = state["cursor"]
    end = start + BATCH_SIZE
    batch_entries = (universe + universe)[start:end]  # wrap around the end
    symbols = [e["symbol"] for e in batch_entries]

    price_history = fetch_market_prices(symbols)
    results, matched, objective = run_qaoa_batch(
        symbols, price_history, BUDGET_PER_BATCH, RISK_FACTOR
    )

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    by_symbol = {e["symbol"]: e for e in batch_entries}
    for r in results:
        meta = by_symbol[r["symbol"]]
        master[r["symbol"]] = {
            **r,
            "name": meta["name"],
            "industry": meta["industry"],
            "last_updated": now,
            "cycle": state["cycle"],
        }

    new_cursor = end % n
    wrapped = end >= n
    state["cursor"] = new_cursor
    state["analyzed_this_cycle"] = state.get("analyzed_this_cycle", 0) + len(symbols)
    if wrapped:
        state["analyzed_this_cycle"] = new_cursor  # the overflow part starts the new cycle count
        state["cycle"] += 1

    latest_batch_output = {
        "generated_at": now,
        "engine": "QAOA on Qiskit + Yahoo Finance NSE daily data",
        "data_source": "Yahoo Finance (.NS tickers)",
        "market_data_interval": "1d",
        "batch_symbols": symbols,
        "matched_classical_optimum": matched,
        "objective_value": objective,
        "signals": results,
        "cycle": state["cycle"],
    }
    with open(LATEST_BATCH_FILE, "w") as f:
        json.dump(latest_batch_output, f, indent=2)

    return wrapped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["single", "full"], default="single",
        help="'single' processes one batch (use this in a cron/scheduler in "
             "production). 'full' loops until the entire universe has been "
             "covered once — useful to populate a demo from scratch."
    )
    args = parser.parse_args()

    universe = load_universe()
    state = load_state(len(universe))
    master = load_master()

    if args.mode == "single":
        process_one_batch(universe, state, master)
        save_state(state)
        save_master(master)
        print(f"Processed batch. cursor={state['cursor']} cycle={state['cycle']} "
              f"analyzed_this_cycle={state['analyzed_this_cycle']}/{len(universe)}")
    else:
        start_cycle = state["cycle"]
        batches_run = 0
        while True:
            wrapped = process_one_batch(universe, state, master)
            batches_run += 1
            if batches_run % 10 == 0:
                print(f"...{state['analyzed_this_cycle']}/{len(universe)} analyzed "
                      f"({batches_run} batches so far)")
            if wrapped:
                break
        save_state(state)
        save_master(master)
        print(f"Full cycle {start_cycle} complete in {batches_run} batches. "
              f"{len(master)} stocks now in master_signals.json.")


if __name__ == "__main__":
    main()
