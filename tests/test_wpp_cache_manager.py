"""CacheManager resilience tests isolated from the application's services."""

import ast
import json
import logging
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CacheManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "services" / "etl_parser" / "wpp_ddd_parser.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        cache_class = next(node for node in tree.body
                           if isinstance(node, ast.ClassDef) and node.name == "CacheManager")
        cls.logger = logging.getLogger("test_wpp_cache_manager")
        cls.namespace = {
            "os": os,
            "json": json,
            "log": cls.logger,
            "emit_and_log": lambda msg: None,
        }
        exec(compile(ast.Module(body=[cache_class], type_ignores=[]), str(source), "exec"), cls.namespace)
        cls.CacheManager = cls.namespace["CacheManager"]

    def manager(self, folder):
        os.environ["tmp"] = folder
        return self.CacheManager(136616, "WinT", "Netwtw10.pdb")

    @property
    def cache_path(self):
        return Path(os.environ["tmp"]) / "wpp_ddd_parser_cache.json"

    def test_empty_cache_file_is_a_miss_not_a_crash(self):
        with TemporaryDirectory() as folder:
            mgr = self.manager(folder)
            self.cache_path.write_text("", encoding="utf-8")
            with self.assertLogs(self.logger, level="WARNING"):
                self.assertIsNone(mgr.get_binary_path())

    def test_corrupt_cache_file_is_a_miss_not_a_crash(self):
        with TemporaryDirectory() as folder:
            mgr = self.manager(folder)
            self.cache_path.write_text("{not valid json", encoding="utf-8")
            with self.assertLogs(self.logger, level="WARNING"):
                self.assertIsNone(mgr.get_binary_path())

    def test_non_object_cache_file_is_a_miss(self):
        with TemporaryDirectory() as folder:
            mgr = self.manager(folder)
            self.cache_path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertLogs(self.logger, level="WARNING"):
                self.assertIsNone(mgr.get_binary_path())

    def test_store_overwrites_corrupt_cache_and_round_trips(self):
        with TemporaryDirectory() as folder:
            binary = Path(folder) / "Netwtw10.pdb"
            binary.write_bytes(b"fake")
            mgr = self.manager(folder)
            self.cache_path.write_text("", encoding="utf-8")
            with self.assertLogs(self.logger, level="WARNING"):
                mgr.store_binary_path(str(binary))
            stored = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["136616"]["WinT"]["Netwtw10.pdb"], str(binary))
            self.assertEqual(mgr.get_binary_path(), str(binary))

    def test_valid_cache_but_missing_binary_is_a_miss(self):
        with TemporaryDirectory() as folder:
            mgr = self.manager(folder)
            self.cache_path.write_text(json.dumps(
                {"136616": {"WinT": {"Netwtw10.pdb": str(Path(folder) / "gone.pdb")}}}
            ), encoding="utf-8")
            self.assertIsNone(mgr.get_binary_path())


if __name__ == "__main__":
    unittest.main()
