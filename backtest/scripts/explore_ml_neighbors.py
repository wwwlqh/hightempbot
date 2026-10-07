"""Do neighbor-augmented ML forecasts beat EMOS ``p_E`` on walk-forward trading
metrics? The ML only replaces the probability; gate, calibration and fills are
the same as sweep_calibrated_gate.py. Features use data through D−1 only.
(Live ERA5 lags ~5 days, so the D−5 ablation is the live-feasible bound.)

    python backtest/scripts/explore_ml_neighbors.py [--stage all|features|trade|forecast]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import backtest.scripts.sweep_calibrated_gate as S  # noqa: E402

MLDS = Path(r"C:\Users\leowq\AppData\Local\Temp\claude"
            r"\c--Users-leowq-OneDrive-Desktop-hightempbot"
            r"\a9ed1d4f-dff5-472f-9a13-e9a899375ce4\scratchpad\mlds")
DECISION = _REPO_ROOT / "backtest/data/decision_table_may11plus_l2.parquet"

CENTRES = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless",
           "meteofrance_seamless", "ukmo_seamless", "knmi_seamless",
           "dmi_seamless", "ncep_gfs013", "jma_seamless"]
POINTS = ["TGT", "N", "NE", "E", "SE", "S", "SW", "W", "NW"]
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# ABCD window starts (from decision table date_chunks) — used to pick which
# leak-free model generates each row's p_ML.
WINDOW_STARTS = {"A": "2026-02-18", "B": "2026-03-13",
                 "C": "2026-04-05", "D": "2026-04-28"}


# --------------------------------------------------------------------------- features
def _daily_grid(df, id_col, date_col, val_cols):
    """Reindex each id to a complete daily date grid (fills gaps with NaN)."""
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    out = []
    for sid, g in df.groupby(id_col, sort=False):
        g = g.sort_values(date_col).set_index(date_col)
        full = pd.date_range(g.index.min(), g.index.max(), freq="D")
        g = g.reindex(full)
        g[id_col] = sid
        out.append(g[[id_col] + val_cols])
    r = pd.concat(out)
    r.index.name = "d"
    return r.reset_index()


def build_features() -> pd.DataFrame:
    fc = pd.read_parquet(MLDS / "forecasts.parquet")
    ac = pd.read_parquet(MLDS / "actuals.parquet")
    nb = pd.read_parquet(MLDS / "neighbors.parquet")

    # ---- ensemble per-centre wide + grand stats ----
    piv = fc.pivot_table(index=["station_id", "target_date"], columns="centre",
                         values="tmax_celsius", aggfunc="last")
    piv = piv.reindex(columns=CENTRES)
    ens = pd.DataFrame(index=piv.index)
    vals = piv[CENTRES].to_numpy(dtype=float)
    with np.errstate(all="ignore"):
        ens["ens_mean"] = np.nanmean(vals, axis=1)
        ens["ens_std"] = np.nanstd(vals, axis=1)
        ens["ens_min"] = np.nanmin(vals, axis=1)
        ens["ens_max"] = np.nanmax(vals, axis=1)
        ens["ens_median"] = np.nanmedian(vals, axis=1)
        ens["ens_iqr"] = (np.nanpercentile(vals, 75, axis=1)
                          - np.nanpercentile(vals, 25, axis=1))
    for c in CENTRES:
        ens[f"c_{c}"] = piv[c]
    ens = ens.reset_index().rename(columns={"target_date": "date"})
    ens["d"] = pd.to_datetime(ens["date"])

    # ---- target actual label + own lag features ----
    ac_g = _daily_grid(ac, "station_id", "local_date", ["tmax_celsius"])
    ac_g = ac_g.rename(columns={"tmax_celsius": "act"})
    ac_g = ac_g.sort_values(["station_id", "d"])
    grp = ac_g.groupby("station_id")["act"]
    ac_g["act_D1"] = grp.shift(1)
    ac_g["act_D2"] = grp.shift(2)
    roll = grp.transform(lambda s: s.rolling(30, min_periods=10).mean().shift(1))
    ac_g["_act_anom"] = ac_g["act"] - roll
    ac_g["tgt_anom_D1"] = ac_g.groupby("station_id")["_act_anom"].shift(1)
    ac_g["tgt_anom_D2"] = ac_g.groupby("station_id")["_act_anom"].shift(2)

    # ---- neighbor ring: per-point value/anomaly at D-1, D-2 + gradient ----
    npiv = nb.pivot_table(index=["station_id", "date"], columns="point_label",
                          values="tmax_c", aggfunc="last").reset_index()
    npiv = _daily_grid(npiv, "station_id", "date", POINTS)
    npiv = npiv.sort_values(["station_id", "d"])
    gb = npiv.groupby("station_id")
    feat_nb = npiv[["station_id", "d"]].copy()
    for p in POINTS:
        v = npiv[p]
        roll_p = gb[p].transform(lambda s: s.rolling(30, min_periods=10).mean().shift(1))
        anom = v - roll_p
        feat_nb[f"nb_{p}_D1"] = gb[p].shift(1)     # value at D-1
        feat_nb[f"nb_{p}_D2"] = gb[p].shift(2)
        feat_nb[f"nbanom_{p}_D1"] = anom.groupby(npiv["station_id"]).shift(1)
        feat_nb[f"nbanom_{p}_D2"] = anom.groupby(npiv["station_id"]).shift(2)
    # gradient D-1: dir minus target
    for p in DIRS:
        feat_nb[f"grad_{p}_D1"] = feat_nb[f"nb_{p}_D1"] - feat_nb["nb_TGT_D1"]
    # D-5 ablation values (live-feasible-with-ERA5 lower bound)
    for p in POINTS:
        feat_nb[f"nb_{p}_D5"] = gb[p].shift(5)

    # ---- assemble base frame keyed (station, date=target_date) ----
    df = ens.merge(ac_g[["station_id", "d", "act", "act_D1", "act_D2",
                         "tgt_anom_D1", "tgt_anom_D2"]],
                   on=["station_id", "d"], how="left")
    df = df.merge(feat_nb, on=["station_id", "d"], how="left")

    # ---- trailing 7d ensemble-mean bias vs actual (target station) ----
    df = df.sort_values(["station_id", "d"])
    df["_bias"] = df["ens_mean"] - df["act"]
    df["bias7"] = df.groupby("station_id")["_bias"].transform(
        lambda s: s.rolling(7, min_periods=3).mean().shift(1))

    # ---- day-of-year cyclic ----
    doy = df["d"].dt.dayofyear.to_numpy()
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)

    df = df.drop(columns=["_bias"])
    df["station_id"] = df["station_id"].astype("category")
    return df


# feature column groups
def feature_cols(df, *, neighbors=True, era5_lag="D1"):
    ens_c = ["ens_mean", "ens_std", "ens_min", "ens_max", "ens_median", "ens_iqr"]
    ens_c += [f"c_{c}" for c in CENTRES]
    own_c = ["act_D1", "act_D2", "tgt_anom_D1", "tgt_anom_D2", "bias7"]
    doy_c = ["doy_sin", "doy_cos"]
    cat_c = ["station_id"]
    base = ens_c + own_c + doy_c + cat_c
    if not neighbors:
        return base
    nb_c = []
    if era5_lag == "D1":
        for p in POINTS:
            nb_c += [f"nb_{p}_D1", f"nb_{p}_D2", f"nbanom_{p}_D1", f"nbanom_{p}_D2"]
        nb_c += [f"grad_{p}_D1" for p in DIRS]
    else:  # D5-only (live-feasible-with-ERA5)
        for p in POINTS:
            nb_c += [f"nb_{p}_D5"]
    return base + nb_c


# --------------------------------------------------------------------------- models
_CAT = None
def _cat_dtype():
    """Fixed 46-station CategoricalDtype so train/test encodings always match
    (a pandas merge silently coerces category->object, which LightGBM rejects)."""
    global _CAT
    if _CAT is None:
        s = pd.read_csv(MLDS / "stations.csv")["station_id"].astype(str).tolist()
        _CAT = pd.CategoricalDtype(categories=sorted(s))
    return _CAT


def _prep(X):
    X = X.copy()
    if "station_id" in X.columns:
        X["station_id"] = X["station_id"].astype(str).astype(_cat_dtype())
    return X


def _lgbm(**kw):
    import lightgbm as lgb
    params = dict(n_estimators=250, learning_rate=0.05, num_leaves=15,
                  min_child_samples=30, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, reg_lambda=1.0, random_state=0,
                  n_jobs=-1, verbose=-1)
    params.update(kw)
    return lgb.LGBMRegressor(**params)


def fit_predict_gaussian(train, test, cols, label="act"):
    """M1: GBM mean + heteroscedastic GBM sigma (OOF-residual trained). Returns
    (mu, sigma) arrays aligned to `test`."""
    from sklearn.model_selection import KFold
    tr = train.dropna(subset=[label])
    Xtr, ytr = _prep(tr[cols]), tr[label].to_numpy(dtype=float)
    Xte = _prep(test[cols])
    # OOF residuals for an honest sigma
    oof = np.zeros(len(tr))
    kf = KFold(n_splits=4, shuffle=True, random_state=0)
    for a, b in kf.split(Xtr):
        m = _lgbm().fit(Xtr.iloc[a], ytr[a])
        oof[b] = m.predict(Xtr.iloc[b])
    abs_res = np.abs(ytr - oof)
    mean_model = _lgbm().fit(Xtr, ytr)
    # sigma model on abs residual; sigma = E|r| * sqrt(pi/2)
    sig_model = _lgbm(n_estimators=150).fit(Xtr, np.log1p(abs_res))
    mu = mean_model.predict(Xte)
    sig = np.expm1(sig_model.predict(Xte)) * np.sqrt(np.pi / 2.0)
    floor = max(0.8, float(np.nanpercentile(abs_res, 15)) * np.sqrt(np.pi / 2.0))
    sig = np.clip(sig, floor, 25.0)
    return mu, sig


QLEVELS = np.array([0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50,
                    0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98])


def fit_predict_quantiles(train, test, cols, label="act"):
    """M2: monotonized quantile GBMs. Returns preds array (n_test, n_q)."""
    tr = train.dropna(subset=[label])
    Xtr, ytr = _prep(tr[cols]), tr[label].to_numpy(dtype=float)
    Xte = _prep(test[cols])
    preds = np.zeros((len(test), len(QLEVELS)))
    for j, q in enumerate(QLEVELS):
        m = _lgbm(objective="quantile", alpha=float(q), n_estimators=200)
        m.fit(Xtr, ytr)
        preds[:, j] = m.predict(Xte)
    preds = np.maximum.accumulate(preds, axis=1)  # monotonize
    return preds


# ---- bracket probabilities from a predictive distribution ----
def gauss_cdf(x, mu, sig):
    from scipy.special import ndtr
    return ndtr((x - mu) / sig)


def bracket_prob_gauss(lo, hi, mu, sig):
    lo = np.where(np.isneginf(lo), -1e9, lo)
    hi = np.where(np.isposinf(hi), 1e9, hi)
    return np.clip(gauss_cdf(hi, mu, sig) - gauss_cdf(lo, mu, sig), 1e-9, 1 - 1e-9)


def _qcdf_row(x, qpred, qlev):
    """CDF at x for one row's monotone quantile predictions (linear interp +
    linear tail extrapolation on end slope)."""
    if x <= qpred[0]:
        s = (qlev[1] - qlev[0]) / max(qpred[1] - qpred[0], 1e-6)
        return max(0.0, qlev[0] + (x - qpred[0]) * s)
    if x >= qpred[-1]:
        s = (qlev[-1] - qlev[-2]) / max(qpred[-1] - qpred[-2], 1e-6)
        return min(1.0, qlev[-1] + (x - qpred[-1]) * s)
    return float(np.interp(x, qpred, qlev))


def bracket_prob_quant(lo, hi, qpreds):
    out = np.empty(len(lo))
    for i in range(len(lo)):
        loi = -1e9 if np.isneginf(lo[i]) else lo[i]
        hii = 1e9 if np.isposinf(hi[i]) else hi[i]
        qp = qpreds[i]
        out[i] = _qcdf_row(hii, qp, QLEVELS) - _qcdf_row(loi, qp, QLEVELS)
    return np.clip(out, 1e-9, 1 - 1e-9)


# --------------------------------------------------------------------------- p_ML for decision table
def compute_pml_for_decision(feat, model="M1", neighbors=True, era5_lag="D1"):
    """Return DataFrame(row_idx-> p_ML) for every decision-table row, leak-free: each
    row uses a model trained on target_dates strictly before its ABCD window start."""
    dec = pd.read_parquet(DECISION, columns=["station_id", "market_date",
                                             "bracket_index", "lo_c", "hi_c"])
    dec = dec.reset_index().rename(columns={"index": "row_idx"})
    dec["d"] = pd.to_datetime(dec["market_date"])
    cols = feature_cols(feat, neighbors=neighbors, era5_lag=era5_lag)

    pml = np.full(len(dec), np.nan)
    # assign each row a window by market_date
    for wlab, wstart in WINDOW_STARTS.items():
        # rows whose window this is
        if wlab == "A":
            row_mask = dec["market_date"] < WINDOW_STARTS["B"]
        elif wlab == "B":
            row_mask = (dec["market_date"] >= WINDOW_STARTS["B"]) & (dec["market_date"] < WINDOW_STARTS["C"])
        elif wlab == "C":
            row_mask = (dec["market_date"] >= WINDOW_STARTS["C"]) & (dec["market_date"] < WINDOW_STARTS["D"])
        else:
            row_mask = dec["market_date"] >= WINDOW_STARTS["D"]
        if not row_mask.any():
            continue
        train = feat[feat["date"] < wstart]
        # station-days needed for this window
        need = dec.loc[row_mask, ["station_id", "market_date"]].drop_duplicates()
        test_sd = feat.merge(need, left_on=["station_id", "date"],
                             right_on=["station_id", "market_date"], how="inner")
        if len(test_sd) == 0:
            continue
        if model in ("M1", "M3"):
            mu, sig = fit_predict_gaussian(train, test_sd, cols)
            test_sd = test_sd.assign(_mu=mu, _sig=sig)
        else:  # M2
            qp = fit_predict_quantiles(train, test_sd, cols)
        # map station-day -> prediction, then to bracket rows
        sd_key = (test_sd["station_id"].astype(str) + "|" + test_sd["date"].astype(str)).to_numpy()
        rows = dec[row_mask]
        rkey = (rows["station_id"].astype(str) + "|" + rows["market_date"].astype(str)).to_numpy()
        idx_map = {k: i for i, k in enumerate(sd_key)}
        sel = np.array([idx_map.get(k, -1) for k in rkey])
        ok = sel >= 0
        lo = rows["lo_c"].to_numpy(dtype=float)
        hi = rows["hi_c"].to_numpy(dtype=float)
        p = np.full(len(rows), np.nan)
        if model in ("M1", "M3"):
            mu_r = np.where(ok, test_sd["_mu"].to_numpy()[np.clip(sel, 0, None)], np.nan)
            sig_r = np.where(ok, test_sd["_sig"].to_numpy()[np.clip(sel, 0, None)], np.nan)
            p[ok] = bracket_prob_gauss(lo[ok], hi[ok], mu_r[ok], sig_r[ok])
        else:
            qp_r = qp[np.clip(sel, 0, None)]
            p[ok] = bracket_prob_quant(lo[ok], hi[ok], qp_r[ok])
        pml[rows.index.to_numpy()] = p
    dec["p_ML"] = pml
    return dec[["row_idx", "station_id", "market_date", "bracket_index", "p_ML"]]


# --------------------------------------------------------------------------- trading eval
def run_trading_variant(pml_df, name):
    """Swap p_E := p_ML, p_B_50 := p_ML, run the identical blend/0.04/both/cap
    protocol. Returns variant_row dict."""
    df = pd.read_parquet(DECISION)
    p = pml_df.set_index("row_idx")["p_ML"].reindex(range(len(df))).to_numpy()
    df = df.copy()
    df["p_E"] = np.where(np.isfinite(p), p, df["p_E"].to_numpy())  # fallback keeps row testable
    df["p_B_50"] = df["p_E"]
    ctx = S.run(df)
    ev = ctx["eval_variant"]
    pw, oos = ev("blend", 0.04, True, "both")
    r = S.variant_row(name, "blend", 0.04, True, "both", pw, oos, ctx["n_oos_days"])
    r["_ctx"] = ctx
    return r, ctx


def print_trade_row(r):
    print(f"  {r['name']:<24} n={r['n']:>4} /day={r['bets_per_day']:>5.2f} "
          f"win={r['win_rate']*100:>5.1f}%±{r['win_se']*100:>3.1f} "
          f"gap={r['rel_gap_pp']:>+5.1f}pp ROS={r['ros_pct']:>+6.2f}% "
          f"PnL@10=${r['pnl_10']:>+8.2f}  B/C/D={r['B_pnl']:>+6.0f}/{r['C_pnl']:>+6.0f}/{r['D_pnl']:>+6.0f} "
          f"(n {r['B_n']}/{r['C_n']}/{r['D_n']})")


# --------------------------------------------------------------------------- fired-row tracking (for disagreement analysis + sanity)
def fired_rows(df, ctx, *, transform="blend", min_edge=0.04, cap_on=True, units="both"):
    """Row-level fired set for a variant. Mirrors S.run's arrays + gate exactly
    but keeps row identity so we can compare which bets ML vs EMOS take."""
    grp = S._group_arr(df["bracket_label"].to_numpy())
    md = df["market_date"].astype(str).to_numpy()
    raw_claimed = 1.0 - df["p_E"].to_numpy(dtype=float)
    price = df["no_price"].to_numpy(dtype=float)
    won_no = 1.0 - df["won_yes"].to_numpy(dtype=float)
    n_cum = df["n_cum"].to_numpy(dtype=float)
    vol = df["avg_volume"].to_numpy(dtype=float)
    bracket_high = (df["bracket_kind"].astype(str).to_numpy() == "high")
    pb50 = df["p_B_50"].to_numpy(dtype=float)
    raw_ceil = 1.0 - pb50
    pb50_ok = np.isfinite(pb50)
    fee = S._fee_arr(price)
    base_ok = ((grp != "") & np.isfinite(price) & (price > 0.0)
               & np.isfinite(n_cum) & (n_cum >= S.NO_MIN_N)
               & np.isfinite(vol) & (vol >= S.NO_MIN_VOL) & np.isfinite(raw_claimed))
    strict_cap = S.RAW_STRICT_CAP if cap_on else np.inf
    ceil_cap = S.RAW_CEIL_CAP if cap_on else np.inf
    recs = []
    for w in ctx["eval_windows"]:
        idx = w.idx
        f = ctx["fits"][w.label]
        rc, rcc, pr = raw_claimed[idx], raw_ceil[idx], price[idx]
        g_idx = grp[idx]
        cs, cc = rc.copy(), rcc.copy()
        for g in ("NO_F", "NO_C"):
            gm = g_idx == g
            if transform == "1d":
                cs[gm] = S._apply_iso_vec(f.iso[g], rc[gm]); cc[gm] = S._apply_iso_vec(f.iso[g], rcc[gm])
            elif transform == "blend":
                cs[gm] = S._apply_blend_vec(f.blend[g], rc[gm], pr[gm]); cc[gm] = S._apply_blend_vec(f.blend[g], rcc[gm], pr[gm])
        fired, claimed_used = S.fire_mask(
            transform=transform, min_edge=min_edge, strict_cap=strict_cap, ceil_cap=ceil_cap,
            units=units, base_ok=base_ok[idx], price=pr, fee=fee[idx], grp=g_idx,
            bracket_high=bracket_high[idx], pb50_ok=pb50_ok[idx], claimed_strict=cs, claimed_ceil=cc)
        pnl = S._pnl_arr(won_no[idx], pr)
        for j in np.where(fired)[0]:
            gi = idx[j]
            recs.append(dict(window=w.label, row_idx=int(gi),
                             station_id=str(df["station_id"].to_numpy()[gi]),
                             market_date=md[gi], bracket_index=int(df["bracket_index"].to_numpy()[gi]),
                             bracket_unit=("F" if g_idx[j] == "NO_F" else "C"),
                             claimed=float(claimed_used[j]), price=float(pr[j]),
                             won=bool(won_no[gi] >= 0.5), pnl=float(pnl[j])))
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------- forecast metrics (a)
def crps_gauss(mu, sig, y):
    from scipy.special import ndtr
    z = (y - mu) / sig
    phi = np.exp(-0.5 * z * z) / np.sqrt(2 * np.pi)
    return sig * (z * (2 * ndtr(z) - 1) + 2 * phi - 1.0 / np.sqrt(np.pi))


def crps_quant(qpreds, y):
    # CRPS ~ 2 * mean_k pinball(tau_k). qpreds (n,nq)
    out = np.zeros(len(y))
    for j, tau in enumerate(QLEVELS):
        d = y - qpreds[:, j]
        out += np.where(d >= 0, tau * d, (tau - 1) * d)
    return 2.0 * out / len(QLEVELS)


def forecast_eval(feat):
    stations = pd.read_csv(MLDS / "stations.csv")[["station_id", "unit"]]
    unit_map = dict(zip(stations.station_id, stations.unit))
    feat = feat.copy()
    feat["ym"] = feat["d"].dt.to_period("M").astype(str)
    cols = feature_cols(feat, neighbors=True, era5_lag="D1")
    cols_noN = feature_cols(feat, neighbors=False)
    months = sorted(m for m in feat["ym"].unique() if m >= "2026-03")  # need >=1 prior month
    rows = []
    for m in months:
        train = feat[feat["ym"] < m]
        test = feat[(feat["ym"] == m)].dropna(subset=["act"]).copy()
        if len(train.dropna(subset=["act"])) < 100 or len(test) == 0:
            continue
        y = test["act"].to_numpy(dtype=float)
        # M1
        mu, sig = fit_predict_gaussian(train, test, cols)
        # M3 (no neighbors)
        mu3, sig3 = fit_predict_gaussian(train, test, cols_noN)
        # M2
        qp = fit_predict_quantiles(train, test, cols)
        med2 = qp[:, np.where(QLEVELS == 0.50)[0][0]]
        # control: raw ensemble mean + ensemble std (floored)
        muC = test["ens_mean"].to_numpy(dtype=float)
        sigC = np.clip(test["ens_std"].to_numpy(dtype=float), 0.8, 25.0)
        for i in range(len(test)):
            rows.append(dict(
                station_id=str(test["station_id"].iloc[i]), month=m,
                unit=unit_map.get(str(test["station_id"].iloc[i]), "?"), y=y[i],
                ae_M1=abs(mu[i] - y[i]), ae_M2=abs(med2[i] - y[i]),
                ae_M3=abs(mu3[i] - y[i]), ae_C=abs(muC[i] - y[i]),
                crps_M1=crps_gauss(mu[i], sig[i], y[i]),
                crps_M3=crps_gauss(mu3[i], sig3[i], y[i]),
                crps_C=crps_gauss(muC[i], sigC[i], y[i]),
                crps_M2=crps_quant(qp[i:i+1], y[i:i+1])[0]))
    return pd.DataFrame(rows)


def _mean_se(x):
    x = np.asarray(x, dtype=float)
    return x.mean(), x.std(ddof=1) / np.sqrt(len(x))


def _paired(a, b):
    """mean(a-b), SE, and whether |diff|>2SE (a<b favors a for loss metrics)."""
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    m, se = d.mean(), d.std(ddof=1) / np.sqrt(len(d))
    return m, se, (abs(m) > 2 * se)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all")
    args = ap.parse_args()
    fp = MLDS / "features.parquet"
    if args.stage in ("features",) or not fp.exists():
        print("building features...")
        feat = build_features()
        feat.to_parquet(fp, index=False)
        print(f"  wrote {fp}  shape={feat.shape}")
        if args.stage == "features":
            sys.exit(0)
    feat = pd.read_parquet(fp)
    feat["date"] = feat["date"].astype(str)
    feat["d"] = pd.to_datetime(feat["d"])
    feat["station_id"] = feat["station_id"].astype("category")

    df0 = pd.read_parquet(DECISION)

    # ============================ TRADING EVAL (b) ============================
    if args.stage in ("all", "trade"):
        print("\n" + "=" * 96)
        print("DECISIVE TABLE — walk-forward trading (blend / min_edge 0.04 / cap 0.15 / both units / $10)")
        print("=" * 96)
        # CONTROL (untouched p_E)
        ctxC = S.run(df0.copy())
        pwC, oosC = ctxC["eval_variant"]("blend", 0.04, True, "both")
        rC = S.variant_row("CONTROL EMOS p_E", "blend", 0.04, True, "both", pwC, oosC, ctxC["n_oos_days"])
        print_trade_row(rC)
        variants = {"CONTROL": (rC, ctxC, df0.copy())}
        specs = [("M1", dict(model="M1", neighbors=True, era5_lag="D1")),
                 ("M2", dict(model="M2", neighbors=True, era5_lag="D1")),
                 ("M3_noNbr", dict(model="M3", neighbors=False, era5_lag="D1")),
                 ("M1_D5nbr", dict(model="M1", neighbors=True, era5_lag="D5"))]
        for nm, kw in specs:
            cache = MLDS / f"probs_{nm}.parquet"
            if cache.exists():
                pml = pd.read_parquet(cache)
            else:
                print(f"  ... computing p_ML for {nm}")
                pml = compute_pml_for_decision(feat, **kw)
                pml.to_parquet(cache, index=False)
            r, ctx = run_trading_variant(pml, f"{nm}")
            # build df with swapped probs for fired-row tracking
            dfv = df0.copy()
            p = pml.set_index("row_idx")["p_ML"].reindex(range(len(df0))).to_numpy()
            dfv["p_E"] = np.where(np.isfinite(p), p, dfv["p_E"].to_numpy())
            dfv["p_B_50"] = dfv["p_E"]
            print_trade_row(r)
            variants[nm] = (r, ctx, dfv)

        print("\nPer-window PnL@$10 (B / C / D), positive windows out of 3:")
        for nm, (r, _, _) in variants.items():
            pos = sum(1 for w in ("B", "C", "D") if r[f"{w}_ros"] > 0)
            print(f"  {r['name']:<24} PnL {r['pnl_10']:>+8.2f}  ROS {r['ros_pct']:>+6.2f}%  "
                  f"B/C/D ${r['B_pnl']:>+6.0f}/${r['C_pnl']:>+6.0f}/${r['D_pnl']:>+6.0f}  {pos}/3 pos")

        # ---- disagreement analysis (c): CONTROL vs M1 fired slices ----
        print("\n" + "-" * 96)
        print("DISAGREEMENT (c): fired-slice overlap CONTROL(EMOS) vs M1")
        fc = fired_rows(variants["CONTROL"][2], variants["CONTROL"][1])
        fm = fired_rows(variants["M1"][2], variants["M1"][1])
        keyC = set(zip(fc.row_idx))
        keyM = set(zip(fm.row_idx))
        both = keyC & keyM
        onlyC = keyC - keyM
        onlyM = keyM - keyC
        def _slice(fr, keys):
            s = fr[fr.row_idx.isin([k[0] for k in keys])]
            return len(s), (s.won.mean()*100 if len(s) else float('nan')), s.pnl.sum()
        for lbl, ks in [("both fire", both), ("EMOS-only", onlyC), ("M1-only", onlyM)]:
            frref = fc if lbl != "M1-only" else fm
            n, wr, pnl = _slice(frref, ks)
            print(f"  {lbl:<12} n={n:>4}  win={wr:>5.1f}%  PnL@10=${pnl:>+8.2f}")
        print(f"  (CONTROL fired {len(fc)}, M1 fired {len(fm)}; sanity: variant_row n "
              f"C={variants['CONTROL'][0]['n']} M1={variants['M1'][0]['n']})")

        # persist trade summary
        summ = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith('_')}
                             for r, _, _ in variants.values()])
        summ.to_parquet(MLDS / "trade_summary.parquet", index=False)

    # ============================ FORECAST EVAL (a) ============================
    if args.stage in ("all", "forecast"):
        print("\n" + "=" * 96)
        print("FORECAST QUALITY (a) — expanding monthly refit, ML vs EMOS-proxy(raw ensemble)")
        print("=" * 96)
        fcache = MLDS / "forecast_eval.parquet"
        if fcache.exists():
            fe = pd.read_parquet(fcache)
        else:
            fe = forecast_eval(feat)
            fe.to_parquet(fcache, index=False)
        print(f"n station-days scored = {len(fe)}   (months {sorted(fe.month.unique())})")
        def report_split(name, sub):
            if len(sub) == 0:
                return
            print(f"\n  [{name}] n={len(sub)}")
            for met, cols in [("MAE(°C)", ["ae_C", "ae_M1", "ae_M2", "ae_M3"]),
                              ("CRPS(°C)", ["crps_C", "crps_M1", "crps_M2", "crps_M3"])]:
                parts = []
                for c in cols:
                    mu, se = _mean_se(sub[c])
                    parts.append(f"{c.split('_')[1] if '_' in c else c}={mu:.3f}±{se:.3f}")
                print(f"    {met:<9} " + "  ".join(parts))
            # paired ML vs control
            for c, cc, lab in [("ae_M1", "ae_C", "MAE M1-C"), ("crps_M1", "crps_C", "CRPS M1-C"),
                               ("crps_M1", "crps_M3", "CRPS M1-M3(nbr)")]:
                m, se, sig = _paired(sub[c], sub[cc])
                print(f"    Δ{lab:<15} {m:>+.4f} ± {se:.4f}  {'SIGNIF' if sig else 'ns'}")
        report_split("ALL", fe)
        for u in ["F", "C"]:
            report_split(f"unit={u}", fe[fe.unit == u])
        print("\n  Per-month MAE (M1 vs Control):")
        for m in sorted(fe.month.unique()):
            s = fe[fe.month == m]
            print(f"    {m}: n={len(s):>4}  MAE_C={s.ae_C.mean():.3f}  MAE_M1={s.ae_M1.mean():.3f}  "
                  f"MAE_M2={s.ae_M2.mean():.3f}  MAE_M3={s.ae_M3.mean():.3f}")

        # bracket-boundary reliability: p (YES bracket prob) vs won_yes, EMOS vs M1
        print("\n  Bracket-prob reliability (predicted YES vs realized, all decision rows):")
        pmlM1 = pd.read_parquet(MLDS / "probs_M1.parquet").set_index("row_idx")["p_ML"]
        dd = df0.copy()
        dd["p_M1"] = pmlM1.reindex(range(len(dd))).to_numpy()
        bins = [0, 0.01, 0.05, 0.15, 0.35, 0.65, 0.85, 0.95, 0.99, 1.0]
        for lab, col in [("EMOS p_E", "p_E"), ("M1 p_ML", "p_M1")]:
            print(f"    {lab}:")
            v = dd.dropna(subset=[col])
            v = v.assign(b=pd.cut(v[col], bins, include_lowest=True))
            for bkt, g in v.groupby("b", observed=True):
                if len(g) < 20:
                    continue
                print(f"      p∈[{bkt.left:.2f},{bkt.right:.2f}] n={len(g):>5} "
                      f"pred={g[col].mean()*100:>5.1f}% real={g.won_yes.mean()*100:>5.1f}%")
    print("\ndone")
