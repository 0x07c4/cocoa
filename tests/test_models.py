from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.models import EventKind, event, to_jsonable


class ModelTests(unittest.TestCase):
    def test_event_is_jsonable(self) -> None:
        runtime_event = event(
            EventKind.THREAD_STARTED,
            thread_id="thr_test",
            payload={"path": Path("/tmp/example")},
        )

        encoded = to_jsonable(runtime_event)

        self.assertEqual(encoded["kind"], "thread_started")
        self.assertEqual(encoded["thread_id"], "thr_test")
        self.assertEqual(encoded["payload"]["path"], "/tmp/example")


if __name__ == "__main__":
    unittest.main()
