"""Transactional outbox dispatch with at-least-once sink delivery."""
from __future__ import annotations

import threading
from typing import Any, Mapping, Protocol

from orchestrator.observability import correlated_span


class EventSink(Protocol):
    def publish(self, event: Mapping[str, Any]) -> None: ...


class OpenTelemetryEventSink:
    def publish(self, event: Mapping[str, Any]) -> None:
        payload = dict(event.get("payload_json") or {})
        body = dict(payload.get("payload") or {})
        with correlated_span(
            "outbox.publish",
            run_id=str(event.get("run_id")),
            work_item_id=(
                str(body["work_item_id"]) if body.get("work_item_id") else None
            ),
        ) as span:
            span.add_event(
                str(event.get("event_type")),
                {
                    "event_id": str(event.get("event_id")),
                    "actor": str(payload.get("actor", "system")),
                },
            )


class OutboxDispatcher:
    def __init__(self, repository, sink: EventSink):
        self.repository = repository
        self.sink = sink

    def dispatch_once(self, limit: int = 200) -> int:
        published = 0
        for event in self.repository.list_outbox(limit=limit):
            self.sink.publish(event)
            self.repository.mark_outbox_published([event["id"]])
            published += 1
        return published

    def run_forever(
        self,
        *,
        poll_interval: float = 1.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            if not self.dispatch_once():
                stop_event.wait(poll_interval)
