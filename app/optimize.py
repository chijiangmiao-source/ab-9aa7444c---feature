"""Exact weighted closest-lattice-point selection for integer solutions.

Every integer solution of the reviewed coupling system ``A x = b`` has the
form ``x = x0 + B t`` with ``t`` an arbitrary integer vector, where ``x0``
is the particular solution returned by :mod:`app.diophantine` and the
columns of ``B`` are the homogeneous basis vectors (a Z-basis of the
integer kernel, taken here from columns of the Smith ``V`` matrix).

The preferred-shim audit minimises the weighted squared deviation

    f(t) = sum_i w_i (x0_i + (B t)_i - p_i)^2
         = t^T G t + 2 h^T t + c,   G = B^T W B, h = B^T W (x0 - p),

which is a closest-lattice-point problem under the weighted inner product.
It is solved exactly by LLL reduction followed by Fincke-Pohst
(sphere-decoder) enumeration:

  * ``G`` is positive definite (the basis columns are linearly independent
    and every weight is a positive integer);
  * an exact rational LLL reduction (delta = 3/4) replaces the coordinate
    system by a unimodular transform ``U`` -- every step is integer or
    rational arithmetic, so the reduced Gram matrix ``U^T G U`` is exact;
  * an exact rational LDL^T decomposition of the reduced Gram matrix
    triangularises the quadratic form without any square root or floating
    point value;
  * the search ellipsoid is the *incumbent solution's own exact cost*,
    initially the particular solution ``t = 0`` -- a genuine lattice
    point, so the optimum is provably inside; no artificial radius is
    ever introduced;
  * every coordinate interval is obtained from integer square-root bounds
    on cross-multiplied rational discriminants (``math.isqrt``); no
    floating distance is ever measured and no free direction is resolved
    by a greedy one-axis search;
  * candidates attaining the same minimum are adjudicated by the
    lexicographically smallest adjustment vector in variable order.

All arithmetic is arbitrary-precision ``int`` / ``fractions.Fraction``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd, isqrt
from typing import Iterator, List, Sequence


@dataclass
class LevelEvidence:
    """Reproducible enumeration record on the optimal coordinate path.

    With the outer coordinates fixed at the optimum, every integer in
    ``[low, high]`` was admitted for examination under the final bound
    (outside it the residual quadratic form provably exceeds the optimum).
    ``chosen`` is the coordinate of the optimum and ``accumulated`` is the
    exact rational value ``rho_0 + sum_{j >= level} d_j s_j^2`` there.
    """

    level: int
    low: int
    high: int
    chosen: int
    accumulated_num: int
    accumulated_den: int


@dataclass
class OptimizationResult:
    correction: List[int]
    adjustments: List[int]
    per_item_cost: List[int]
    cost: int
    coordinates: List[int]  # t* in homogeneousBasis column order
    gram: List[List[int]]  # reduced Gram matrix U^T B^T W B U (enumerated)
    linear: List[int]  # reduced linear coefficients U^T h; term is 2 h^T t'
    constant: int  # c = (x0 - p)^T W (x0 - p) = f(0)
    basis_transform: List[List[int]]  # unimodular U; t = U t'
    reduced_coordinates: List[int]  # optimum t' in the LLL-reduced basis
    lower: List[List[Fraction]]  # unit lower triangular L of gram = L D L^T
    diagonal: List[Fraction]  # pivots D
    rational_minimum: Fraction  # unrestricted real minimum rho_0
    nodes_visited: int
    nodes_pruned: int
    leaves_evaluated: int
    ties_compared: int
    level_evidence: List[LevelEvidence] = field(default_factory=list)


def _ceil_div(a: int, b: int) -> int:
    """Exact ``ceil(a / b)`` with ``b > 0``."""
    return -((-a) // b)


def _as_int(value: Fraction) -> int:
    """Convert an integer-valued rational to ``int`` exactly."""
    if value.denominator != 1:  # pragma: no cover - exact identity
        raise ArithmeticError("内部校验失败：公共分母换算未得到整数")
    return value.numerator


def _ldl_decomposition(
    gram: Sequence[Sequence[int]],
) -> tuple[List[List[Fraction]], List[Fraction]]:
    """Exact rational LDL^T decomposition with unit lower triangular L."""
    r = len(gram)
    lower = [[Fraction(0) for _ in range(r)] for _ in range(r)]
    diagonal: List[Fraction] = []
    for k in range(r):
        lower[k][k] = Fraction(1)
        pivot = Fraction(gram[k][k])
        for j in range(k):
            pivot -= lower[k][j] * lower[k][j] * diagonal[j]
        if pivot <= 0:
            # B columns are independent and W is positive diagonal, so G
            # must be positive definite; failure here is an internal bug.
            raise ArithmeticError("内部校验失败：加权格 Gram 矩阵非正定")
        diagonal.append(pivot)
        for i in range(k + 1, r):
            value = Fraction(gram[i][k])
            for j in range(k):
                value -= lower[i][j] * diagonal[j] * lower[k][j]
            lower[i][k] = value / pivot
    return lower, diagonal


def _lll_reduce(gram: Sequence[Sequence[int]]) -> tuple[List[List[int]], List[List[int]]]:
    """Exact LLL reduction of a positive definite integer Gram matrix.

    Returns ``(transform, reduced)`` with ``transform`` a unimodular integer
    matrix U and ``reduced = U^T gram U`` LLL-reduced (delta = 3/4).  Every
    step is integer or rational arithmetic; the Gram-Schmidt coefficients
    are recomputed exactly after each elementary operation.
    """
    r = len(gram)
    reduced = [[Fraction(gram[i][j]) for j in range(r)] for i in range(r)]
    transform = [[1 if i == j else 0 for j in range(r)] for i in range(r)]
    delta = Fraction(3, 4)

    def gram_schmidt():
        mu = [[Fraction(0)] * r for _ in range(r)]
        norms = [Fraction(0)] * r
        for i in range(r):
            for j in range(i):
                value = reduced[i][j]
                for k in range(j):
                    value -= mu[i][k] * mu[j][k] * norms[k]
                mu[i][j] = value / norms[j]
            norm = reduced[i][i]
            for k in range(i):
                norm -= mu[i][k] * mu[i][k] * norms[k]
            if norm <= 0:
                # Positive definiteness of G is guaranteed upstream.
                raise ArithmeticError("内部校验失败：LLL 约化遇到非正定形式")
            norms[i] = norm
        return mu, norms

    mu, norms = gram_schmidt()
    k = 1
    while k < r:
        # Size-reduce basis vector k against the earlier ones.
        for j in range(k - 1, -1, -1):
            coeff = mu[k][j]
            nearest = (2 * coeff.numerator + coeff.denominator) // (
                2 * coeff.denominator
            )
            if nearest:
                for i in range(r):
                    transform[i][k] -= nearest * transform[i][j]
                for i in range(r):
                    reduced[i][k] -= nearest * reduced[i][j]
                for i in range(r):
                    reduced[k][i] -= nearest * reduced[j][i]
                mu, norms = gram_schmidt()
        if norms[k] >= (delta - mu[k][k - 1] * mu[k][k - 1]) * norms[k - 1]:
            k += 1
        else:
            for i in range(r):
                transform[i][k], transform[i][k - 1] = (
                    transform[i][k - 1],
                    transform[i][k],
                )
                reduced[i][k], reduced[i][k - 1] = reduced[i][k - 1], reduced[i][k]
            for i in range(r):
                reduced[k][i], reduced[k - 1][i] = reduced[k - 1][i], reduced[k][i]
            mu, norms = gram_schmidt()
            k = max(k - 1, 1)

    reduced_int = [[_as_int(value) for value in row] for row in reduced]
    return transform, reduced_int


def _ordered_integers(low: int, high: int, center_num: int, den: int) -> Iterator[int]:
    """Yield the integers of ``[low, high]`` in non-decreasing distance
    from ``center_num / den``.

    At equal distance the smaller integer is yielded first.  The
    non-decreasing distance property is what allows the enumeration to
    stop as soon as a candidate exceeds the incumbent's exact bound:
    every later candidate is at least as far from the centre.
    """
    if low > high:
        return
    quotient, remainder = divmod(center_num, den)  # den > 0, 0 <= rem < den
    start = quotient if 2 * remainder <= den else quotient + 1
    # eps = centre - start lies in [-1/2, 1/2]; its sign decides which side
    # is closer for every delta >= 1 (ties resolve to the smaller side).
    eps_num = center_num - start * den
    plus_first = eps_num > 0
    delta = 0
    while start - delta >= low or start + delta <= high:
        if delta == 0:
            candidates = (start,)
        elif plus_first:
            candidates = (start + delta, start - delta)
        else:
            candidates = (start - delta, start + delta)
        for value in candidates:
            if low <= value <= high:
                yield value
        delta += 1


def _interval_for(k, current, partial, bound, *, shift_bases, muls, denoms,
                  diag_num, diag_den, rho0):
    """Exact integer interval for coordinate t_k given fixed outer ones.

    Feasibility of the residual form is

        d_k (num_s / denom_k)^2 <= bound - rho_0 - partial,

    where ``num_s = denom_k t_k + shift`` and ``d_k`` and the residual are
    nonnegative rationals.  Cross-multiplying turns it into
    ``num_s^2 <= p_num / p_den`` and an integer ``isqrt`` bound -- no
    irrational number and no floating point value appears.
    """
    residual = Fraction(bound) - rho0 - partial
    if residual < 0:
        return None
    shift = shift_bases[k]
    for j in range(k + 1, len(current)):
        shift += muls[k][j] * current[j]
    denom = denoms[k]
    p_num = residual.numerator * diag_den[k] * denom * denom
    p_den = residual.denominator * diag_num[k]
    radius = isqrt(p_num // p_den)
    low = _ceil_div(-radius - shift, denom)
    high = (radius - shift) // denom
    return low, high, shift, denom


def optimize_correction(
    solution: Sequence[int],
    homogeneous_basis: Sequence[Sequence[int]],
    preferred: Sequence[int],
    weights: Sequence[int],
) -> OptimizationResult:
    """Return the exact weighted closest-lattice-point correction.

    ``homogeneous_basis`` is the sequence of r kernel vectors (each of
    length n); feasible corrections are
    ``solution + sum_k t_k * basis[k]`` for integer ``t_k``.
    ``preferred`` and ``weights`` are aligned with the variables.
    """
    n = len(solution)
    if len(preferred) != n or len(weights) != n:
        raise ValueError("首选值与权重必须逐项对齐全部变量")
    if any(not isinstance(w, int) or isinstance(w, bool) or w <= 0 for w in weights):
        raise ValueError("权重必须为正整数")
    basis = [list(vector) for vector in homogeneous_basis]
    if any(len(vector) != n for vector in basis):
        raise ValueError("齐次解向量长度必须与变量数一致")

    x0 = [int(value) for value in solution]
    p = [int(value) for value in preferred]
    w = [int(value) for value in weights]
    r = len(basis)
    offset = [x0[i] - p[i] for i in range(n)]

    # Exact integer quadratic form f(t) = t^T G t + 2 h^T t + c.
    gram: List[List[int]] = [[0] * r for _ in range(r)]
    linear: List[int] = [0] * r
    constant = 0
    for i in range(n):
        wi = w[i]
        constant += wi * offset[i] * offset[i]
        for k in range(r):
            bk = basis[k][i]
            linear[k] += wi * offset[i] * bk
            for j in range(k, r):
                gram[k][j] += wi * bk * basis[j][i]
    for k in range(1, r):
        for j in range(k):
            gram[k][j] = gram[j][k]

    def per_item(adj):
        return [w[i] * adj[i] * adj[i] for i in range(n)]

    if r == 0:
        # Unique solution: the only feasible correction, hence the optimum.
        cost = constant
        if cost < 0:  # pragma: no cover - sum of nonnegative terms
            raise ArithmeticError("内部校验失败：代价为负")
        return OptimizationResult(
            correction=list(x0),
            adjustments=list(offset),
            per_item_cost=per_item(offset),
            cost=cost,
            coordinates=[],
            gram=[],
            linear=[],
            constant=constant,
            basis_transform=[],
            reduced_coordinates=[],
            lower=[],
            diagonal=[],
            rational_minimum=Fraction(constant, 1),
            nodes_visited=0,
            nodes_pruned=0,
            leaves_evaluated=1,
            ties_compared=0,
        )

    # Exact rational LLL reduction of the coordinate lattice: t = U t' with
    # U unimodular, so enumerating all integer t' visits every feasible
    # correction exactly once.  The Smith V columns can be highly skewed;
    # the LLL-reduced Gram matrix keeps the Fincke-Pohst intervals small.
    transform, gram = _lll_reduce(gram)
    original_linear = linear
    linear = [
        sum(transform[a][k] * original_linear[a] for a in range(r))
        for k in range(r)
    ]
    reduced_basis = [
        [
            sum(basis[c][i] * transform[c][k] for c in range(r))
            for i in range(n)
        ]
        for k in range(r)
    ]

    lower, diagonal = _ldl_decomposition(gram)

    # z = L^{-1} h (forward substitution, unit diagonal); v = D^{-1} z;
    # rho_0 = c - z^T D^{-1} z is the unrestricted real minimum, an exact
    # rational lower bound on every feasible integer cost.
    z_vec: List[Fraction] = []
    v_vec: List[Fraction] = []
    rational_minimum = Fraction(constant)
    for k in range(r):
        value = Fraction(linear[k])
        for j in range(k):
            value -= lower[k][j] * z_vec[j]
        z_vec.append(value)
        v_vec.append(value / diagonal[k])
        rational_minimum -= value * value / diagonal[k]
    if rational_minimum < 0:  # pragma: no cover - impossible for G > 0
        raise ArithmeticError("内部校验失败：连续最优代价为负")

    # Exact integer descriptions of s_k = t_k + sum_{j>k} L[j][k] t_j + v_k:
    # s_k = (denom_k t_k + shift) / denom_k with shift rebuilt per node.
    denoms: List[int] = []
    shift_bases: List[int] = []
    muls: List[List[int]] = [[0] * r for _ in range(r)]
    diag_num: List[int] = []
    diag_den: List[int] = []
    for k in range(r):
        denom = v_vec[k].denominator
        for j in range(k + 1, r):
            lkj = lower[j][k]
            denom = denom * lkj.denominator // gcd(denom, lkj.denominator)
        denoms.append(denom)
        shift_bases.append(_as_int(v_vec[k] * denom))
        for j in range(k + 1, r):
            muls[k][j] = _as_int(lower[j][k] * denom)
        diag_num.append(diagonal[k].numerator)
        diag_den.append(diagonal[k].denominator)

    interval_args = dict(shift_bases=shift_bases, muls=muls, denoms=denoms,
                         diag_num=diag_num, diag_den=diag_den,
                         rho0=rational_minimum)

    stats = {"visited": 0, "pruned": 0, "leaves": 0, "ties": 0}

    # Incumbent starts at the genuine lattice point t = 0 (the particular
    # solution). Its exact cost is the first bound, and the only kind of
    # bound ever used: the ellipsoid always contains the incumbent itself.
    best_cost = constant
    best_coordinates = [0] * r
    best_adjustments = list(offset)
    current = [0] * r

    def exact_cost() -> int:
        cost = constant
        for a in range(r):
            ta = current[a]
            cost += 2 * linear[a] * ta
            for b in range(r):
                cost += gram[a][b] * ta * current[b]
        return cost

    def adjustments_of(coords) -> List[int]:
        return [
            offset[i]
            + sum(reduced_basis[col][i] * coords[col] for col in range(r))
            for i in range(n)
        ]

    def search(k: int, partial: Fraction) -> None:
        nonlocal best_cost
        stats["visited"] += 1
        interval = _interval_for(k, current, partial, best_cost, **interval_args)
        if interval is None:
            stats["pruned"] += 1
            return
        low, high, shift, denom = interval
        if low > high:
            stats["pruned"] += 1
            return
        for value in _ordered_integers(low, high, -shift, denom):
            current[k] = value
            numerator_s = denom * value + shift
            contribution = Fraction(
                diag_num[k] * numerator_s * numerator_s,
                diag_den[k] * denom * denom,
            )
            # Candidates arrive in non-decreasing |s_k|: once the partial
            # cost exceeds the incumbent's exact cost, every later
            # candidate at this level is hopeless as well.  The bound is
            # always the incumbent lattice point's own cost -- never an
            # artificial radius.
            if rational_minimum + partial + contribution > best_cost:
                break
            if k == 0:
                stats["leaves"] += 1
                cost = exact_cost()
                # Exact cross-check against the triangular rational form.
                rational_value = rational_minimum + partial + contribution
                if rational_value != cost:  # pragma: no cover - identity
                    raise ArithmeticError("内部校验失败：枚举代价与有理式不一致")
                adjustments = adjustments_of(current)
                if cost < best_cost:
                    best_cost = cost
                    best_coordinates[:] = current
                    best_adjustments[:] = adjustments
                elif cost == best_cost:
                    stats["ties"] += 1
                    if tuple(adjustments) < tuple(best_adjustments):
                        best_coordinates[:] = current
                        best_adjustments[:] = adjustments
            else:
                search(k - 1, partial + contribution)

    search(r - 1, Fraction(0))

    correction = [p[i] + best_adjustments[i] for i in range(n)]
    per_item_cost = per_item(best_adjustments)
    if sum(per_item_cost) != best_cost:  # pragma: no cover - exact identity
        raise ArithmeticError("内部校验失败：逐项代价之和不等于最优代价")
    # Map the optimum back to the original homogeneous coordinates t = U t'.
    original_coordinates = [
        sum(transform[a][k] * best_coordinates[k] for k in range(r))
        for a in range(r)
    ]
    for i in range(n):
        check = x0[i] + sum(basis[col][i] * original_coordinates[col] for col in range(r))
        if check != correction[i]:  # pragma: no cover - exact identity
            raise ArithmeticError("内部校验失败：最优校正量复算失败")
    # Exact integrity checks: the coordinate transform produced by LLL must
    # be unimodular, and the reduced columns must be exactly B U.
    if _integer_determinant(transform) not in (1, -1):  # pragma: no cover
        raise ArithmeticError("内部校验失败：LLL 坐标变换非幺模")
    for k in range(r):
        for i in range(n):
            rebuilt = sum(basis[c][i] * transform[c][k] for c in range(r))
            if rebuilt != reduced_basis[k][i]:  # pragma: no cover - identity
                raise ArithmeticError("内部校验失败：约化基复算失败")

    # Replay the winning path under the final (tightest) bound to emit
    # reproducible per-level interval evidence.
    level_evidence: List[LevelEvidence] = []
    partial = Fraction(0)
    for k in range(r - 1, -1, -1):
        low, high, shift, denom = _interval_for(
            k, best_coordinates, partial, best_cost, **interval_args
        )
        numerator_s = denom * best_coordinates[k] + shift
        partial += Fraction(
            diag_num[k] * numerator_s * numerator_s,
            diag_den[k] * denom * denom,
        )
        accumulated = rational_minimum + partial
        level_evidence.append(
            LevelEvidence(
                level=k,
                low=low,
                high=high,
                chosen=best_coordinates[k],
                accumulated_num=accumulated.numerator,
                accumulated_den=accumulated.denominator,
            )
        )

    return OptimizationResult(
        correction=correction,
        adjustments=best_adjustments,
        per_item_cost=per_item_cost,
        cost=best_cost,
        coordinates=original_coordinates,
        gram=gram,
        linear=linear,
        constant=constant,
        basis_transform=transform,
        reduced_coordinates=list(best_coordinates),
        lower=lower,
        diagonal=diagonal,
        rational_minimum=rational_minimum,
        nodes_visited=stats["visited"],
        nodes_pruned=stats["pruned"],
        leaves_evaluated=stats["leaves"],
        ties_compared=stats["ties"],
        level_evidence=level_evidence,
    )


def _integer_determinant(matrix: Sequence[Sequence[int]]) -> int:
    """Exact determinant of an integer matrix (fraction-free Bareiss)."""
    a = [list(row) for row in matrix]
    size = len(a)
    sign = 1
    previous = 1
    for k in range(size - 1):
        if a[k][k] == 0:
            pivot = next((i for i in range(k + 1, size) if a[i][k] != 0), None)
            if pivot is None:
                return 0
            a[k], a[pivot] = a[pivot], a[k]
            sign = -sign
        for i in range(k + 1, size):
            for j in range(k + 1, size):
                a[i][j] = (a[i][j] * a[k][k] - a[i][k] * a[k][j]) // previous
        previous = a[k][k]
        for i in range(k + 1, size):
            a[i][k] = 0
    return sign * a[size - 1][size - 1]
