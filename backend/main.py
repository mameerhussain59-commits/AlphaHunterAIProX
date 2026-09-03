import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete

from database import init_db, async_session, ScanResult
from scanner import run_scan
import market_regime
import multichain
import dex_scanner
import telegram_alerts

app = FastAPI(title="Alpha Hunter Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_last_scan_meta = {"scan_id": None, "scanned": 0, "gate_failed": 0, "qualified": 0, "finished_at": None}
_scan_lock_running = False
_previously_alerted_high_symbols = set()

HIGH_SCORE_THRESHOLD = float(os.getenv("HIGH_SCORE_THRESHOLD", "80"))
MEDIUM_SCORE_THRESHOLD = float(os.getenv("MEDIUM_SCORE_THRESHOLD", "50"))


def get_tier(score: float) -> str:
    if score >= HIGH_SCORE_THRESHOLD:
        return "high"
    elif score >= MEDIUM_SCORE_THRESHOLD:
        return "medium"
    return "low"


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/market-regime")
async def get_market_regime_endpoint():
    return await market_regime.get_market_regime()


@app.get("/api/multichain/trending")
async def get_multichain_trending_endpoint(max_per_chain: int = 10):
    return await multichain.scan_multichain(max_per_chain=max_per_chain)


@app.post("/api/scan")
async def trigger_scan(min_volume_usdt: float = 500_000, max_symbols: int = 300, max_per_chain: int = 10):
    global _scan_lock_running
    if _scan_lock_running:
        raise HTTPException(status_code=409, detail="A scan is already running — try again shortly.")
    _scan_lock_running = True
    try:
        binance_result = await run_scan(min_volume_usdt=min_volume_usdt, max_symbols=max_symbols)
        dex_results = await dex_scanner.scan_dex(max_per_chain=max_per_chain)

        merged = binance_result["results"] + dex_results
        merged.sort(key=lambda r: r["score"], reverse=True)
        merged = merged[:20]

        for r in merged:
            r["tier"] = get_tier(r["score"])

        scan_id = str(uuid.uuid4())

        async with async_session() as session:
            await session.execute(delete(ScanResult))
            for r in merged:
                session.add(ScanResult(
                    scan_id=scan_id,
                    symbol=r["symbol"],
                    score=r["score"],
                    monthly_rsi=r["monthly_rsi"],
                    passed_gate=1,
                    entry=r["entry"],
                    stop_loss=r["stop_loss"],
                    tp1=r["tp1"],
                    tp2=r["tp2"],
                    tp3=r["tp3"],
                    volume_24h=r["volume_24h"],
                    breakdown=json.dumps(r["breakdown"]),
                ))
            await session.commit()

        current_high_symbols = set()
        for r in merged:
            if r["tier"] == "high":
                current_high_symbols.add(r["symbol"])
                if r["symbol"] not in _previously_alerted_high_symbols:
                    message = telegram_alerts.format_high_score_alert(r)
                    await telegram_alerts.send_telegram_message(message)
        _previously_alerted_high_symbols.clear()
        _previously_alerted_high_symbols.update(current_high_symbols)

        _last_scan_meta.update({
            "scan_id": scan_id,
            "scanned": binance_result["scanned"],
            "gate_failed": binance_result["gate_failed"],
            "qualified": len(merged),
            "binance_qualified": binance_result["qualified"],
            "dex_qualified": len(dex_results),
            "high_score_count": len(current_high_symbols),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

        return {"scan_id": scan_id, **_last_scan_meta, "results": merged}
    finally:
        _scan_lock_running = False


@app.get("/api/scan/latest")
async def latest_scan(min_score: float = 0, tier: str | None = None):
    async with async_session() as session:
        query = select(ScanResult).where(ScanResult.score >= min_score).order_by(ScanResult.score.desc())
        res = await session.execute(query)
        rows = res.scalars().all()

    if tier:
        rows = [r for r in rows if get_tier(r.score) == tier]

    if not rows:
        return {"scan_id": None, "results": [], "message": "No scan yet — POST /api/scan to run one."}

    return {
        "scan_id": rows[0].scan_id,
        "meta": _last_scan_meta,
        "results": [
            {
                "symbol": r.symbol,
                "score": r.score,
                "tier": get_tier(r.score),
                "monthly_rsi": r.monthly_rsi,
                "entry": r.entry,
                "stop_loss": r.stop_loss,
                "tp1": r.tp1,
                "tp2": r.tp2,
                "tp3": r.tp3,
                "volume_24h": r.volume_24h,
                "breakdown": json.loads(r.breakdown) if r.breakdown else {},
            }
            for r in rows
        ],
    }


@app.get("/api/scan/high-score")
async def high_score_tokens():
    async with async_session() as session:
        query = select(ScanResult).where(ScanResult.score >= HIGH_SCORE_THRESHOLD).order_by(ScanResult.score.desc())
        res = await session.execute(query)
        rows = res.scalars().all()
    return {
        "threshold": HIGH_SCORE_THRESHOLD,
        "count": len(rows),
        "results": [
            {
                "symbol": r.symbol,
                "score": r.score,
                "tier": "high",
                "monthly_rsi": r.monthly_rsi,
                "entry": r.entry,
                "stop_loss": r.stop_loss,
                "tp1": r.tp1,
                "tp2": r.tp2,
                "tp3": r.tp3,
                "volume_24h": r.volume_24h,
            }
            for r in rows
        ],
    }


@app.get("/api/tokens/{symbol}")
async def token_detail(symbol: str):
    async with async_session() as session:
        res = await session.execute(select(ScanResult).where(ScanResult.symbol == symbol.upper()))
        row = res.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"{symbol} not found in latest scan")
    return {
        "symbol": row.symbol,
        "score": row.score,
        "tier": get_tier(row.score),
        "monthly_rsi": row.monthly_rsi,
        "entry": row.entry,
        "stop_loss": row.stop_loss,
        "tp1": row.tp1,
        "tp2": row.tp2,
        "tp3": row.tp3,
        "volume_24h": row.volume_24h,
        "breakdown": json.loads(row.breakdown) if row.breakdown else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@app.get("/api/export/csv")
async def export_csv():
    async with async_session() as session:
        res = await session.execute(select(ScanResult).order_by(ScanResult.score.desc()))
        rows = res.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol", "score", "tier", "monthly_rsi", "entry", "stop_loss", "tp1", "tp2", "tp3", "volume_24h"])
    for r in rows:
        writer.writerow([r.symbol, r.score, get_tier(r.score), r.monthly_rsi, r.entry, r.stop_loss, r.tp1, r.tp2, r.tp3, r.volume_24h])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=alpha_hunter_scan.csv"},
    )


@app.on_event("startup")
async def on_startup():
    await init_db()


if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
