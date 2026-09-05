import React, { useState, useEffect, useCallback } from "react";

const API_BASE = window.location.port === "3000" ? "http://localhost:8000" : "";

function pumpBadgeStyle(level) {
  if (level === "high") return { background: "#1db95422", color: "#1db954", border: "1px solid #1db954" };
  if (level === "medium") return { background: "#e0a80822", color: "#e0a808", border: "1px solid #e0a808" };
  return { background: "#8b949e22", color: "#8b949e", border: "1px solid #8b949e" };
}

function tierBadgeStyle(tier) {
  if (tier === "high") return { background: "#1db95422", color: "#1db954", border: "1px solid #1db954" };
  if (tier === "medium") return { background: "#e0a80822", color: "#e0a808", border: "1px solid #e0a808" };
  return { background: "#8b949e22", color: "#8b949e", border: "1px solid #8b949e" };
}

function scoreColor(score) {
  if (score >= 75) return "#1db954";
  if (score >= 55) return "#e0a800";
  return "#c0392b";
}

function Badge({ text, style }) {
  return (
    <span style={{ ...style, padding: "2px 8px", borderRadius: 12, fontSize: 11, fontWeight: 700, textTransform: "uppercase" }}>
      {text}
    </span>
  );
}

function ScannerTab() {
  const [results, setResults] = useState([]);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [minVolume, setMinVolume] = useState(500000);
  const [showRawBreakdown, setShowRawBreakdown] = useState(false);

  const loadLatest = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/scan/latest`);
      const data = await res.json();
      setResults(data.results || []);
      setMeta(data.meta || null);
    } catch (e) {
      setError("Could not reach backend.");
    }
  }, []);

  useEffect(() => {
    loadLatest();
  }, [loadLatest]);

  async function runScan() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/scan?min_volume_usdt=${minVolume}`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "Scan failed");
      }
      const data = await res.json();
      setResults(data.results || []);
      setMeta(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function openDetail(r) {
    setSelected(r);
    setShowRawBreakdown(false);
  }

  const timeEstimate = selected?.breakdown?.time_estimate;

  return (
    <>
      <div style={styles.controls}>
        <label style={styles.label}>
          Min 24h volume (USDT)
          <input
            type="number"
            value={minVolume}
            onChange={(e) => setMinVolume(Number(e.target.value))}
            style={styles.input}
          />
        </label>
        <button onClick={runScan} disabled={loading} style={styles.button}>
          {loading ? "Scanning…" : "Run Scan"}
        </button>
        <a href={`${API_BASE}/api/export/csv`} style={styles.linkButton}>
          Export CSV
        </a>
        <button
          onClick={() => {
            const rank = { high: 0, medium: 1, low: 2 };
            setResults((prev) =>
              [...prev].sort((a, b) => {
                const la = a.breakdown?.early_pump_signal?.early_pump_probability || "low";
                const lb = b.breakdown?.early_pump_signal?.early_pump_probability || "low";
                return rank[la] - rank[lb];
              })
            );
          }}
          style={styles.linkButton}
        >
          Sort by Pump Signal
        </button>
      </div>

      {error && <div style={styles.error}>{error}</div>}

      {meta && (
        <div style={styles.metaBar}>
          Scanned {meta.scanned ?? "—"} · Gate-failed {meta.gate_failed ?? "—"} · Qualified{" "}
          {meta.qualified ?? results.length}
          {meta.high_score_count != null && <> · High-score {meta.high_score_count}</>}
        </div>
      )}

      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Symbol</th>
              <th style={styles.th}>Tier</th>
              <th style={styles.th}>Score</th>
              <th style={styles.th}>Pump Signal</th>
              <th style={styles.th}>Monthly RSI</th>
              <th style={styles.th}>Entry</th>
              <th style={styles.th}>SL</th>
              <th style={styles.th}>TP1</th>
              <th style={styles.th}>TP2</th>
              <th style={styles.th}>TP3</th>
              <th style={styles.th}>24h Vol</th>
            </tr>
          </thead>
          <tbody>
            {results.length === 0 && (
              <tr>
                <td colSpan={11} style={styles.empty}>
                  No results yet — tap "Run Scan".
                </td>
              </tr>
            )}
            {results.map((r) => (
              <tr key={r.symbol} onClick={() => openDetail(r)} style={styles.row}>
                <td style={styles.tdSymbol}>{r.symbol}</td>
                <td style={styles.td}>{r.tier && <Badge text={r.tier} style={tierBadgeStyle(r.tier)} />}</td>
                <td style={{ ...styles.td, color: scoreColor(r.score), fontWeight: 700 }}>{r.score}</td>
                <td style={styles.td}>
                  {(() => {
                    const level = r.breakdown?.early_pump_signal?.early_pump_probability || "low";
                    return <Badge text={level} style={pumpBadgeStyle(level)} />;
                  })()}
                </td>
                <td style={styles.td}>{r.monthly_rsi}</td>
                <td style={styles.td}>{r.entry}</td>
                <td style={styles.td}>{r.stop_loss}</td>
                <td style={styles.td}>{r.tp1}</td>
                <td style={styles.td}>{r.tp2}</td>
                <td style={styles.td}>{r.tp3}</td>
                <td style={styles.td}>{Number(r.volume_24h).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div style={styles.modalBackdrop} onClick={() => setSelected(null)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <h2 style={{ marginTop: 0 }}>{selected.symbol}</h2>

            <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
              {selected.tier && <Badge text={selected.tier} style={tierBadgeStyle(selected.tier)} />}
              {selected.breakdown?.early_pump_signal?.early_pump_probability && (
                <Badge
                  text={`Pump: ${selected.breakdown.early_pump_signal.early_pump_probability}`}
                  style={pumpBadgeStyle(selected.breakdown.early_pump_signal.early_pump_probability)}
                />
              )}
            </div>

            <p style={{ margin: "4px 0" }}>
              Score: <b>{selected.score}</b> / 100
            </p>
            {selected.monthly_rsi != null && <p style={{ margin: "4px 0" }}>Monthly RSI: {selected.monthly_rsi}</p>}

            {selected.breakdown?.contract_address && (
              <p style={{ wordBreak: "break-all", fontSize: 12, color: "#8b949e", marginTop: 10 }}>
                Contract: <span style={{ fontFamily: "monospace" }}>{selected.breakdown.contract_address}</span>
              </p>
            )}

            <div style={{ display: "flex", gap: 8, margin: "10px 0", flexWrap: "wrap" }}>
              {selected.breakdown?.dexscreener_url && (
                <a href={selected.breakdown.dexscreener_url} target="_blank" rel="noopener noreferrer" style={styles.linkButton}>
                  View on DexScreener
                </a>
              )}
              {selected.breakdown?.binance_url && (
                <a href={selected.breakdown.binance_url} target="_blank" rel="noopener noreferrer" style={styles.linkButton}>
                  View on Binance
                </a>
              )}
            </div>

            {selected.breakdown?.market_regime && (
              <div style={{ fontSize: 12, color: "#8b949e", margin: "10px 0", padding: 8, background: "#0d1117", borderRadius: 6 }}>
                Market regime: <b>{selected.breakdown.market_regime.regime}</b> (24h cap {selected.breakdown.market_regime.total_market_cap_change_24h_pct}%)
                {selected.breakdown.market_regime.score_penalty_applied > 0 && (
                  <> — score penalty: -{selected.breakdown.market_regime.score_penalty_applied}</>
                )}
              </div>
            )}

            {selected.breakdown?.confirmation_candle && (
              <div style={{ fontSize: 12, color: selected.breakdown.confirmation_candle.confirmed ? "#1db954" : "#e0a808", marginBottom: 10 }}>
                {selected.breakdown.confirmation_candle.note}
              </div>
            )}

            <h3 style={styles.sectionTitle}>Trade setup</h3>
            <table style={styles.miniTable}>
              <tbody>
                <tr>
                  <td style={styles.miniTd}>Entry</td>
                  <td style={styles.miniTdVal}>{selected.entry}</td>
                  <td style={styles.miniTdTime}></td>
                </tr>
                <tr>
                  <td style={styles.miniTd}>Stop-loss</td>
                  <td style={styles.miniTdVal}>{selected.stop_loss}</td>
                  <td style={styles.miniTdTime}></td>
                </tr>
                <tr>
                  <td style={styles.miniTd}>TP1</td>
                  <td style={styles.miniTdVal}>{selected.tp1}</td>
                  <td style={styles.miniTdTime}>{timeEstimate?.tp1_human || "—"}</td>
                </tr>
                <tr>
                  <td style={styles.miniTd}>TP2</td>
                  <td style={styles.miniTdVal}>{selected.tp2}</td>
                  <td style={styles.miniTdTime}>{timeEstimate?.tp2_human || "—"}</td>
                </tr>
                <tr>
                  <td style={styles.miniTd}>TP3</td>
                  <td style={styles.miniTdVal}>{selected.tp3}</td>
                  <td style={styles.miniTdTime}>{timeEstimate?.tp3_human || "—"}</td>
                </tr>
              </tbody>
            </table>
            {timeEstimate?.note && <p style={{ fontSize: 11, color: "#6e7681", marginTop: 6 }}>{timeEstimate.note}</p>}

            {selected.breakdown?.risk_management?.note && (
              <p style={{ fontSize: 11, color: "#6e7681", marginTop: 10 }}>{selected.breakdown.risk_management.note}</p>
            )}

            <button onClick={() => setShowRawBreakdown((v) => !v)} style={{ ...styles.linkButton, marginTop: 14 }}>
              {showRawBreakdown ? "Hide" : "Show"} full breakdown
            </button>
            {showRawBreakdown && <pre style={styles.pre}>{JSON.stringify(selected.breakdown, null, 2)}</pre>}

            <button onClick={() => setSelected(null)} style={{ ...styles.button, marginTop: 14 }}>
              Close
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function BacktestTab() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [days, setDays] = useState(500);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [history, setHistory] = useState([]);

  async function runBacktest() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/backtest?symbol=${encodeURIComponent(symbol)}&days=${days}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Backtest failed");
      setResult(data);
      setHistory((prev) => [data, ...prev.filter((h) => h.symbol !== data.symbol)]);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function winRateColor(pct) {
    if (pct >= 50) return "#1db954";
    if (pct >= 30) return "#e0a800";
    return "#c0392b";
  }

  return (
    <>
      <div style={styles.controls}>
        <label style={styles.label}>
          Symbol
          <input
            type="text"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            placeholder="BTCUSDT"
            style={styles.input}
          />
        </label>
        <label style={styles.label}>
          Days of history
          <input type="number" value={days} onChange={(e) => setDays(Number(e.target.value))} style={styles.input} />
        </label>
        <button onClick={runBacktest} disabled={loading || !symbol} style={styles.button}>
          {loading ? "Running…" : "Run Backtest"}
        </button>
      </div>

      {error && <div style={styles.error}>{error}</div>}

      {result && result.total_setups === 0 && (
        <div style={styles.metaBar}>{result.message}</div>
      )}

      {result && result.total_setups > 0 && (
        <div style={styles.card}>
          <h2 style={{ marginTop: 0 }}>{result.symbol}</h2>
          <p style={{ fontSize: 12, color: "#8b949e" }}>
            {result.candles_fetched} daily candles tested · {result.total_setups} qualifying setups found
          </p>

          <div style={styles.statRow}>
            <div style={styles.statBox}>
              <div style={{ fontSize: 24, fontWeight: 800, color: winRateColor(result.win_rate_pct) }}>
                {result.win_rate_pct}%
              </div>
              <div style={styles.statLabel}>Hit TP1 or better</div>
            </div>
            <div style={styles.statBox}>
              <div style={{ fontSize: 24, fontWeight: 800, color: "#c0392b" }}>{result.stop_loss_hit_pct}%</div>
              <div style={styles.statLabel}>Hit stop-loss</div>
            </div>
            <div style={styles.statBox}>
              <div style={{ fontSize: 24, fontWeight: 800, color: "#8b949e" }}>{result.no_outcome_within_window_pct}%</div>
              <div style={styles.statLabel}>No outcome ({result.max_hold_days}d)</div>
            </div>
          </div>

          <table style={styles.miniTable}>
            <tbody>
              <tr>
                <td style={styles.miniTd}>TP1 only</td>
                <td style={styles.miniTdVal}>{result.tp1_only}</td>
              </tr>
              <tr>
                <td style={styles.miniTd}>TP2 reached</td>
                <td style={styles.miniTdVal}>{result.tp2_reached}</td>
              </tr>
              <tr>
                <td style={styles.miniTd}>TP3 reached</td>
                <td style={styles.miniTdVal}>{result.tp3_reached}</td>
              </tr>
              <tr>
                <td style={styles.miniTd}>Stop-loss hit</td>
                <td style={styles.miniTdVal}>{result.stop_loss_hit}</td>
              </tr>
            </tbody>
          </table>

          <p style={{ fontSize: 11, color: "#6e7681", marginTop: 12 }}>{result.disclaimer}</p>
        </div>
      )}

      {history.length > 1 && (
        <>
          <h3 style={styles.sectionTitle}>Compared so far</h3>
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>Symbol</th>
                  <th style={styles.th}>Setups</th>
                  <th style={styles.th}>Win rate</th>
                  <th style={styles.th}>Stop rate</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.symbol}>
                    <td style={styles.tdSymbol}>{h.symbol}</td>
                    <td style={styles.td}>{h.total_setups}</td>
                    <td style={{ ...styles.td, color: winRateColor(h.win_rate_pct), fontWeight: 700 }}>{h.win_rate_pct}%</td>
                    <td style={styles.td}>{h.stop_loss_hit_pct}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

export default function App() {
  const [tab, setTab] = useState("scanner");

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>⚡ Alpha Hunter Pro</h1>
        <p style={styles.subtitle}>Binance Alpha Token Hunting &amp; Reversal Scanner</p>
      </header>

      <div style={styles.tabs}>
        <button onClick={() => setTab("scanner")} style={tab === "scanner" ? styles.tabActive : styles.tab}>
          Scanner
        </button>
        <button onClick={() => setTab("backtest")} style={tab === "backtest" ? styles.tabActive : styles.tab}>
          Backtest
        </button>
      </div>

      {tab === "scanner" ? <ScannerTab /> : <BacktestTab />}
    </div>
  );
}

const styles = {
  page: { fontFamily: "system-ui, sans-serif", background: "#0d1117", color: "#e6edf3", minHeight: "100vh", padding: "16px" },
  header: { marginBottom: 12 },
  title: { margin: 0, fontSize: 24 },
  subtitle: { margin: "4px 0 0", color: "#8b949e", fontSize: 13 },
  tabs: { display: "flex", gap: 8, marginBottom: 16, borderBottom: "1px solid #30363d" },
  tab: { padding: "8px 16px", background: "transparent", border: "none", color: "#8b949e", cursor: "pointer", fontSize: 14, fontWeight: 600, borderBottom: "2px solid transparent" },
  tabActive: { padding: "8px 16px", background: "transparent", border: "none", color: "#e6edf3", cursor: "pointer", fontSize: 14, fontWeight: 600, borderBottom: "2px solid #238636" },
  controls: { display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 },
  label: { display: "flex", flexDirection: "column", fontSize: 12, color: "#8b949e" },
  input: { marginTop: 4, padding: "6px 8px", borderRadius: 6, border: "1px solid #30363d", background: "#161b22", color: "#e6edf3" },
  button: { padding: "8px 16px", borderRadius: 6, border: "none", background: "#238636", color: "#fff", cursor: "pointer", fontWeight: 600 },
  linkButton: { padding: "8px 16px", borderRadius: 6, background: "#30363d", color: "#e6edf3", textDecoration: "none", fontSize: 14, border: "none", cursor: "pointer" },
  error: { background: "#3d1418", color: "#ff6b6b", padding: 10, borderRadius: 6, marginBottom: 12 },
  metaBar: { fontSize: 12, color: "#8b949e", marginBottom: 8 },
  tableWrap: { overflowX: "auto", border: "1px solid #30363d", borderRadius: 8 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: { textAlign: "left", padding: "10px 12px", background: "#161b22", borderBottom: "1px solid #30363d", whiteSpace: "nowrap" },
  td: { padding: "8px 12px", borderBottom: "1px solid #21262d", whiteSpace: "nowrap" },
  tdSymbol: { padding: "8px 12px", borderBottom: "1px solid #21262d", fontWeight: 700, whiteSpace: "nowrap" },
  row: { cursor: "pointer" },
  empty: { padding: 20, textAlign: "center", color: "#8b949e" },
  modalBackdrop: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 },
  modal: { background: "#161b22", border: "1px solid #30363d", borderRadius: 10, padding: 20, maxWidth: 480, width: "100%", maxHeight: "80vh", overflowY: "auto" },
  sectionTitle: { fontSize: 14, marginBottom: 6, marginTop: 16, color: "#8b949e" },
  miniTable: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  miniTd: { padding: "4px 6px", color: "#8b949e", borderBottom: "1px solid #21262d" },
  miniTdVal: { padding: "4px 6px", fontWeight: 600, borderBottom: "1px solid #21262d" },
  miniTdTime: { padding: "4px 6px", color: "#e0a808", textAlign: "right", borderBottom: "1px solid #21262d", fontSize: 12 },
  pre: { background: "#0d1117", padding: 12, borderRadius: 6, overflowX: "auto", fontSize: 12, marginTop: 8 },
  card: { border: "1px solid #30363d", borderRadius: 10, padding: 16, marginBottom: 16, background: "#161b22" },
  statRow: { display: "
