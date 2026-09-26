"""Unit tests for the exact weighted closest-lattice-point optimizer."""

import itertools
import random
import unittest

from app.diophantine import solve_diophantine
from app.lattice import optimize_corrections


def brute_force(particular, basis, preferred, weights, span=10):
    """Enumerate every coordinate vector in a box for cross-checking."""
    k = len(basis)
    n = len(particular)
    best = None
    ties = 0
    for z in itertools.product(range(-span, span + 1), repeat=k):
        x = [
            particular[i] + sum(z[j] * basis[j][i] for j in range(k))
            for i in range(n)
        ]
        cost = sum(weights[i] * (x[i] - preferred[i]) ** 2 for i in range(n))
        if best is None or cost < best[0]:
            best = (cost, list(x))
            ties = 1
        elif cost == best[0]:
            ties += 1
            if tuple(x[i] - preferred[i] for i in range(n)) < tuple(
                best[1][i] - preferred[i] for i in range(n)
            ):
                best = (cost, list(x))
    return best, ties


class OptimizeCorrectionsTest(unittest.TestCase):
    def test_unique_solution(self):
        result = solve_diophantine(
            [[1, 0], [0, 1]], [5, -7]
        )
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [0, 0], [3, 4]
        )
        self.assertEqual(opt.optimal, [5, -7])
        self.assertEqual(opt.deviations, [5, -7])
        self.assertEqual(opt.weighted_terms, [75, 196])
        self.assertEqual(opt.cost, 271)
        self.assertEqual(opt.tie_count, 1)
        self.assertEqual(opt.optimal_coordinates, [])

    def test_single_free_direction_weighted_pick(self):
        # 2x + 2y = 2  =>  x + y = 1; minimize (x-p)^2 + w(y-q)^2.
        result = solve_diophantine([[2, 2]], [2])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [0, 0], [1, 1]
        )
        # Candidates ... (0,1), (1,0) both cost 1; deviations (0,1) vs (1,0):
        # lexicographically smallest adjustment is (0,1).
        self.assertEqual(opt.cost, 1)
        self.assertEqual(opt.optimal, [0, 1])
        self.assertEqual(opt.deviations, [0, 1])
        self.assertEqual(opt.tie_count, 2)

    def test_lex_tie_breaks_on_adjustment_vector(self):
        # x - y = 0 => x = y; preferred (0,3): x=1 -> cost 5, x=2 -> cost 5.
        result = solve_diophantine([[2, -2]], [0])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [0, 3], [1, 1]
        )
        self.assertEqual(opt.cost, 5)
        self.assertEqual(opt.optimal, [1, 1])
        self.assertEqual(opt.tie_count, 2)

    def test_weighted_choice_prefers_heavier_coordinate(self):
        # x + y = 1; prefer (0,0), weights (1, 100): deviating in x is much
        # cheaper, so (1,0) is uniquely optimal.
        result = solve_diophantine([[1, 1]], [1])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [0, 0], [1, 100]
        )
        self.assertEqual(opt.optimal, [1, 0])
        self.assertEqual(opt.cost, 1)
        self.assertEqual(opt.tie_count, 1)

    def test_preference_itself_solvable_gives_zero_cost(self):
        # x + y = 0 and preferred (2, -2) is itself a solution.
        result = solve_diophantine([[1, 1]], [0])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [2, -2], [3, 7]
        )
        self.assertEqual(opt.optimal, [2, -2])
        self.assertEqual(opt.deviations, [0, 0])
        self.assertEqual(opt.cost, 0)
        self.assertEqual(opt.tie_count, 1)

    def test_exact_coordinates_reproduce_optimum(self):
        # Underdetermined system with one (huge-entry) homogeneous direction.
        result = solve_diophantine(
            [[9007199254740993, 1, 0], [1, 3, 1]],
            [18014398509481989, 12],
        )
        self.assertEqual(len(result.homogeneous_basis), 1)
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [1, 2, 0], [1, 2, 1]
        )
        # Reproduce the optimum from particular + integer lattice coordinates.
        n = len(result.solution)
        rebuilt = list(result.solution)
        for s, z in enumerate(opt.optimal_coordinates):
            for i in range(n):
                rebuilt[i] += z * result.homogeneous_basis[s][i]
        self.assertEqual(rebuilt, opt.optimal)
        self.assertEqual(opt.optimal, [2, 3, 1])
        # Coordinates are integer multiples of the (LLL-reduced) kernel
        # basis; the rebuilt-point equality above is the basis-independent
        # contract, so no coordinate sign is hard-coded here.
        self.assertEqual(
            opt.weighted_terms,
            [
                1 * (opt.optimal[0] - 1) ** 2,
                2 * (opt.optimal[1] - 2) ** 2,
                1 * (opt.optimal[2] - 0) ** 2,
            ],
        )
        self.assertEqual(sum(opt.weighted_terms), opt.cost)

    def test_bound_evidence_is_feasible_and_valid(self):
        result = solve_diophantine([[1, -1000003]], [7])
        opt = optimize_corrections(
            result.solution,
            result.homogeneous_basis,
            [500000, -123456],
            [2, 3],
        )
        # The Babai seed is an actual lattice point (rebuild from coordinates).
        n = 2
        seed = list(result.solution)
        for ell, zeta in enumerate(opt.babai_coordinates):
            for i in range(n):
                seed[i] += zeta * opt.reduced_basis[ell][i]
        self.assertEqual(seed, opt.babai_point)
        seed_cost = sum(
            opt.weights[i] * (seed[i] - opt.preferred[i]) ** 2 for i in range(n)
        )
        self.assertEqual(seed_cost, opt.babai_cost)
        # Initial bound must be at least the optimum; enumeration counted leaves.
        self.assertGreaterEqual(opt.babai_cost, opt.cost)
        self.assertGreaterEqual(opt.leaves_evaluated, 1)
        self.assertGreaterEqual(opt.nodes_visited, 1)

    def test_orthogonal_residual_zero_when_lattice_spans_space(self):
        # Zero equations matrix: lattice = Z^n, every vector reachable, so
        # the target has no orthogonal component.
        result = solve_diophantine([[0, 0, 0]], [0])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [5, -7, 2], [2, 3, 1]
        )
        self.assertEqual(opt.orthogonal_residual, 0)
        self.assertEqual(opt.optimal, [5, -7, 2])
        self.assertEqual(opt.cost, 0)

    def test_orthogonal_residual_is_exact_constant(self):
        # 1D lattice in 2D ambient space; residual is the squared weighted
        # distance of the target to span(lattice), and cost - lattice-bound
        # improvement stays above it.
        from fractions import Fraction

        result = solve_diophantine([[2, 2]], [2])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [2, 1], [1, 3]
        )
        self.assertGreater(opt.orthogonal_residual, 0)
        # Every attainable cost is residual + a non-negative lattice term,
        # so the optimum must be at least the residual.
        self.assertGreaterEqual(Fraction(opt.cost), opt.orthogonal_residual)
        self.assertEqual(opt.common_denominator, int(opt.common_denominator))
        self.assertGreaterEqual(opt.common_denominator, 1)

    def test_unique_solution_residual_is_its_cost(self):
        result = solve_diophantine([[1, 0], [0, 1]], [5, -7])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [0, 0], [3, 4]
        )
        from fractions import Fraction

        self.assertEqual(opt.orthogonal_residual, Fraction(271))

    def test_no_floating_point_types(self):
        result = solve_diophantine([[2, 3, -1], [1, 1, 2]], [5, 4])
        opt = optimize_corrections(
            result.solution, result.homogeneous_basis, [1, 2, 3], [2, 3, 5]
        )
        from fractions import Fraction

        self.assertIsInstance(opt.cost, int)
        for row in opt.mu:
            for value in row:
                self.assertIsInstance(value, Fraction)
        for value in opt.gs_norm:
            self.assertIsInstance(value, Fraction)
        for row in opt.gram:
            for value in row:
                self.assertIsInstance(value, int)

    def test_big_integer_exactness(self):
        big = 10**80 + 7
        result = solve_diophantine([[2 * big, big]], [big])
        self.assertTrue(result.solvable)
        opt = optimize_corrections(
            result.solution,
            result.homogeneous_basis,
            [10**79, -2 * 10**79],
            [1, big],
        )
        rebuilt_cost = sum(
            opt.weights[i] * (opt.optimal[i] - opt.preferred[i]) ** 2 for i in range(2)
        )
        self.assertEqual(rebuilt_cost, opt.cost)
        self.assertIsInstance(opt.cost, int)
        # Heavy second weight pins dev_2 to its smallest integer value 1,
        # so the exact optimum cost is the 81-digit weight itself, carried
        # without any floating-point conversion.
        self.assertEqual(opt.cost, big)
        self.assertGreater(len(str(opt.cost)), 53)

    def test_non_positive_weight_rejected(self):
        result = solve_diophantine([[1, 1]], [0])
        with self.assertRaises(ValueError):
            optimize_corrections(
                result.solution, result.homogeneous_basis, [0, 0], [1, 0]
            )
        with self.assertRaises(ValueError):
            optimize_corrections(
                result.solution, result.homogeneous_basis, [0, 0], [1, -2]
            )

    def test_random_systems_match_brute_force(self):
        rng = random.Random(20260926)
        compared = 0
        for _ in range(300):
            m = rng.randint(1, 4)
            n = rng.randint(1, 4)
            matrix = [[rng.randint(-4, 4) for _ in range(n)] for _ in range(m)]
            x_seed = [rng.randint(-4, 4) for _ in range(n)]
            target = [
                sum(matrix[i][j] * x_seed[j] for j in range(n)) for i in range(m)
            ]
            result = solve_diophantine(matrix, target)
            if not result.solvable or len(result.homogeneous_basis) > 3:
                continue
            preferred = [rng.randint(-5, 5) for _ in range(n)]
            weights = [rng.randint(1, 5) for _ in range(n)]
            opt = optimize_corrections(
                result.solution,
                result.homogeneous_basis,
                preferred,
                weights,
            )
            # Only compare when the global minimizer is guaranteed inside
            # the brute-force box (its coordinates are the reported ones).
            if any(abs(z) > 9 for z in opt.optimal_coordinates):
                continue
            (best_cost, best_x), _ = brute_force(
                result.solution,
                result.homogeneous_basis,
                preferred,
                weights,
            )
            self.assertEqual(opt.cost, best_cost, (matrix, target, preferred, weights))
            self.assertEqual(opt.optimal, best_x)
            compared += 1
        self.assertGreater(compared, 50)


if __name__ == "__main__":
    unittest.main()
