"""Exact weighted closest-lattice-point optimization.

Every integer solution of ``A x = b`` found by :mod:`app.diophantine` has
the form ``x = x0 + sum_j z_j * h_j`` where ``x0`` is a particular integer
solution, ``h_j`` are the columns of an integer basis of the homogeneous
solution lattice (built by exact gcd syzygies) and ``z`` ranges over all
integer vectors.  Choosing the correction closest to the engineer's
preferred shim counts is therefore a *closest vector problem* on that
lattice under the weighted inner product ``<u,v>_W = u^T diag(w) v``::

    minimize  F(z) = sum_i w_i (x_i - preferred_i)^2,  z in Z^k.

The search is exact, end to end:

* the homogeneous basis is LLL reduced under the weighted inner product.
  LLL is a performance preprocessing step only: column operations are
  always exact integers and the coordinate transform is unimodular.
  Small/normal inputs use exact rational LLL; pathological huge-entry
  bases use adaptive-precision decisions (80..8192 decimal digits).  No
  approximate value ever enters the closest-point search itself;
* an initial, *provably feasible* squared-distance bound is taken from an
  exact Babai nearest-plane point (never an arbitrary radius);
* a Fincke-Pohst style depth-first enumeration then visits every integer
  coordinate vector whose weighted squared distance cannot exceed that
  bound.  All level intervals and comparisons are evaluated on plain
  arbitrary-precision integers under one common rational denominator
  (integer square roots only); there is no floating-point distance, no
  coordinate-by-coordinate greedy decision and no hand-set search radius;
* ties at the minimum are resolved stably by the lexicographically
  smallest adjustment vector in variable-identification order.  (Because
  preferences are fixed per coordinate, comparing correction vectors or
  deviation vectors lexicographically gives the same result.)
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from math import gcd as _gcd, isqrt
from typing import List, Sequence

# Products of large coefficients may exceed the default str(int) digit cap;
# the optimizer is explicitly arbitrary-precision, so lift it (idempotent).
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

Vector = List[int]

# The bounded-precision LLL fallback temporarily changes the *global*
# Decimal context precision; serialize such reductions across threads.
_lll_decimal_lock = threading.Lock()


@dataclass
class OptimizationResult:
    """Exact weighted-CVP result with independently re-checkable evidence."""

    optimal: Vector
    deviations: Vector  # optimal - preferred, per variable (signed)
    weighted_terms: List[int]  # weight_i * deviation_i^2
    cost: int  # sum of weighted_terms
    tie_count: int  # number of lattice points attaining ``cost``

    particular: Vector
    preferred: Vector
    weights: Vector

    # LLL-reduced lattice (columns, in ambient variable coordinates) and
    # the unimodular coordinate transform C with reduced = original @ C,
    # i.e. original_coordinates = C @ reduced_coordinates.
    reduced_basis: List[Vector]
    transform: List[List[int]]
    gram: List[List[int]]  # weighted Gram matrix of the reduced basis

    # Exact rational Gram-Schmidt data under the weighted inner product:
    # mu[i][j] for j < i, squared norms gs_norm[i] = ||b*_i||_W^2.
    mu: List[List[Fraction]]
    gs_norm: List[Fraction]

    # Exact Babai nearest-plane point used as the proven initial bound.
    babai_coordinates: Vector
    babai_point: Vector
    babai_cost: int

    # Exact squared weighted length of the target component orthogonal to
    # span(lattice): dist²(z) = this constant + the lattice sum.
    orthogonal_residual: Fraction

    # Common integer denominator Q of mu / GS norms / target projections
    # used by the exact integer enumeration.
    common_denominator: int

    # Which arithmetic the LLL preprocessing used; enumeration arithmetic
    # is always exact.  ``"exact_rational"`` or the adaptive heuristic tag.
    lll_arithmetic: str

    # Integer coordinates of ``optimal`` in the *original* homogeneous
    # basis: optimal = particular + sum_s coordinates_s * basis[s].
    optimal_coordinates: Vector

    # Enumeration bookkeeping (re-producible optimality evidence).
    nodes_visited: int
    leaves_evaluated: int
    empty_intervals: int


def _nearest_integer(value: Fraction) -> int:
    """Nearest integer to an exact rational (ties rounded toward +inf)."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _weighted_inner(u: Sequence[int], v: Sequence[int], weights: Sequence[int]) -> int:
    return sum(weights[i] * u[i] * v[i] for i in range(len(u)))


