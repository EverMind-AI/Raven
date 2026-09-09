#!/usr/bin/env python3
"""2D heat equation on the unit square, explicit forward-Euler in time.

    u_t = alpha * (u_xx + u_yy),  u = 0 on the boundary

Started from u(x,y,0) = sin(pi x) sin(pi y), which the equation carries forward
exactly as u = exp(-2 alpha pi^2 t) sin(pi x) sin(pi y). The whole point of a
known answer is that the error at the end is a number, not an impression.

Usage:  heat2d.py <nx> <alpha> <dt> <t_end>

  nx      grid points per side, including both boundaries
  alpha   diffusivity
  dt      time step
  t_end   how far to integrate

Writes result.json in the current directory. Reads nothing else.
"""

import json
import math
import sys
import time

import numpy as np

nx = int(sys.argv[1])
alpha = float(sys.argv[2])
dt = float(sys.argv[3])
t_end = float(sys.argv[4])

dx = 1.0 / (nx - 1)
nsteps = int(round(t_end / dt))
xs = np.linspace(0.0, 1.0, nx)
X, Y = np.meshgrid(xs, xs, indexing="ij")
u = np.sin(math.pi * X) * np.sin(math.pi * Y)

started = time.time()
c = alpha * dt / (dx * dx)
for step in range(nsteps):
    lap = u[:-2, 1:-1] + u[2:, 1:-1] + u[1:-1, :-2] + u[1:-1, 2:] - 4.0 * u[1:-1, 1:-1]
    u[1:-1, 1:-1] += c * lap
wall = time.time() - started

exact = math.exp(-2.0 * alpha * math.pi**2 * t_end) * np.sin(math.pi * X) * np.sin(math.pi * Y)
diff = u - exact
l2 = float(np.sqrt(np.mean(diff * diff)))
peak = float(np.max(np.abs(u)))

json.dump(
    {
        "status": "succeeded",
        "rc": 0,
        "nx": nx,
        "dx": dx,
        "dt": dt,
        "alpha": alpha,
        "t_end": t_end,
        "steps": nsteps,
        "wall_seconds": round(wall, 2),
        "gpu_minutes_used": round(wall / 60.0, 4),
        # The pair the ledger reads its metric from. Written whatever the number
        # turned out to be: a run whose error is not finite is a reading, and
        # dropping it would show up as "this trial reported no metric", which is
        # what never having looked also looks like.
        "eval_points": [[nsteps, l2]],
        "l2_error": l2 if math.isfinite(l2) else None,
        "l2_error_raw": repr(l2),
        "peak_abs_u": peak if math.isfinite(peak) else None,
        "peak_abs_u_raw": repr(peak),
    },
    open("result.json", "w"),
    indent=2,
)
print(f"[job] nx={nx} dt={dt} steps={nsteps} wall={wall:.1f}s l2={l2!r} peak={peak!r}")
