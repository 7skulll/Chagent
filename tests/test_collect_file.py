import unittest
from uuid import uuid4

from chagent.tools.collect_file import source_path


class CollectFilePathTests(unittest.TestCase):
    def test_only_own_scratch_and_cache_are_accepted(self):
        run = uuid4()
        self.assertEqual(source_path(f"/tmp/chagent-run-{run}/out.bin", run),
                         f"/tmp/chagent-run-{run}/out.bin")
        self.assertEqual(source_path("/cache/build/out.jar", run), "/cache/build/out.jar")
        for path in ("/proc/self/environ", "/cache/../etc/passwd", "/cache//out", "/cache/./out",
                     f"/tmp/chagent-run-{uuid4()}/out", "/cache", "relative.bin"):
            with self.subTest(path=path), self.assertRaises((ValueError, PermissionError)):
                source_path(path, run)