def _gram_matrix(columns: Sequence[Sequence[int]], weights: Sequence[int]) -> List[List[int]]:
    k = len(columns)
    return [
        [_weighted_inner(columns[i], columns[j], weights) for j in range(k)]
        for i in range(k)
    ]


def _gram_schmidt(
    gram: Sequence[Sequence[int]], k: int
) -> tuple[List[List[Fraction]], List[Fraction]]:
    """Rational Gram-Schmidt coefficients and squared norms from a Gram matrix.

    ``mu[i][j] = <b_i, b*_j> / ||b*_j||^2`` (j < i); the ambient b*
    vectors are never needed because every projection factorizes through
    the (integer) Gram matrix.
    """
    mu = [[Fraction(0) for _ in range(k)] for _ in range(k)]
    norm = [Fraction(0) for _ in range(k)]
    for i in range(k):
        for j in range(i):
            numerator = Fraction(
                gram[i][j] - sum(
                    mu[i][ell] * mu[j][ell] * norm[ell] for ell in range(j)
                )
            )
            mu[i][j] = numerator / norm[j]
        norm[i] = Fraction(
            gram[i][i] - sum(mu[i][j] ** 2 * norm[j] for j in range(i))
        )
        if norm[i] <= 0:  # pragma: no cover - Smith columns are a basis
            raise ArithmeticError("齐次解基出现线性相关向量，无法构造格搜索")
    return mu, norm


def _lll_swap_update(mu, gs, i, k):
    """In-place local Gram-Schmidt update for swapping columns i-1 and i.

    Works for both exact Fractions and bounded-precision Decimals; the
    formula is the standard LLL swap update (the lattice vectors are
    swapped by the caller).  Returns the new coefficient mu'[i][i-1].
    """
    nu = mu[i][i - 1]
    for j in range(i - 1):
        mu[i - 1][j], mu[i][j] = mu[i][j], mu[i - 1][j]
    b_old = gs[i - 1]
    bbar = gs[i] + nu ** 2 * b_old
    new_mu = nu * b_old / bbar
    mu[i][i - 1] = new_mu
    gs[i - 1] = bbar
    gs[i] = b_old * gs[i] / bbar
    for ell in range(i + 1, k):
        p, q = mu[ell][i - 1], mu[ell][i]
        mu[ell][i] = p - nu * q
        mu[ell][i - 1] = q + new_mu * mu[ell][i]
    return new_mu


