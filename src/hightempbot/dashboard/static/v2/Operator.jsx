/* eslint-disable */
function shortAddr(addr) {
  if (!addr) return "-";
  const s = String(addr);
  return s.length > 14 ? `${s.slice(0, 6)}...${s.slice(-4)}` : s;
}

function statusPill(ok, label) {
  return <span className={"pill " + (ok ? "pill-green" : "pill-yellow")}>{label}</span>;
}

function fmtStatus(status) {
  return String(status || "-")
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function OperatorPage({ d, setData }) {
  const operator = d.operator || {};
  const wallet = d.wallet || {};
  const readiness = d.readiness || {};
  const snapshot = wallet.snapshot || {};
  const autoRedeem = wallet.autoRedeem || {};
  const recentRedemptions = autoRedeem.recent || [];
  const pendingPositionCount = Number(
    snapshot.openPositionsCount ?? snapshot.dataApiOpenPositionsCount ?? 0
  );
  const pendingPositionValue = Number(
    snapshot.dataApiTrustedOpenPositionsValueUsd
    ?? snapshot.dataApiOpenPositionsValueUsd
    ?? snapshot.dataApiOpenPositionsInitialValueUsd
    ?? 0
  );
  const [busy, setBusy] = React.useState("");
  const [amount, setAmount] = React.useState("");
  const [preview, setPreview] = React.useState(null);
  const [message, setMessage] = React.useState("");
  const [redemptionPage, setRedemptionPage] = React.useState(0);
  // Snapshot the previewed amount + generated confirmation. Submit reads from
  // this ref so an operator who edits the amount after Preview cannot ship a
  // different transfer than the one safety checks approved.
  const previewedRef = React.useRef(null);

  const refresh = async () => {
    const resp = await fetch("/api/v2/data", { cache: "no-store" });
    if (resp.ok) setData(await resp.json());
  };

  const post = async (url, body = {}) => {
    setBusy(url);
    setMessage("");
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok || payload.ok === false) {
        const detail = payload.detail || (payload.preview && payload.preview.errors && payload.preview.errors.join("; ")) || "Action refused";
        setMessage(detail);
      } else {
        setMessage("OK");
        // Mark a recent mutation so the App-level
        // 30s poll defers its next /api/v2/data read for 5s.
        try { window.__htbMutationAt = Date.now(); } catch (e) {}
        await refresh();
      }
      return payload;
    } finally {
      setBusy("");
    }
  };

  const runPreview = async () => {
    const payload = await post("/api/v2/admin/operator/transfer/preview", {
      amount,
    });
    if (payload.preview) {
      setPreview(payload.preview);
      // Snapshot the previewed values for the submit path.
      previewedRef.current = {
        amount,
        confirmation: payload.preview.confirmation || "",
      };
    }
    return payload;
  };

  const submitTransfer = async () => {
    let snap = previewedRef.current;
    if (
      !snap
      || snap.amount !== amount
      || !preview
      || !preview.ok
    ) {
      const payload = await runPreview();
      if (!payload || !payload.preview || !payload.preview.ok) return;
      snap = previewedRef.current;
    }
    if (!snap) {
      setMessage("Preview failed");
      return;
    }
    const locked = await post("/api/v2/admin/operator/transfer/lock", {
      reason: "dashboard transfer",
    });
    if (!locked || locked.ok === false) return;
    return post("/api/v2/admin/operator/transfer/submit", {
      amount: snap.amount,
      confirmation: snap.confirmation,
    });
  };

  const warnings = Array.from(new Set([]
    .concat(wallet.warnings || [])
    .concat(wallet.reconciliationWarnings || [])
    .concat((snapshot.warnings || []).filter(Boolean))
    .concat((snapshot.dataApiReconciliationWarnings || []).filter(Boolean))
    .filter(Boolean)));
  const canAttemptLiveAction = !operator.bootDryRun;
  const transferEligible = !!wallet.transferEligible;
  const returnWallet = wallet.returnWallet || "";
  const resetPreview = () => {
    setPreview(null);
    previewedRef.current = null;
  };
  const redemptionPageSize = 20;
  const redemptionPageCount = Math.max(1, Math.ceil(recentRedemptions.length / redemptionPageSize));
  const clampedRedemptionPage = Math.min(redemptionPage, redemptionPageCount - 1);
  React.useEffect(() => {
    if (redemptionPage !== clampedRedemptionPage) {
      setRedemptionPage(clampedRedemptionPage);
    }
  }, [redemptionPage, clampedRedemptionPage]);
  const redemptionStart = clampedRedemptionPage * redemptionPageSize;
  const redemptionEnd = Math.min(redemptionStart + redemptionPageSize, recentRedemptions.length);
  const pagedRedemptions = recentRedemptions.slice(redemptionStart, redemptionEnd);

  return (
    <div className="content" data-screen-label="Operator">
      <div className="operator-grid">
        <div className="card lg">
          <div className="card-head">
            <div>
              <div className="card-title">Processing state</div>
              <div className="operator-sub">SQLite control state, independent from .env</div>
            </div>
            {statusPill(operator.processingEnabled, operator.state || "UNKNOWN")}
          </div>
          <div className="operator-state-row">
            <div>
              <div className="operator-label">Boot mode</div>
              <div className="operator-value">{operator.bootDryRun ? "DRY_RUN" : "LIVE"}</div>
            </div>
            <div>
              <div className="operator-label">Updated</div>
              <div className="operator-value mono">{operator.updatedAt || "-"}</div>
            </div>
          </div>
          <div className="operator-actions">
            <button className="op-btn danger" disabled={!!busy} onClick={() => post("/api/v2/admin/operator/stop", { reason: "dashboard stop" })}>
              Stop Processing
            </button>
            <button className="op-btn primary" disabled={!!busy || !canAttemptLiveAction || operator.processingEnabled} onClick={() => post("/api/v2/admin/operator/start", { reason: "dashboard start" })}>
              Start Processing
            </button>
          </div>
          {operator.reason && <div className="operator-note">{operator.reason}</div>}
        </div>

        <div className="card lg">
          <div className="card-head">
            <div>
              <div className="card-title">Readiness</div>
              <div className="operator-sub">Latest shared live preflight report</div>
            </div>
            {statusPill(readiness.status === "OK" || readiness.status === "SKIPPED", readiness.status || "UNKNOWN")}
          </div>
          <div className="operator-checks">
            {(readiness.checks || []).slice(0, 8).map((c, i) => (
              <div key={i} className="operator-check">
                <span className={"check-dot " + (c.status === "OK" || c.status === "SKIPPED" ? "ok" : c.status === "WARNING" ? "warn" : "bad")} />
                <span>{c.name}</span>
                <span className="muted">{c.status}</span>
              </div>
            ))}
            {(!readiness.checks || readiness.checks.length === 0) && <div className="muted">No readiness report recorded yet.</div>}
          </div>
        </div>
      </div>

      <div className="card lg" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div>
            <div className="card-title">POLY_FUNDER wallet record</div>
            <div className="operator-sub">Dashboard history follows this wallet first; ledger rows enrich strategy context.</div>
          </div>
          {statusPill(wallet.fresh, wallet.fresh ? "fresh" : "stale")}
        </div>
        <div className="wallet-row">
          <div className="wallet-block">
            <div className="wallet-key">Wallet</div>
            <div className="wallet-addr">
              <span title={wallet.primaryWallet || "-"}>
                {wallet.primaryWallet
                  ? `${wallet.primaryWallet.slice(0, 6)}…${wallet.primaryWallet.slice(-6)}`
                  : "-"}
              </span>
              {wallet.primaryWallet && (
                <button
                  type="button"
                  className="copy"
                  title="Copy address"
                  onClick={() => navigator.clipboard && navigator.clipboard.writeText(wallet.primaryWallet)}
                >
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" /></svg>
                </button>
              )}
            </div>
          </div>
          <div className="sampled-block">
            <div className="wallet-key">Sampled</div>
            <div className="ts mono">{snapshot.sampledAt || "-"}</div>
          </div>
        </div>
        <div className="metric-grid">
          <div className="metric-cell"><div className="metric-label">CLOB pUSD</div><div className="metric-value">{snapshot.clobBalanceUsd == null ? "-" : "$" + Number(snapshot.clobBalanceUsd).toFixed(2)}</div></div>
          <div className="metric-cell"><div className="metric-label">On-chain pUSD</div><div className="metric-value">{snapshot.chainBalanceUsd == null ? "-" : "$" + Number(snapshot.chainBalanceUsd).toFixed(2)}</div></div>
          <div className="metric-cell"><div className="metric-label">Open orders</div><div className="metric-value">{snapshot.openOrdersCount || 0}</div></div>
          <div className="metric-cell"><div className="metric-label">Open positions</div><div className="metric-value">{snapshot.openPositionsCount || 0}</div></div>
          <div className="metric-cell split"><div className="metric-label">Pending positions</div><div className="metric-value">{pendingPositionCount || 0} <span className="slash">/</span> ${pendingPositionValue.toFixed(2)}</div></div>
        </div>
        {recentRedemptions.length > 0 && (
          <>
            <table className="tbl compact">
              <thead><tr><th>Auto redeem</th><th>Side</th><th>Value</th><th>Status</th></tr></thead>
              <tbody>
                {pagedRedemptions.map((r, i) => (
                  <tr key={`${r.tokenId || "redemption"}-${redemptionStart + i}`}>
                    <td className="mono">{r.updatedAt || r.createdAt || "-"}</td>
                    <td>{r.outcome || "-"}</td>
                    <td>{r.currentValueUsd == null ? "-" : "$" + Number(r.currentValueUsd).toFixed(2)}</td>
                    <td>{fmtStatus(r.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="table-pager">
              <span className="muted">Showing {redemptionStart + 1}-{redemptionEnd} of {recentRedemptions.length}</span>
              <div className="pager-actions">
                <button
                  type="button"
                  className="op-btn"
                  disabled={clampedRedemptionPage <= 0}
                  onClick={() => setRedemptionPage((p) => Math.max(0, p - 1))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="op-btn"
                  disabled={clampedRedemptionPage >= redemptionPageCount - 1}
                  onClick={() => setRedemptionPage((p) => Math.min(redemptionPageCount - 1, p + 1))}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
        {warnings.length > 0 && (
          <div className="operator-warnings">
            {warnings.map((w, i) => <div key={i}>{w}</div>)}
          </div>
        )}
      </div>

      <div className="operator-grid" style={{ marginTop: 14 }}>
        <div className="card lg">
          <div className="card-head">
            <div>
              <div className="card-title">Return transfer</div>
              <div className="operator-sub">pUSD from bot deposit wallet to configured return wallet</div>
            </div>
            {statusPill(transferEligible, transferEligible ? "ready" : "locked")}
          </div>
          <div className="transfer-form">
            <label>
              <span>Amount</span>
              <input value={amount} onChange={(e) => { setAmount(e.target.value); resetPreview(); }} placeholder="0.00" inputMode="decimal" />
            </label>
            <div className="transfer-destination">
              <span>Return wallet</span>
              <div className="operator-value mono">{returnWallet || "POLY_RETURN_WALLET not configured"}</div>
            </div>
          </div>
          <div className="operator-actions">
            <button className="op-btn" disabled={!!busy} onClick={runPreview}>Preview</button>
            <button className="op-btn danger" disabled={!!busy || !amount || !canAttemptLiveAction} onClick={submitTransfer}>Transfer</button>
          </div>
          {wallet.transferBlockedReason && <div className="operator-note">{wallet.transferBlockedReason}</div>}
          {preview && (
            <div className={"transfer-preview " + (preview.ok ? "ok" : "bad")}>
              <div><strong>{preview.ok ? "Preview OK" : "Preview refused"}</strong></div>
              {preview.confirmation && <div className="mono">{preview.confirmation}</div>}
              {(preview.errors || []).map((e, i) => <div key={i} className="neg">{e}</div>)}
              {(preview.warnings || []).map((w, i) => <div key={"w" + i} className="muted">{w}</div>)}
            </div>
          )}
          {message && <div className={"operator-note " + (message === "OK" ? "pos" : "neg")}>{message}</div>}
        </div>

        <div className="card lg">
          <div className="card-head"><div className="card-title">Operator audit</div></div>
          <table className="tbl">
            <thead><tr><th>Time</th><th>Action</th><th>State</th><th>Reason</th></tr></thead>
            <tbody>
              {(operator.events || []).map((e, i) => (
                <tr key={i}>
                  <td className="mono">{e.createdAt || "-"}</td>
                  <td>{e.action}</td>
                  <td>{e.fromState} / {e.toState}</td>
                  <td className="muted">{e.reason || "-"}</td>
                </tr>
              ))}
              {(!operator.events || operator.events.length === 0) && <tr><td colSpan="4" className="muted">No operator mutations yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

window.OperatorPage = OperatorPage;
