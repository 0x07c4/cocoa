from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.workspace import WorkspaceScope


class WorkspaceTests(unittest.TestCase):
    def test_workspace_inspect_ignores_heavy_dirs(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "src").mkdir()
            (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
            (tmp_path / "node_modules").mkdir()
            (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")

            entries = WorkspaceScope(tmp_path).inspect(".")

            self.assertEqual([entry.path for entry in entries], ["src/main.py"])

    def test_workspace_rejects_path_escape(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            scope = WorkspaceScope(Path(tmpdir))

            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                scope.resolve("..")

    def test_workspace_rejects_direct_ignored_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "node_modules").mkdir()
            (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "path is ignored"):
                WorkspaceScope(tmp_path).inspect("node_modules/dep.js")

    def test_workspace_rejects_non_positive_max_entries(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "max_entries"):
                WorkspaceScope(Path(tmpdir)).inspect(".", max_entries=0)


if __name__ == "__main__":
    unittest.main()
