import unittest

from stats import largest, mean


class MeanTests(unittest.TestCase):
    def test_mean_of_three(self):
        self.assertAlmostEqual(mean([1, 2, 3]), 2.0)

    def test_mean_of_two(self):
        self.assertAlmostEqual(mean([4, 8]), 6.0)

    def test_mean_of_one(self):
        self.assertAlmostEqual(mean([7]), 7.0)


class LargestTests(unittest.TestCase):
    def test_largest_in_the_middle(self):
        self.assertEqual(largest([3, 9, 4]), 9)

    def test_largest_first(self):
        self.assertEqual(largest([9, 1]), 9)

    def test_empty_sequence(self):
        self.assertIsNone(largest([]))


if __name__ == "__main__":
    unittest.main()
