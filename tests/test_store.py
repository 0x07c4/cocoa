from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.models import EventKind, event
from cocoa.store import JsonlStore


class StoreTests(unittest.TestCase):
    def test_store_appends_and_reads_events(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore(tmp_path / ".cocoa")
            store.append(event(EventKind.THREAD_STARTED, thread_id="thr_test"))

            rows = store.read_thread("thr_test")
            logs = store.list_threads()

            self.assertEqual(rows[0]["kind"], "thread_started")
            self.assertEqual(logs[0].thread_id, "thr_test")
            self.assertEqual(logs[0].event_count, 1)

    def test_store_rejects_path_escape_thread_id(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            store = JsonlStore(Path(tmpdir) / ".cocoa")

            with self.assertRaisesRegex(ValueError, "invalid thread_id"):
                store.thread_path("../outside")


if __name__ == "__main__":
    unittest.main()
