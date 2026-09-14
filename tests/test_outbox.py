import unittest

from orchestrator.outbox import OutboxDispatcher
from orchestrator.workflow_repository import SqlWorkflowRepository


class _Sink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class OutboxTests(unittest.TestCase):
    def test_dispatch_marks_only_published_events(self):
        repository = SqlWorkflowRepository("sqlite://")
        try:
            repository.create_run("p1", "ship", run_id="r1")
            sink = _Sink()
            dispatcher = OutboxDispatcher(repository, sink)
            self.assertEqual(dispatcher.dispatch_once(), 1)
            self.assertEqual(len(sink.events), 1)
            self.assertEqual(dispatcher.dispatch_once(), 0)
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
