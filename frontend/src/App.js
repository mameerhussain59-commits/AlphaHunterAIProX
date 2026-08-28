import React, { useState, useEffect, useCallback } from "react";

// Same-origin in production (backend serves this build from /static).
// In local dev (npm start on :3000), point at the uvicorn server on :8000.
const API_BASE = window.location.port === "3000" ? "http://localhost:8000" : "";

function pumpBadgeStyle(level) {
  if (level === "high") return { background: "#1db95422", color: "#1db954", border: "1px solid #1db954" };
  if (level === "medium") return { background: "#e0a80822", color: "#e0a808", border: "1px solid #e0a808" };
  return { background: "#8b949e22", color: "#8b949e", border: "1px solid #8b949e" };
}

function scoreColor(score) {
  if (score >= 75) return "#1db954";
  if (score >= 55) return "#e0a800";
  return "#c0392b";
}

export default function App() {
  const [results, setResults] = useState([]);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [minVolume, setMinVolume] = useState(500000);

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

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>⚡ Alpha Hunter Pro</h1>
        <p style={styles.subtitle}>Binance Alpha Token Hunting &amp; Reversal Scanner</p>
      </header>

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
        </div>
      )}

      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Symbol</th>
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
                <td colSpan={10} style={styles.empty}>
                  No results yet — tap "Run Scan".
                </td>
              </tr>
            )}
            {results.map((r) => (
              <tr key={r.symbol} onClick={() => setSelected(r)} style={styles.row}>
                <td style={styles.tdSymbol}>{r.symbol}</td>
                <td style={{ ...styles.td, color: scoreColor(r.score), fontWeight: 700 }}>{r.score}</td>
                <td style={styles.td}>
                  {(() => {
                    const level = r.breakdown?.early_pump_signal?.early_pump_probability || "low";
                    return (
                      <span style={{ ...pumpBadgeStyle(level), padding: "2px 8px", borderRadius: 12, fontSize: 11, fontWeight: 700, textTransform: "uppercase" }}>
                        {level}
                      </span>
                    );
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
            <h2>{selected.symbol}</h2>
            <p>Score: {selected.score} / 100</p>
            <p>Monthly RSI: {selected.monthly_rsi}</p>
            <h3>Timeframe breakdown</h3>
            <pre style={styles.pre}>{JSON.stringify(selected.breakdown, null, 2)}</pre>
            <button onClick={() => setSelected(null)} style={styles.button}>
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const styles = {
  page: { fontFamily: "system-ui, sans-serif", background: "#0d1117", color: "#e6edf3", minHeight: "100vh", padding: "16px" },
  header: { marginBottom: 16 },
  title: { margin: 0, fontSize: 24 },
  subtitle: { margin: "4px 0 0", color: "#8b949e", fontSize: 13 },
  controls: { display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 },
  label: { display: "flex", flexDirection: "column", fontSize: 12, color: "#8b949e" },
  input: { marginTop: 4, padding: "6px 8px", borderRadius: 6, border: "1px solid #30363d", background: "#161b22", color: "#e6edf3" },
  button: { padding: "8px 16px", borderRadius: 6, border: "none", background: "#238636", color: "#fff", cursor: "pointer", fontWeight: 600 },
  linkButton: { padding: "8px 16px", borderRadius: 6, background: "#30363d", color: "#e6edf3", textDecoration: "none", fontSize: 14 },
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
  pre: { background: "#0d1117", padding: 12, borderRadius: 6, overflowX: "auto", fontSize: 12 },
};