def _lll_run(columns, weights, delta, arithmetic, last_resort=False):
    """Core LLL loop.

    ``arithmetic`` is either ``"exact"`` (Fractions) or a Decimal context
    precision (``int``).  Returns ``(reduced, transform)`` or ``None`` if
    the bounded-precision run loses numerical reliability, in which case
    the caller retries at higher precision.
    """
    k = len(columns)
    reduced = [list(column) for column in columns]
    transform = [[1 if a == b else 0 for b in range(k)] for a in range(k)]

    if arithmetic == "exact":
        ctx = None
        number = Fraction
        half = Fraction(1, 2)
        d_delta = delta

        def nearest(value):
            return _nearest_integer(value)

        def finite_pos(value):
            return value > 0

        gram_nums = None
        mu, gs = _gram_schmidt(_gram_matrix(reduced, weights), k)
    else:
        from decimal import (
            ROUND_HALF_UP,
            Decimal,
            DivisionByZero,
            InvalidOperation,
            Overflow as DecimalOverflow,
            getcontext,
        )

        decimal_context = getcontext()
        previous_precision = decimal_context.prec
        decimal_context.prec = arithmetic
        ctx = decimal_context
        number = Decimal
        zero = Decimal(0)
        half = Decimal(1) / Decimal(2)
        d_delta = Decimal(delta.numerator) / Decimal(delta.denominator)
        noise = Decimal(1) / (Decimal(10) ** max(arithmetic // 3, 1))

        def nearest(value):
            return int(value.to_integral_value(rounding=ROUND_HALF_UP))

        def finite_pos(value):
            return value.is_finite() and value > 0

        # Scale the *vector entries* (not the Gram products): with
        # v' = v / 10^L every inner product is ~O(n * max_weight), which
        # stays far inside the fixed precision even for thousands-digit
        # inputs.  mu is scale-invariant and both sides of every Lovász
        # test carry the same factor, so the decisions use v' while the
        # actual columns remain exact integers.
        max_entry_digits = max(
            (len(str(abs(value))) for column in reduced for value in column),
            default=1,
        )
        entry_scale = Decimal(10) ** max_entry_digits

        def scaled_gram(cols):
            return [
                [
                    sum(
                        Decimal(cols[a][t])
                        * Decimal(cols[b][t])
                        * Decimal(weights[t])
                        for t in range(len(weights))
                    )
                    / (entry_scale * entry_scale)
                    for b in range(k)
                ]
                for a in range(k)
            ]

        gram_nums = scaled_gram(reduced)

        def recompute():
            try:
                mu0 = [[zero] * k for _ in range(k)]
                gs0 = [zero] * k
                for a in range(k):
                    for j in range(a):
                        numerator = gram_nums[a][j] - sum(
                            mu0[a][ell] * mu0[j][ell] * gs0[ell] for ell in range(j)
                        )
                        mu0[a][j] = numerator / gs0[j]
                    gs0[a] = gram_nums[a][a] - sum(
                        mu0[a][j] ** 2 * gs0[j] for j in range(a)
                    )
            except (DivisionByZero, InvalidOperation, DecimalOverflow):
                return None, None
            return mu0, gs0

        mu, gs = recompute()
        if mu is None:
            getcontext().prec = previous_precision
            if last_resort:
                return [list(c) for c in columns], [
                    [1 if a == b else 0 for b in range(k)] for a in range(k)
                ]
            return None

    if not all(finite_pos(value) for value in gs):
        if arithmetic != "exact":
            getcontext().prec = previous_precision
            if last_resort:
                return [list(c) for c in columns], [
                    [1 if a == b else 0 for b in range(k)] for a in range(k)
                ]
        return None

    i = 1
    swaps = 0
    failed = False
    swap_cap = 64 * k * k + 256 if arithmetic != "exact" else None
    try:
        while i < k:
            for j in range(i - 1, -1, -1):
                if abs(mu[i][j]) <= half:
                    continue
                q = nearest(mu[i][j])
                if q == 0:
                    continue
                qn = number(q) if arithmetic != "exact" else q
                reduced[i] = [
                    value - q * earlier
                    for value, earlier in zip(reduced[i], reduced[j])
                ]
                transform[i] = [
                    value - q * earlier
                    for value, earlier in zip(transform[i], transform[j])
                ]
                for ell in range(j):
                    mu[i][ell] -= qn * mu[j][ell]
                mu[i][j] -= qn

            nu = mu[i][i - 1]
            lhs = gs[i] + nu ** 2 * gs[i - 1]
            rhs = d_delta * gs[i - 1]
            if arithmetic != "exact" and not (lhs.is_finite() and rhs.is_finite()):
                if last_resort:
                    break  # keep the current exact-integer basis
                failed = True
                break
            if (
                arithmetic != "exact"
                and not last_resort
                and abs(lhs - rhs) <= noise * abs(rhs)
            ):
                failed = True  # ambiguous Lovász test: raise precision
                break
            if lhs >= rhs:
                i += 1
                continue

            reduced[i], reduced[i - 1] = reduced[i - 1], reduced[i]
            transform[i], transform[i - 1] = transform[i - 1], transform[i]
            swaps += 1
            if swap_cap is not None and swaps > swap_cap:
                # Still a valid integer basis of the same lattice; the
                # exact enumeration below proves optimality regardless.
                break
            if arithmetic == "exact":
                _lll_swap_update(mu, gs, i, k)
            else:
                # Local O(k) update; periodically (and whenever a rewritten
                # norm degrades) fully recompute from the exact columns so
                # bounded-precision drift cannot mislead later decisions.
                _lll_swap_update(mu, gs, i, k)
                if swaps % 64 == 0 or not (
                    finite_pos(gs[i - 1]) and finite_pos(gs[i])
                ):
                    gram_nums = scaled_gram(reduced)
                    mu, gs = recompute()
                    if mu is None or not all(finite_pos(v) for v in gs):
                        if last_resort:
                            break
                        failed = True
                        break
            i = max(i - 1, 1)
    finally:
        if arithmetic != "exact":
            getcontext().prec = previous_precision
    if failed:
        return None
    return reduced, transform


def _lll_reduce_exact(columns, weights, delta=Fraction(3, 4)):
    return _lll_run(columns, weights, delta, "exact")


def _lll_reduce_adaptive(columns, weights, delta=Fraction(3, 4)):
    """Adaptive-precision heuristic LLL with exact-integer column ops.

    Starts at 80 decimal digits and doubles precision whenever a Lovász
    decision falls inside the numerical noise band or a squared norm
    loses positivity.  The lattice columns and coordinate transform are
    always mutated by exact integer operations only, so the transform is
    unimodular and the lattice is unchanged.  Decision precision cannot
    affect correctness of the later exact enumeration, only its speed.
    """
    precision = 80
    # The run mutates the global Decimal context precision; hold the
    # module lock for the whole adaptive attempt chain.
    with _lll_decimal_lock:
        while precision <= 8192:
            result = _lll_run(columns, weights, delta, precision)
            if result is not None:
                return result
            precision *= 2
        return _lll_run(columns, weights, delta, 8192, last_resort=True)


def optimize_corrections(
    particular: Sequence[int],
    basis: Sequence[Sequence[int]],
    preferred: Sequence[int],
    weights: Sequence[int],
) -> OptimizationResult:
    """Minimize ``sum_i w_i (x_i - preferred_i)^2`` over every integer solution.

    ``particular`` is the particular solution ``x0`` and ``basis`` lists
    the homogeneous-solution basis vectors (each of the same length as
    ``particular``); ``weights`` must be strictly positive integers.
    """
    n = len(particular)
    if len(preferred) != n or len(weights) != n:
        raise ValueError("首选值与权重长度必须与变量数一致")
    if any(not isinstance(w, int) or w <= 0 for w in weights):
        raise ValueError("权重必须为正整数")
    if any(len(vector) != n for vector in basis):
        raise ValueError("齐次解基向量长度必须与变量数一致")

    preferred = list(preferred)
    weights = list(weights)
    particular = list(particular)
    k = len(basis)

    def compose(coordinates: Sequence[int]) -> Vector:
        x = list(particular)
        for j, coordinate in enumerate(coordinates):
            if coordinate:
                for i in range(n):
                    x[i] += coordinate * basis[j][i]
        return x

    def exact_cost(x: Sequence[int]) -> int:
        return sum(
            weights[i] * (x[i] - preferred[i]) ** 2 for i in range(n)
        )

    # Unique solution: the lattice has dimension zero.
    if k == 0:
        x = list(particular)
        deviations = [x[i] - preferred[i] for i in range(n)]
        terms = [weights[i] * deviations[i] ** 2 for i in range(n)]
        cost = sum(terms)
        return OptimizationResult(
            optimal=x,
            deviations=deviations,
            weighted_terms=terms,
            cost=cost,
            tie_count=1,
            particular=list(particular),
            preferred=preferred,
            weights=weights,
            reduced_basis=[],
            transform=[],
            gram=[],
            mu=[],
            gs_norm=[],
            babai_coordinates=[],
            babai_point=list(x),
            babai_cost=cost,
            orthogonal_residual=Fraction(cost),
            common_denominator=1,
            lll_arithmetic="exact_rational",
            optimal_coordinates=[],
            nodes_visited=0,
            leaves_evaluated=1,
            empty_intervals=0,
        )

    # LLL is a performance preprocessing step only; the exact enumeration
    # below proves optimality for *any* reduced basis.  Exact rational LLL
    # is used by default; only pathological knapsack-like bases whose
    # entries and rank are both huge switch to float64 LLL *decisions*.
    # Even then every column operation stays an exact integer (the
    # coordinate transform stays unimodular); no float value enters the
    # enumeration bounds or comparisons.
    max_basis_digits = max(
        (len(str(abs(value))) for column in basis for value in column),
        default=1,
    )
    if max_basis_digits * k > 400:
        reduced_columns, transform_columns = _lll_reduce_adaptive(basis, weights)
        lll_arithmetic = "adaptive_decimal_decisions_exact_integer_columns"
    else:
        reduced_columns, transform_columns = _lll_reduce_exact(basis, weights)
        lll_arithmetic = "exact_rational"
    gram = _gram_matrix(reduced_columns, weights)
    mu, gs_norm = _gram_schmidt(gram, k)

    # Target in lattice coordinates: t = preferred - particular.  Its
    # Gram-Schmidt projections c_i = <t, b*_i> / ||b*_i||^2 are rational.
    t = [preferred[i] - particular[i] for i in range(n)]
    alpha = [_weighted_inner(t, reduced_columns[j], weights) for j in range(k)]
    t_star = [Fraction(0) for _ in range(k)]
    c = [Fraction(0) for _ in range(k)]
    for i in range(k):
        t_star[i] = alpha[i] - sum(
            mu[i][j] * t_star[j] for j in range(i)
        )
        c[i] = t_star[i] / gs_norm[i]

    # -- Integer view under one common denominator Q ------------------
    # Hot-loop comparisons then run on plain ints (equivalent to the
    # rational comparisons without per-operation gcd work):
    #   mu[i][j] = M[i][j]/Q,  B_i = N_i/Q,  c_i = C_i/Q
    # so r_i = c_i - sum_{j>i} z_j mu[j][i] = R_i/Q and the accumulated
    # squared distance is used = U/Q^3 with
    #   U = sum_{fixed j} N_j (Q z_j - R_j)^2.
    q = 1
    for value in gs_norm:
        q = q * value.denominator // _gcd(q, value.denominator)
    for i in range(k):
        for j in range(i):
            q = q * mu[i][j].denominator // _gcd(q, mu[i][j].denominator)
        q = q * c[i].denominator // _gcd(q, c[i].denominator)
    mu_int = [[0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i):
            mu_int[i][j] = int(mu[i][j] * q)
    norm_int = [int(value * q) for value in gs_norm]
    c_int = [int(value * q) for value in c]
    q_cubed = q * q * q

    # The ambient space (dimension n) can be larger than the lattice rank
    # k, so the target generally has a component orthogonal to span(L):
    #   dist²(z) = perp² + sum_i B_i (z_i - r_i(z_{>i}))²,
    #   perp²    = ||t||²_W - sum_i c_i² B_i          (coordinate-free).
    # Enumeration bounds must subtract this exact constant.  In the Q-scaled
    # integer view perp² is perp_scaled / Q³ with every term integral.
    t_norm_sq = _weighted_inner(t, t, weights)
    perp_scaled = t_norm_sq * q_cubed - sum(
        norm_int[i] * c_int[i] * c_int[i] for i in range(k)
    )
    if perp_scaled < 0:  # pragma: no cover - defensive exact identity
        raise ArithmeticError("内部校验失败：目标正交补分量为负")

    # -- Exact Babai nearest-plane point: a feasible lattice vector, hence
    #    a proven (non-artificial) initial squared-distance upper bound.
    babai_zeta = [0] * k
    for i in range(k - 1, -1, -1):
        center = c[i] - sum(babai_zeta[j] * mu[j][i] for j in range(i + 1, k))
        babai_zeta[i] = _nearest_integer(center)

    def reduced_to_original(zeta: Sequence[int]) -> Vector:
        original = [0] * k
        for ell in range(k):
            if zeta[ell]:
                for s in range(k):
                    original[s] += transform_columns[ell][s] * zeta[ell]
        return original

    def point_from_reduced(zeta: Sequence[int]) -> Vector:
        x = list(particular)
        for ell, coordinate in enumerate(zeta):
            if coordinate:
                for i in range(n):
                    x[i] += coordinate * reduced_columns[ell][i]
        return x

    babai_original = reduced_to_original(babai_zeta)
    babai_point = compose(babai_original)
    best_cost = exact_cost(babai_point)

    best_x = list(babai_point)
    best_z = list(babai_original)
    best_scaled = best_cost * q_cubed  # total bound (perp + lattice) in U units
    lattice_bound_scaled = best_scaled - perp_scaled  # bound on sum_i N_i v_i^2
    # The enumeration below covers every point up to and including the
    # bound, so the Babai seed itself is counted there (equality included)
    # rather than pre-counted; start with zero ties.
    tie_count = 0
    nodes_visited = 0
    leaves_evaluated = 0
    empty_intervals = 0

    def consider_leaf(zeta: Sequence[int], lattice_used: int) -> None:
        nonlocal best_cost, best_scaled, lattice_bound_scaled
        nonlocal best_x, best_z, tie_count
        used_scaled = perp_scaled + lattice_used
        if used_scaled < best_scaled:
            x = point_from_reduced(zeta)
            cost = exact_cost(x)
            if cost * q_cubed != used_scaled:  # pragma: no cover - internal
                raise ArithmeticError("内部校验失败：整数化枚举距离不一致")
            best_cost = cost
            best_scaled = used_scaled
            lattice_bound_scaled = best_scaled - perp_scaled
            best_x = x
            best_z = reduced_to_original(zeta)
            tie_count = 1
        elif used_scaled == best_scaled:
            # Co-optimal point: count it, and keep the lexicographically
            # smallest adjustment (x - preferred) in variable order.
            tie_count += 1
            x = point_from_reduced(zeta)
            candidate_dev = tuple(x[i] - preferred[i] for i in range(n))
            current_dev = tuple(best_x[i] - preferred[i] for i in range(n))
            if candidate_dev < current_dev:
                best_x = x
                best_z = reduced_to_original(zeta)

    def _interval_bounds(center_int: int, headroom: int, n_i: int) -> tuple[int, int]:
        """Integers ``z`` with ``n_i (Q z - center_int)^2 <= headroom``.

        ``v = Q z - center_int`` satisfies ``v == -center_int (mod Q)``;
        the admissible v-range comes from one exact integer square root.
        """
        bound_v = isqrt(headroom // n_i)
        residue = (-center_int) % q
        v_high = bound_v - ((bound_v - residue) % q)
        v_low = -bound_v + ((residue + bound_v) % q)
        return (v_low + center_int) // q, (v_high + center_int) // q

    # -- Exact Fincke-Pohst enumeration over the reduced coordinates.
    #    Intervals at every level derive from the current (tightening)
    #    squared-distance bound using integer square roots only.  Trying
    #    the nearest value first tightens the bound early; every compatible
    #    integer is still tried, so this is not a coordinate-wise greedy
    #    decision and no search radius is ever supplied by hand.
    zeta = [0] * k
    # centers[l] = C_l - sum_{j >= i+1} z_j M[j][l] while visiting level i
    centers = list(c_int)

    def search(i: int, lattice_used: int) -> None:
        nonlocal nodes_visited, leaves_evaluated, empty_intervals
        nodes_visited += 1
        headroom = lattice_bound_scaled - lattice_used
        if headroom < 0:
            return
        n_i = norm_int[i]
        r_i = centers[i]
        low, high = _interval_bounds(r_i, headroom, n_i)
        if low > high:
            empty_intervals += 1
            return
        # Nearest integer to r_i / Q (ties toward +inf), clamped into range.
        start = (2 * r_i + q) // (2 * q)
        if start < low:
            start = low
        elif start > high:
            start = high
        sequence = [start]
        down, up = start - 1, start + 1
        while down >= low or up <= high:
            if down >= low:
                sequence.append(down)
                down -= 1
            if up <= high:
                sequence.append(up)
                up += 1
        for value in sequence:
            zeta[i] = value
            delta_v = q * value - r_i
            added = n_i * delta_v * delta_v
            if lattice_used + added > lattice_bound_scaled:
                # Equality must survive so co-optimal points are counted.
                continue
            if i == 0:
                leaves_evaluated += 1
                consider_leaf(zeta, lattice_used + added)
            else:
                # Commit z_i's contribution to all lower-level centers:
                # R_l <- R_l - z_i M[i][l] for l < i; restore on return.
                row_mu = mu_int[i]
                for ell in range(i):
                    centers[ell] -= value * row_mu[ell]
                search(i - 1, lattice_used + added)
                for ell in range(i):
                    centers[ell] += value * row_mu[ell]
        zeta[i] = 0

    search(k - 1, 0)

    # Independent exact self-check: the winner reproduces a lattice point
    # and the rational enumeration cost matches the integer cost.
    check_point = compose(best_z)
    if check_point != best_x:
        raise ArithmeticError("内部校验失败：最优坐标未复现最优校正向量")
    deviations = [best_x[i] - preferred[i] for i in range(n)]
    terms = [weights[i] * deviations[i] ** 2 for i in range(n)]
    if sum(terms) != best_cost:
        raise ArithmeticError("内部校验失败：逐项加权平方未复现总成本")

    return OptimizationResult(
        optimal=list(best_x),
        deviations=deviations,
        weighted_terms=terms,
        cost=best_cost,
        tie_count=tie_count,
        particular=list(particular),
        preferred=preferred,
        weights=weights,
        reduced_basis=[list(column) for column in reduced_columns],
        transform=[list(column) for column in transform_columns],
        gram=gram,
        mu=mu,
        gs_norm=gs_norm,
        babai_coordinates=list(babai_zeta),
        babai_point=babai_point,
        babai_cost=exact_cost(babai_point),
        orthogonal_residual=Fraction(perp_scaled, q_cubed),
        common_denominator=q,
        lll_arithmetic=lll_arithmetic,
        optimal_coordinates=list(best_z),
        nodes_visited=nodes_visited,
        leaves_evaluated=leaves_evaluated,
        empty_intervals=empty_intervals,
    )
