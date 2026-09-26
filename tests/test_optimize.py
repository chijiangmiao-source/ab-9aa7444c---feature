"""Unit tests for the exact weighted closest-lattice-point audit."""

import itertools
import random
import unittest
from fractions import Fraction

from app.diophantine import solve_diophantine
from app.optimize import optimize_correction


class OptimizeBasicTest(unittest.TestCase):
    def test_unique_solution_is_the_only_choice(self):
        result = solve_diophantine([[2, 0], [0, 3]], [4, 9])
        self.assertTrue(result.solvable)
        self.assertEqual(result.homogeneous_basis, [])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [100, -50], [7, 3]
        )
        self.assertEqual(opt.correction, [2, 3])
        self.assertEqual(opt.adjustments, [2 - 100, 3 - (-50)])
        self.assertEqual(opt.cost, 7 * 98 * 98 + 3 * 53 * 53)
        self.assertEqual(sum(opt.per_item_cost), opt.cost)
        self.assertEqual(opt.coordinates, [])
        self.assertEqual(opt.leaves_evaluated, 1)

    def test_preferred_is_reachable_with_zero_cost(self):
        # x + y = 5 with preferred (2, 3) is itself a solution.
        result = solve_diophantine([[1, 1]], [5])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [2, 3], [1, 1]
        )
        self.assertEqual(opt.correction, [2, 3])
        self.assertEqual(opt.adjustments, [0, 0])
        self.assertEqual(opt.cost, 0)
        self.assertEqual(opt.rational_minimum, 0)

    def test_weight_redirects_correction(self):
        # x + y = 2, prefer (0, 0): the heavy variable should stay put.
        result = solve_diophantine([[1, 1]], [2])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [0, 0], [1, 100]
        )
        self.assertEqual(opt.correction, [2, 0])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [0, 0], [100, 1]
        )
        self.assertEqual(opt.correction, [0, 2])

    def test_cost_and_adjustments_are_exact_big_integers(self):
        big = 2**70 + 12345
        matrix = [[big, 1], [1, 1]]
        target = [big * 5 + 7, 12]
        result = solve_diophantine(matrix, target)
        self.assertTrue(result.solvable)
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [0, 0], [1, 1]
        )
        for i, row in enumerate(matrix):
            self.assertEqual(sum(a * x for a, x in zip(row, opt.correction)), target[i])
        self.assertEqual(sum(opt.per_item_cost), opt.cost)
        self.assertIsInstance(opt.cost, int)

    def test_invalid_weights_rejected(self):
        result = solve_diophantine([[1, 1]], [2])
        with self.assertRaises(ValueError):
            optimize_correction(
                result.solution, result.homogeneous_basis, [0, 0], [0, 1]
            )
        with self.assertRaises(ValueError):
            optimize_correction(
                result.solution, result.homogeneous_basis, [0, 0], [-2, 1]
            )

    def test_misaligned_inputs_rejected(self):
        result = solve_diophantine([[1, 1]], [2])
        with self.assertRaises(ValueError):
            optimize_correction(
                result.solution, result.homogeneous_basis, [0], [1, 1]
            )


