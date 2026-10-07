"""EMOS (Ensemble Model Output Statistics) — ensemble dressing.

Fits a non-homogeneous Gaussian regression per (station, horizon):
  mu    = a + b * ensemble_mean
  sigma = sqrt(exp(c) + exp(d) * ensemble_variance)

Parameters {a, b, c, d} minimise mean CRPS over training pairs.
The exp() transform enforces sigma² > 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from properscoring import crps_gaussian
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

# Minimum sigma to prevent numerical issues
_SIGMA_FLOOR = 0.1  # °C


@dataclass
class EMOSParams:
    a: float
    b: float
    c: float  # log-space: variance = exp(c) + exp(d) * ens_var
    d: float
    n_samples: int


def _crps_loss(params: np.ndarray, ens_means: np.ndarray, ens_vars: np.ndarray, actuals: np.ndarray) -> float:
    """Mean CRPS for the Gaussian predictive distribution."""
    a, b, c, d = params
    mu = a + b * ens_means
    sigma2 = np.exp(c) + np.exp(d) * ens_vars
    sigma = np.sqrt(np.maximum(sigma2, _SIGMA_FLOOR ** 2))
    return np.mean(crps_gaussian(actuals, mu=mu, sig=sigma))


def fit_emos(
    ensemble_matrix: np.ndarray,
    actuals: np.ndarray,
) -> EMOSParams | None:
    """Fit EMOS parameters from (n_days, n_members) ensemble and (n_days,) actuals.

    Returns EMOSParams or None if optimisation fails.
    """
    n_days, n_members = ensemble_matrix.shape

    if n_days < 10:
        logger.warning("Too few samples for EMOS: %d", n_days)
        return None

    ens_means = ensemble_matrix.mean(axis=1)
    # ddof=0: population variance — ensemble members are the full forecast
    # distribution sample, not a sample from an unknown population (Gneiting 2005)
    ens_vars = ensemble_matrix.var(axis=1, ddof=0)

    # Initial guess: identity regression + moderate spread
    x0 = np.array([0.0, 1.0, 0.0, 0.0])

    # Bounds: a, b unconstrained; c, d bounded to prevent exp() overflow
    # exp(10) ≈ 22000 — more than enough variance for temperature in °C
    bounds = [(None, None), (None, None), (-10, 10), (-10, 10)]

    result = minimize(
        _crps_loss,
        x0,
        args=(ens_means, ens_vars, actuals),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 500},
    )

    if not result.success:
        logger.error("EMOS optimisation did not converge: %s", result.message)
        # Still return the result — partial convergence is usually usable

    # Degenerate-fit alarm: compare the FITTED sigma against the floor, not
    # the log-space parameter `d` (which is on a different scale and can never
    # equal _SIGMA_FLOOR). When the per-row sigma collapses to the floor for
    # every training pair, the predictive distribution is over-confident and
    # downstream tail probabilities will be misleading.
    a, b, c, d = result.x
    sigma2_fitted = np.exp(c) + np.exp(d) * ens_vars
    sigma_fitted = np.sqrt(np.maximum(sigma2_fitted, _SIGMA_FLOOR ** 2))
    if np.all(sigma_fitted <= _SIGMA_FLOOR + 1e-6):
        logger.critical(
            "EMOS sigma stuck at _SIGMA_FLOOR (%.3f) for ALL %d training pairs — "
            "calibration is degenerate; downstream probabilities will be over-confident",
            _SIGMA_FLOOR, n_days,
        )
    elif np.any(sigma_fitted <= _SIGMA_FLOOR + 1e-6):
        n_at_floor = int(np.sum(sigma_fitted <= _SIGMA_FLOOR + 1e-6))
        logger.warning(
            "EMOS sigma at _SIGMA_FLOOR for %d/%d training pairs — partial degenerate fit",
            n_at_floor, n_days,
        )

    return EMOSParams(a=a, b=b, c=c, d=d, n_samples=n_days)


def predict_emos(
    params: EMOSParams,
    ensemble_members: np.ndarray,
) -> tuple[float, float]:
    """Predict (mu, sigma) from EMOS params and a single day's ensemble.

    Parameters
    ----------
    params : fitted EMOSParams
    ensemble_members : array of shape (n_members,)

    Returns
    -------
    (mu, sigma) of the predictive Gaussian in °C
    """
    ens_mean = ensemble_members.mean()
    ens_var = ensemble_members.var(ddof=0) if len(ensemble_members) > 1 else 0.0

    mu = params.a + params.b * ens_mean
    sigma2 = np.exp(params.c) + np.exp(params.d) * ens_var
    sigma = float(np.sqrt(max(sigma2, _SIGMA_FLOOR ** 2)))

    return float(mu), sigma


def emos_probability(
    params: EMOSParams,
    ensemble_members: np.ndarray,
    threshold: float,
) -> float:
    """P(tmax > threshold) from the EMOS Gaussian.

    Returns a probability in [0, 1].
    """
    from scipy.stats import norm

    mu, sigma = predict_emos(params, ensemble_members)
    # P(X > threshold) = 1 - Φ((threshold - mu) / sigma)
    return float(1.0 - norm.cdf((threshold - mu) / sigma))
