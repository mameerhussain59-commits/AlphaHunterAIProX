"""
Outcome Tracking Engine — records what actually happens to hunted tokens
after discovery, so we can measure REAL (not backtested) win rate over
time. This is the foundation the rest of a self-learning system (false
positive/negative analysis, strategy versioning, etc.) would build on —
none of that is meaningful without first knowing what actually happened.

v1 scope, kept deliberately simple:
  - Binance-sourced tokens only (DEX price re-checking needs a different
    price source per chain — can be added later).
  - Checkpoints sampled at fixed intervals (15m/1h/4h/24h/3d/7d) rather
    than continuous monitoring — a move that touches TP1 between two
    checkpoints and pulls back before the next check can be missed. This
    is a real limitation, stated plainly rather than hidden.
  - One outcome record per symbol per 24h (avoids spamming duplicate
    records every 5 minutes while the same token stays in the scan).
"""
import json
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select

from database import async_session, TokenOutcome

CHECKPOINTS_MINUTES = [15, 60, 240, 1440, 4320, 10080]
CHECKPOINT_LABELS = {15: "15m", 60: "1h", 240: "4h", 1440: "24h", 4320: "3d", 10080: "7d"}

DEDUP_WINDOW_HOURS = 24  # don't create a new outcome row for a symbol already tracked in the last 24h


async def record_new_outcomes(merged_results: list, scan_id: str):
    """Create outcome records for newly-seen Binance-sourced medium/high
    tier tokens from this scan. Skips a symbol if it already has an open
    (or recent) outcome record, so re-appearing in every 5-min scan doesn't
    create duplicate rows."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)

    async with async_session() as session:
        for r in merged_results:
            if r.get("breakdown", {}).get("source") != "binance":
                continue
            if r.get("tier") not in ("high", "medium"):
                continue

            existing = await session.execute(
                select(TokenOutcome).where(
                    TokenOutcome.symbol == r["symbol"],
                    TokenOutcome.discovered_at >= cutoff,
                )
            )
            if existing.scalars().first():
                continue

            session.add(TokenOutcome(
                symbol=r["symbol"],
                scan_id=scan_id,
                score=r["score"],
                tier=r["tier"],
                entry=r["entry"],
                stop_loss=r["stop_loss"],
                tp1=r["tp1"],
                tp2=r["tp2"],
                tp3=r["tp3"],
                checkpoints="{}",
                status="pending",
            ))
        await session.commit()


async def _fetch_price(client: httpx.AsyncClient, symbol: str):
    try:
        resp = await client.get(
            "https://data-api.binance.vision/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


def _classify(price: float, entry: float, stop_loss: float, tp1: float, tp2: float, tp3: float) -> str:
    if price <= stop_loss:
        return "stopped"
    if price >= tp3:
        return "tp3"
    if price >= tp2:
        return "tp2"
    if price >= tp1:
        return "tp1"
    return "none"


_STATUS_RANK = {"none": 0, "tp1": 1, "tp2": 2, "tp3": 3, "stopped": -1}  # stopped ends tracking regardless of rank


async def check_pending_outcomes():
    """Runs on a schedule (see main.py's background loop). For every open
    outcome record, checks whether any checkpoint is now due, fetches the
    current price once per token, and updates status/checkpoints."""
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        res = await session.execute(select(TokenOutcome).where(TokenOutcome.completed == False))  # noqa: E712
        open_outcomes = res.scalars().all()

    if not open_outcomes:
        return

    async with httpx.AsyncClient() as client:
        for outcome in open_outcomes:
            elapsed_minutes = (now - outcome.discovered_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
            checkpoints = json.loads(outcome.checkpoints or "{}")

            due_labels = [
                CHECKPOINT_LABELS[m] for m in CHECKPOINTS_MINUTES
                if elapsed_minutes >= m and CHECKPOINT_LABELS[m] not in checkpoints
            ]
            if not due_labels:
                continue

            price = await _fetch_price(client, outcome.symbol)
            if price is None:
                continue

            new_status_this_round = _classify(price, outcome.entry, outcome.stop_loss, outcome.tp1, outcome.tp2, outcome.tp3)

            for label in due_labels:
                checkpoints[label] = {"price": price, "classification": new_status_this_round}

            if new_status_this_round == "stopped":
                final_status = "stopped"
                completed = True
            else:
                current_rank = _STATUS_RANK.get(outcome.status, 0)
                new_rank = _STATUS_RANK.get(new_status_this_round, 0)
                final_status = new_status_this_round if new_rank > current_rank else outcome.status
                completed = "7d" in checkpoints

            async with async_session() as session:
                db_outcome = await session.get(TokenOutcome, outcome.id)
                if db_outcome:
                    db_outcome.checkpoints = json.dumps(checkpoints)
                    db_outcome.status = final_status
                    db_outcome.completed = completed
                    await session.commit()


async def get_stats():
    """Real (not backtested) aggregate win/loss stats from completed outcomes."""
    async with async_session() as session:
        res = await session.execute(select(TokenOutcome).where(TokenOutcome.completed == True))  # noqa: E712
        completed = res.scalars().all()

    total = len(completed)
    if total == 0:
        return {"total_completed": 0, "message": "No completed outcomes yet — tracking takes up to 7 days per token."}

    counts = {"tp1": 0, "tp2": 0, "tp3": 0, "stopped": 0, "none": 0}
    for o in completed:
        counts[o.status] = counts.get(o.status, 0) + 1

    wins = counts["tp1"] + counts["tp2"] + counts["tp3"]
    return {
        "total_completed": total,
        "wins_tp1_or_better": wins,
        "win_rate_pct": round(wins / total * 100, 1),
        "tp1_only": counts["tp1"],
        "tp2_reached": counts["tp2"],
        "tp3_reached": counts["tp3"],
        "stopped": counts["stopped"],
        "no_outcome": counts["none"],
        "note": "Real outcomes from live discovered tokens, tracked over up to 7 days. Checkpoint-sampled (not continuous), so brief intra-window touches of a target can be missed.",
    }


async def get_recent(limit: int = 50, status: str = None):
    async with async_session() as session:
        query = select(TokenOutcome).order_by(TokenOutcome.discovered_at.desc()).limit(limit)
        if status:
            query = select(TokenOutcome).where(TokenOutcome.status == status).order_by(TokenOutcome.discovered_at.desc()).limit(limit)
        res = await session.execute(query)
        rows = res.scalars().all()

    return [
        {
            "symbol": r.symbol,
            "discovered_at": r.discovered_at.isoformat() if r.discovered_at else None,
            "score": r.score,
            "tier": r.tier,
            "entry": r.entry,
            "stop_loss": r.stop_loss,
            "tp1": r.tp1,
            "tp2": r.tp2,
            "tp3": r.tp3,
            "status": r.status,
            "completed": r.completed,
            "checkpoints": json.loads(r.checkpoints) if r.checkpoints else {},
        }
        for r in rows
  ]