class TieBreakTest(unittest.TestCase):
    def test_lexicographic_tie_on_adjustments(self):
        # x - y = 0, so x = y = t.  Prefer (1, 0):
        # (t - 1)^2 + t^2 = 2t^2 - 2t + 1, minimised at t = 0 and t = 1
        # with equal cost 1.  Adjustments: (-1, 0) vs (0, -1); the
        # lexicographically smaller adjustment wins.
        result = solve_diophantine([[1, -1]], [0])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [1, 0], [1, 1]
        )
        self.assertEqual(opt.cost, 1)
        self.assertEqual(opt.adjustments, [-1, 0])
        self.assertGreaterEqual(opt.ties_compared, 1)

    def test_tie_compared_with_weighted_axes(self):
        # x + y = 1, prefer (0, 0), weights (1, 1): (0, 1) and (1, 0)
        # both cost 1; adjustments (0, 1) vs (1, 0) — lex picks (0, 1).
        result = solve_diophantine([[1, 1]], [1])
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, [0, 0], [1, 1]
        )
        self.assertEqual(opt.cost, 1)
        self.assertEqual(opt.adjustments, [0, 1])
        self.assertEqual(opt.correction, [0, 1])
        self.assertGreaterEqual(opt.ties_compared, 1)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        result = solve_diophantine([[2, 3, 5]], [30])
        self.result = result
        self.opt = optimize_correction(
            result.solution, result.homogeneous_basis, [1, 2, 3], [3, 1, 2]
        )

    def test_final_accumulated_equals_cost(self):
        last = self.opt.level_evidence[-1]
        accumulated = Fraction(last.accumulated_num, last.accumulated_den)
        self.assertEqual(accumulated, self.opt.cost)

    def test_chosen_coordinate_lies_in_interval(self):
        for entry in self.opt.level_evidence:
            self.assertLessEqual(entry.low, entry.chosen)
            self.assertLessEqual(entry.chosen, entry.high)

    def test_quadratic_form_identity(self):
        # Independently recompute f(t*) from G, h, c (reduced coordinates).
        t = self.opt.reduced_coordinates
        r = len(t)
        cost = self.opt.constant
        for a in range(r):
            cost += 2 * self.opt.linear[a] * t[a]
            for b in range(r):
                cost += self.opt.gram[a][b] * t[a] * t[b]
        self.assertEqual(cost, self.opt.cost)

    def test_coordinate_map_is_unimodular_and_consistent(self):
        r = len(self.opt.coordinates)
        if r == 0:
            self.assertEqual(self.opt.basis_transform, [])
            return
        # t (original) = U t' (reduced)
        for a in range(r):
            mapped = sum(
                self.opt.basis_transform[a][k] * self.opt.reduced_coordinates[k]
                for k in range(r)
            )
            self.assertEqual(mapped, self.opt.coordinates[a])

    def test_rational_minimum_below_or_at_cost(self):
        self.assertLessEqual(self.opt.rational_minimum, Fraction(self.opt.cost))
        self.assertGreaterEqual(self.opt.rational_minimum, 0)

    def test_ldl_reproduces_gram(self):
        r = len(self.opt.gram)
        for i in range(r):
            for j in range(r):
                value = Fraction(0)
                for k in range(min(i, j) + 1):
                    value += (
                        self.opt.lower[i][k]
                        * self.opt.diagonal[k]
                        * self.opt.lower[j][k]
                    )
                self.assertEqual(value, Fraction(self.opt.gram[i][j]))


class LLLReductionTest(unittest.TestCase):
    def test_reduced_gram_equals_transformed_original(self):
        from app.optimize import _lll_reduce

        rng = random.Random(55)
        for _ in range(30):
            r = rng.randint(1, 7)
            n = rng.randint(r, r + 4)
            basis = [[rng.randint(-8, 8) for _ in range(n)] for _ in range(r)]
            weights = [rng.randint(1, 6) for _ in range(n)]
            gram = [
                [sum(weights[i] * basis[a][i] * basis[c][i] for i in range(n))
                 for c in range(r)]
                for a in range(r)
            ]
            transform, reduced = _lll_reduce(gram)
            # reduced == U^T G U exactly
            for a in range(r):
                for c in range(r):
                    expected = sum(
                        transform[i][a] * gram[i][j] * transform[j][c]
                        for i in range(r)
                        for j in range(r)
                    )
                    self.assertEqual(reduced[a][c], expected)
            # U is unimodular and symmetric-ish structure stays integral
            from app.optimize import _integer_determinant
            self.assertIn(_integer_determinant(transform), (1, -1))
            # LLL condition: norm order check on Cholesky pivots via LDL
            pivots = _ldl_pivots(reduced)
            delta = Fraction(3, 4)
            mu = _gs_coefficients(reduced)
            for k in range(1, r):
                self.assertGreaterEqual(
                    pivots[k],
                    (delta - mu[k][k - 1] ** 2) * pivots[k - 1],
                )

    def test_identity_transform_for_orthogonal_gram(self):
        from app.optimize import _lll_reduce

        gram = [[2, 0, 0], [0, 3, 0], [0, 0, 5]]
        transform, reduced = _lll_reduce(gram)
        self.assertEqual(reduced, gram)
        self.assertEqual(transform, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])


