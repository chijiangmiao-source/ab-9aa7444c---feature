"""Unit tests for the exact Diophantine solver."""

import random
import unittest
from fractions import Fraction

from app.diophantine import (
    extended_gcd,
    integer_kernel_basis,
    smith_normal_form,
    solve_diophantine,
)


def matmul(a, b):
    return [
        [sum(x * y for x, y in zip(row_a, col_b)) for col_b in zip(*b)]
        for row_a in a
    ]


def bareiss_det(matrix):
    """Exact fraction-free determinant (Bareiss algorithm)."""
    a = [list(row) for row in matrix]
    n = len(a)
    if n == 0:
        return 1
    sign = 1
    prev = 1
    for k in range(n - 1):
        if a[k][k] == 0:
            pivot = next((i for i in range(k + 1, n) if a[i][k] != 0), None)
            if pivot is None:
                return 0
            a[k], a[pivot] = a[pivot], a[k]
            sign = -sign
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                a[i][j] = (a[i][j] * a[k][k] - a[i][k] * a[k][j]) // prev
        prev = a[k][k]
        for i in range(k + 1, n):
            a[i][k] = 0
    return sign * a[n - 1][n - 1]


class SmithNormalFormTest(unittest.TestCase):
    def test_identity(self):
        snf = smith_normal_form([[1, 0], [0, 1]])
        self.assertEqual(snf.diagonal, [1, 1])
        self.assertEqual(snf.rank, 2)

    def test_zero_matrix(self):
        snf = smith_normal_form([[0, 0], [0, 0]])
        self.assertEqual(snf.rank, 0)
        self.assertEqual(snf.diagonal, [])

    def test_single_row_gcd(self):
        snf = smith_normal_form([[6, 10, 15]])
        self.assertEqual(snf.diagonal, [1])

    def test_single_column(self):
        snf = smith_normal_form([[4], [6], [10]])
        self.assertEqual(snf.diagonal, [2])

    def test_diagonal_chain(self):
        snf = smith_normal_form([[2, 0], [0, 3]])
        self.assertEqual(snf.diagonal, [1, 6])

    def test_big_integers(self):
        big = 2**70 + 12345
        snf = smith_normal_form([[2 * big, big], [big, 3 * big]])
        self.assertEqual(snf.diagonal, [big, 5 * big])

    def test_random_small_matrices(self):
        rng = random.Random(20260925)
        for _ in range(60):
            m = rng.randint(1, 5)
            n = rng.randint(1, 5)
            a = [[rng.randint(-9, 9) for _ in range(n)] for _ in range(m)]
            snf = smith_normal_form(a)
            # D == U @ A @ V
            self.assertEqual(matmul(matmul(snf.u, a), snf.v), snf.d)
            # diagonal shape and divisibility chain
            for i in range(snf.rank):
                self.assertGreater(snf.d[i][i], 0)
                self.assertEqual(snf.d[i][i], snf.diagonal[i])
                if i + 1 < snf.rank:
                    self.assertEqual(snf.d[i + 1][i + 1] % snf.d[i][i], 0)
            for i in range(m):
                for j in range(n):
                    if i != j or i >= snf.rank:
                        self.assertEqual(snf.d[i][j], 0)
            # U and V are unimodular (invertible over the integers)
            self.assertEqual(abs(bareiss_det(snf.u)), 1)
            self.assertEqual(abs(bareiss_det(snf.v)), 1)


class SolveDiophantineTest(unittest.TestCase):
    def test_unique_solution_with_big_coefficients(self):
        matrix = [[9007199254740993, 1, 0], [1, 3, 1], [0, 2, 4]]
        target = [18014398509481989, 12, 10]
        result = solve_diophantine(matrix, target)
        self.assertTrue(result.solvable)
        self.assertEqual(result.solution, [2, 3, 1])

    def test_non_divisible_obstruction(self):
        result = solve_diophantine([[2, 0], [0, 2]], [3, 4])
        self.assertFalse(result.solvable)
        obstruction = result.obstruction
        self.assertEqual(obstruction.kind, "non_divisible")
        self.assertEqual(obstruction.pivot, 2)
        self.assertEqual(obstruction.transformed_target, 3)
        self.assertEqual(obstruction.remainder, 1)

    def test_zero_row_obstruction(self):
        result = solve_diophantine([[1, 1], [2, 2]], [1, 3])
        self.assertFalse(result.solvable)
        self.assertEqual(result.obstruction.kind, "zero_row")
        self.assertNotEqual(result.obstruction.transformed_target, 0)

    def test_big_exact_solution(self):
        result = solve_diophantine([[2**62]], [2**63])
        self.assertTrue(result.solvable)
        self.assertEqual(result.solution, [2])

    def test_big_non_divisible(self):
        result = solve_diophantine([[2**62]], [2**62 + 1])
        self.assertFalse(result.solvable)
        self.assertEqual(result.obstruction.kind, "non_divisible")
        self.assertEqual(result.obstruction.pivot, 2**62)
        self.assertEqual(result.obstruction.remainder, 1)

    def test_underdetermined_gcd_case(self):
        # 2x + 3y = 1 is solvable because gcd(2, 3) = 1; a naive
        # pivot-divides-target check on the row echelon form would
        # wrongly reject it.
        result = solve_diophantine([[2, 3]], [1])
        self.assertTrue(result.solvable)
        x, y = result.solution
        self.assertEqual(2 * x + 3 * y, 1)

    def test_random_consistent_systems(self):
        rng = random.Random(7)
        for _ in range(50):
            m = rng.randint(1, 5)
            n = rng.randint(1, 5)
            a = [[rng.randint(-8, 8) for _ in range(n)] for _ in range(m)]
            x0 = [rng.randint(-5, 5) for _ in range(n)]
            b = [sum(aij * xj for aij, xj in zip(row, x0)) for row in a]
            result = solve_diophantine(a, b)
            self.assertTrue(result.solvable)
            reproduced = [
                sum(aij * xj for aij, xj in zip(row, result.solution)) for row in a
            ]
            self.assertEqual(reproduced, b)
            for vector in result.homogeneous_basis:
                self.assertEqual(
                    [sum(aij * vj for aij, vj in zip(row, vector)) for row in a],
                    [0] * m,
                )

    def test_u_row_reconstructs_transformed_target(self):
        matrix = [[4, 2], [2, 4]]
        target = [1, 1]
        result = solve_diophantine(matrix, target)
        self.assertFalse(result.solvable)
        obstruction = result.obstruction
        reconstructed = sum(
            coefficient * value
            for coefficient, value in zip(obstruction.u_row, target)
        )
        self.assertEqual(reconstructed, obstruction.transformed_target)


