"""Each turn keeps its own copy of the files it handed over through deliver_files."""

from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.worker import Worker
from raven.config.raven import RavenConfig
from raven.config.schema import Config


def delivered(path, phase="start"):
    return {
        "kind": "runner.event",
        "event_type": "ToolEvent",
        "event": {"name": "deliver_files", "phase": phase, "arguments": {"files": [{"path": str(path)}]}},
    }


def test_delivered_files_are_copied_per_turn_and_later_rewrites_do_not_touch_the_copy(tmp_path):
    worker = Worker(Baseline(Config(), RavenConfig(), tmp_path), tmp_path / "worker")
    page = tmp_path / "trip.html"
    page.write_text("<h1>v0</h1>")
    records = [
        delivered(page),
        delivered(page, phase="end"),
        delivered(tmp_path / "missing.html"),
        {"kind": "runner.event"},
    ]
    first = worker._keep_deliverables("turn-1", records)
    page.write_text("<h1>v1</h1>")
    second = worker._keep_deliverables("turn-2", [delivered("trip.html")])
    assert first == (str(worker.root / "deliverables" / "turn-1" / "trip.html"),)
    assert open(first[0]).read() == "<h1>v0</h1>" and open(second[0]).read() == "<h1>v1</h1>"
    assert worker._keep_deliverables("turn-3", []) == ()