def _ldl_pivots(gram):
    r = len(gram)
    lower = [[Fraction(0)] * r for _ in range(r)]
    pivots = []
    for k in range(r):
        lower[k][k] = Fraction(1)
        pivot = Fraction(gram[k][k])
        for j in range(k):
            pivot -= lower[k][j] * lower[k][j] * pivots[j]
        pivots.append(pivot)
        for i in range(k + 1, r):
            value = Fraction(gram[i][k])
            for j in range(k):
                value -= lower[i][j] * pivots[j] * lower[k][j]
            lower[i][k] = value / pivot
    return pivots


def _gs_coefficients(gram):
    """Exact Gram-Schmidt coefficients mu[i][j] from a Gram matrix."""
    r = len(gram)
    mu = [[Fraction(0)] * r for _ in range(r)]
    norms = [Fraction(0)] * r
    for i in range(r):
        for j in range(i):
            value = Fraction(gram[i][j])
            for k in range(j):
                value -= mu[i][k] * mu[j][k] * norms[k]
            mu[i][j] = value / norms[j]
        norm = Fraction(gram[i][i])
        for k in range(i):
            norm -= mu[i][k] * mu[i][k] * norms[k]
        norms[i] = norm
    return mu


class HigherDimensionTest(unittest.TestCase):
    def test_sixteen_dim_kernel_optimum_exact(self):
        rng = random.Random(99)
        n, m = 20, 4
        a = [[rng.randint(-8, 8) for _ in range(n)] for _ in range(m)]
        x_seed = [rng.randint(-3, 3) for _ in range(n)]
        b = [sum(aij * xj for aij, xj in zip(row, x_seed)) for row in a]
        result = solve_diophantine(a, b)
        self.assertEqual(len(result.homogeneous_basis), n - m)
        preferred = [rng.randint(-6, 6) for _ in range(n)]
        weights = [rng.randint(1, 5) for _ in range(n)]
        opt = optimize_correction(
            result.solution, result.homogeneous_basis, preferred, weights
        )
        for i, row in enumerate(a):
            self.assertEqual(
                sum(aij * xj for aij, xj in zip(row, opt.correction)), b[i]
            )
        self.assertEqual(sum(opt.per_item_cost), opt.cost)
        self.assertLessEqual(opt.rational_minimum, Fraction(opt.cost))
        self.assertEqual(len(opt.level_evidence), n - m)
        for entry in opt.level_evidence:
            self.assertLessEqual(entry.low, entry.chosen)
            self.assertLessEqual(entry.chosen, entry.high)


class BruteForceAgreementTest(unittest.TestCase):
    def test_random_systems_match_exhaustive_enumeration(self):
        rng = random.Random(20260926)
        checked = 0
        for _ in range(120):
            m = rng.randint(1, 4)
            n = rng.randint(1, 5)
            a = [[rng.randint(-6, 6) for _ in range(n)] for _ in range(m)]
            x_seed = [rng.randint(-4, 4) for _ in range(n)]
            b = [sum(aij * xj for aij, xj in zip(row, x_seed)) for row in a]
            result = solve_diophantine(a, b)
            self.assertTrue(result.solvable)
            r = len(result.homogeneous_basis)
            if r > 3:
                continue
            preferred = [rng.randint(-6, 6) for _ in range(n)]
            weights = [rng.randint(1, 5) for _ in range(n)]
            opt = optimize_correction(
                result.solution, result.homogeneous_basis, preferred, weights
            )
            radius = max([12] + [abs(t) + 3 for t in opt.coordinates])
            best = None
            best_adj = None
            for t in itertools.product(range(-radius, radius + 1), repeat=r):
                adj = [
                    result.solution[i]
                    + sum(result.homogeneous_basis[k][i] * t[k] for k in range(r))
                    - preferred[i]
                    for i in range(n)
                ]
                cost = sum(weights[i] * adj[i] ** 2 for i in range(n))
                if best is None or cost < best or (
                    cost == best and tuple(adj) < tuple(best_adj)
                ):
                    best, best_adj = cost, adj
            self.assertEqual(opt.cost, best)
            self.assertEqual(opt.adjustments, best_adj)
            checked += 1
        self.assertGreater(checked, 50)


if __name__ == "__main__":
    unittest.main()
