import unittest

from service.routes import ROUTES
from service.store import Ledger


class RouteTests(unittest.TestCase):
    def test_health_route_exists(self):
        self.assertIn("GET /health", ROUTES)

    def test_route_count(self):
        self.assertEqual(len(ROUTES), 4)


class StoreTests(unittest.TestCase):
    def test_total_sums_entries(self):
        ledger = Ledger()
        ledger.add(3)
        ledger.add(4)
        self.assertEqual(ledger.total(), 7)


if __name__ == "__main__":
    unittest.main()