class IntegerKernelBasisTest(unittest.TestCase):
    def test_extended_gcd_bezout(self):
        for a, b in ((48, 18), (-48, 18), (0, 7), (7, 0), (-13, -17), (2**80, 3**60)):
            u, v, g = extended_gcd(a, b)
            self.assertGreater(g, 0)
            self.assertEqual(u * a + v * b, g)

    def test_basic_kernels(self):
        kernel_1d = integer_kernel_basis([[2, 2]])
        self.assertEqual(len(kernel_1d), 1)
        self.assertIn(kernel_1d[0], ([-1, 1], [1, -1]))
        self.assertEqual(integer_kernel_basis([[2, 0], [0, 4]]), [])
        # zero rows: kernel is the whole space
        k3 = integer_kernel_basis([[0, 0, 0], [0, 0, 0]])
        self.assertEqual(sorted(k3), [[0, 0, 1], [0, 1, 0], [1, 0, 0]])

    def test_kernel_vectors_are_in_kernel(self):
        rng = random.Random(123)
        for _ in range(100):
            m = rng.randint(1, 4)
            n = rng.randint(1, 5)
            matrix = [[rng.randint(-12, 12) for _ in range(n)] for _ in range(m)]
            kernel = integer_kernel_basis(matrix)
            for vector in kernel:
                for row in matrix:
                    self.assertEqual(sum(a * b for a, b in zip(row, vector)), 0)

    def test_kernel_equals_smith_lattice(self):
        """The gcd-built kernel is the full integer kernel (index 1), not a sublattice."""
        rng = random.Random(456)
        checked = 0
        for _ in range(300):
            m = rng.randint(1, 4)
            n = rng.randint(1, 5)
            matrix = [[rng.randint(-9, 9) for _ in range(n)] for _ in range(m)]
            snf = smith_normal_form(matrix)
            k = n - snf.rank
            smith_kernel = [
                [snf.v[i][j] for i in range(n)] for j in range(snf.rank, n)
            ]
            kernel = integer_kernel_basis(matrix)
            self.assertEqual(len(kernel), k)
            if k == 0:
                continue
            # Express each new-kernel vector in Smith-kernel coordinates;
            # the coordinate matrix must be unimodular (det +/- 1, integral).
            coordinates = self._coordinates(kernel, smith_kernel)
            self.assertTrue(all(x.denominator == 1 for row in coordinates for x in row))
            self.assertEqual(abs(self._determinant(coordinates)), 1)
            checked += 1
        self.assertGreater(checked, 20)

    @staticmethod
    def _coordinates(vectors, basis):
        k = len(basis)
        n = len(basis[0])
        # Fraction Gaussian elimination on the (full column rank) basis.
        cols = [[Fraction(basis[j][i]) for j in range(k)] for i in range(n)]
        result = []
        for target in vectors:
            mat = [row[:] for row in cols]
            rhs = [Fraction(x) for x in target]
            rank = 0
            for c in range(k):
                pivot = next((r for r in range(rank, n) if mat[r][c] != 0), None)
                if pivot is None:
                    continue
                mat[rank], mat[pivot] = mat[pivot], mat[rank]
                rhs[rank], rhs[pivot] = rhs[pivot], rhs[rank]
                divisor = mat[rank][c]
                mat[rank] = [v / divisor for v in mat[rank]]
                rhs[rank] /= divisor
                for r in range(n):
                    if r != rank and mat[r][c] != 0:
                        factor = mat[r][c]
                        rhs[r] -= factor * rhs[rank]
                        mat[r] = [mat[r][j] - factor * mat[rank][j] for j in range(k)]
                rank += 1
            result.append(rhs[:k])
        return result

    @staticmethod
    def _determinant(matrix):
        mat = [row[:] for row in matrix]
        sign = 1
        for c in range(len(mat)):
            pivot = next((r for r in range(c, len(mat)) if mat[r][c] != 0), None)
            if pivot is None:
                return Fraction(0)
            if pivot != c:
                mat[c], mat[pivot] = mat[pivot], mat[c]
                sign = -sign
            for r in range(c + 1, len(mat)):
                factor = mat[r][c] / mat[c][c]
                for j in range(len(mat)):
                    mat[r][j] -= factor * mat[c][j]
        value = Fraction(sign)
        for i in range(len(mat)):
            value *= mat[i][i]
        return value


if __name__ == "__main__":
    unittest.main()
