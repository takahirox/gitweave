"""Attempt-scoped execution events, independent of node inputs and provider protocols."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import threading


output_callback = ContextVar("gitweave_output_callback", default=None)


class JSONEventSink:
    """Write complete, flushed JSON lines even when attempts emit concurrently."""
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def __call__(self, event):
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self.lock:
            self.stream.write(line)
            self.stream.flush()


class AttemptEvents:
    def __init__(self, sink, record):
        self.sink = sink
        self.identity = {key: record[key] for key in ("run_id", "node_id", "instance_id", "attempt")}

    def emit(self, kind, **fields):
        if self.sink is not None:
            event = dict(self.identity, type=kind, timestamp=datetime.now(timezone.utc).isoformat(), **fields)
            try:
                self.sink(event)
            except (OSError, ValueError):
                # Losing an observer must not lose execution output or provenance.
                pass

    def output(self, stream, text):
        self.emit("node_output", stream=stream, text=text)


@contextmanager
def observe_output(callback):
    """Bind output to this worker's attempt; process readers capture the callback."""
    token = output_callback.set(callback)
    try:
        yield
    finally:
        output_callback.reset(token)
