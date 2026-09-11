"""The engine as a JSON-RPC 2.0 server over stdin and stdout.

    python -m schematic.serve            # serve until stdin closes or engine.shutdown
    python -m schematic.serve --schema   # print the protocol's JSON Schema

The transport is the Language Server Protocol's: ``Content-Length`` framed
messages on stdin and stdout, stderr for logs. Long requests stay open until
they finish; while they run, ``job/progress`` and ``job/log`` notifications
carry the request's id, and ``$/cancelRequest`` ends the LOOM process the
request is waiting on and answers it with the cancelled error.

Errors a person can act on are code -32000 with ``data: {kind, detail,
hint}``: ``hint`` is the sentence the engine already raises, ``detail`` says
where. Bad parameters are -32602 with the same shape and ``kind: "params"``.

``protocol/v1.json`` is the contract; the desktop app generates its types
from it. A change here is a change there.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import sys
import traceback
from importlib import resources
from pathlib import Path
from typing import IO, Any, Callable

from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.exceptions import (JsonRpcException, JsonRpcInvalidParams,
                                      JsonRpcRequestCancelled)
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

from . import __version__, config, export, feeds, loom, pipeline
from .crs import to_mercator
from .linegraph import LineGraph
from .render import octilinearity

log = logging.getLogger(__name__)

PROTOCOL = 1
ENGINE_ERROR = -32000

# The same patterns as protocol/v1.json; test_serve checks they agree.
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# An exception's kind is the module that raised it, since that is where the
# sentence for a person was written.
KIND_BY_MODULE = {"feeds": "feed", "pipeline": "feed", "schedule": "schedule",
                  "export": "export", "loom": "loom"}

Progress = pipeline.Progress
Work = Callable[[loom.Job, Progress], Any]


def schema() -> dict[str, Any]:
    """The protocol's JSON Schema, as shipped in the package."""
    text = resources.files("schematic.protocol").joinpath("v1.json").read_text(encoding="utf-8")
    return json.loads(text)


# ---------------------------------------------------------------------- errors

class EngineError(JsonRpcException):
    """Code -32000: something a person can act on, with the sentence to show them."""

    def __init__(self, kind: str, hint: str, detail: str | None = None) -> None:
        super().__init__(message=hint, code=ENGINE_ERROR,
                         data={"kind": kind, "detail": detail or hint, "hint": hint})


def invalid_params(hint: str) -> JsonRpcInvalidParams:
    return JsonRpcInvalidParams(message=hint,
                                data={"kind": "params", "detail": hint, "hint": hint})


def classify(exc: BaseException) -> EngineError:
    """Turn an exception out of the pipeline into an error a client can show."""
    frames = traceback.extract_tb(exc.__traceback__)
    if isinstance(exc, loom.LoomError):
        kind = "loom"
    elif isinstance(exc, OSError):
        kind = "io"
    else:
        kind = "engine"
        for frame in reversed(frames):
            path = Path(frame.filename)
            if path.parent == config.PACKAGE_DIR and path.stem in KIND_BY_MODULE:
                kind = KIND_BY_MODULE[path.stem]
                break

    summary = "".join(traceback.format_exception_only(exc)).strip()
    where = f" ({Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    detail = summary + where

    if isinstance(exc, (ValueError, RuntimeError)) and str(exc):
        hint = str(exc)
    elif isinstance(exc, OSError) and exc.strerror:
        hint = f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    else:
        hint = "The engine hit an error it did not expect; the detail says where."
    return EngineError(kind, hint, detail)


# ------------------------------------------------------------------ parameters

def _object(method: str, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise invalid_params(f"{method} takes an object of parameters")
    return dict(params)


def _no_params(method: str, params: Any) -> None:
    if params not in (None, {}):
        raise invalid_params(f"{method} takes no parameters")


def _no_extra(method: str, left: dict[str, Any]) -> None:
    if left:
        raise invalid_params(f"{method} does not take {', '.join(sorted(left))}")


def _feed_key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_PATTERN.match(value):
        raise invalid_params("key must be a feed key: lower-case letters, digits and hyphens")
    if value not in feeds.FEEDS:
        raise EngineError("feed", f"{value!r} is not a registered feed")
    return value


def _date(value: Any) -> dt.date:
    if value is None:
        raise invalid_params(
            "date is required: the service day to draw, as YYYY-MM-DD. The engine "
            "never picks one, because its choice would depend on the day you asked.")
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        raise invalid_params("date must be a calendar day as YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise invalid_params(f"{value} is not a calendar day") from None


def _token(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_PATTERN.match(value):
        raise invalid_params("out must be a folder name: letters, digits, dot, underscore "
                             "and hyphen, not starting with a dot, never a path")
    return value


def _flag(left: dict[str, Any], name: str) -> bool:
    value = left.pop(name, False)
    if not isinstance(value, bool):
        raise invalid_params(f"{name} must be true or false")
    return value


def _positive(left: dict[str, Any], name: str, default: float) -> float:
    value = left.pop(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise invalid_params(f"{name} must be a number above zero")
    return float(value)


def _strings(left: dict[str, Any], name: str) -> list[str] | None:
    value = left.pop(name, None)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise invalid_params(f"{name} must be a list of strings")
    return value


# ------------------------------------------------------------------- the server

class EngineEndpoint(Endpoint):
    """The engine's methods, plus what the base class lacks: the id of the
    request being handled, and a registry of running jobs for cancellation."""

    def __init__(self, consumer: Callable[[dict[str, Any]], None], max_workers: int = 4) -> None:
        self.jobs: dict[Any, loom.Job] = {}
        self.stopping = False
        self._request_id: Any = None
        super().__init__({
            "engine.info": self.engine_info,
            "engine.shutdown": self.engine_shutdown,
            "graph.build": self.graph_build,
            "map.build": self.map_build,
        }, consumer, max_workers=max_workers)

    @property
    def methods(self) -> list[str]:
        return list(self._dispatcher)

    # The base class calls handlers with the params alone; the id is needed
    # for progress and cancellation, so keep it while the handler runs. Only
    # the reader thread handles requests, so one slot is enough.
    def _handle_request(self, msg_id: Any, method: str, params: Any) -> None:
        self._request_id = msg_id
        try:
            super()._handle_request(msg_id, method, params)
        finally:
            self._request_id = None

    # The base class can only cancel a job that has not started. Ours kills
    # the LOOM process; the worker then raises and the request is answered
    # with the cancelled error through the usual callback.
    def _handle_cancel_notification(self, msg_id: Any) -> None:
        job = self.jobs.get(msg_id)
        if job is None:
            log.warning("cancel for a request that is not running: %s", msg_id)
            return
        log.info("cancelling request %s", msg_id)
        job.cancel()

    def close(self) -> None:
        """Cancel whatever is running and stop the workers. No process outlives this."""
        for job in list(self.jobs.values()):
            job.cancel()
        self.shutdown()

    def _job(self, work: Work) -> Callable[[], Any]:
        """A long method: a Job registered under the request's id, run on a worker.

        The base class runs the returned callable in its pool and answers the
        request with what it returns or raises.
        """
        msg_id = self._request_id
        job = loom.Job(log=lambda line: self.notify(
            "job/log", {"id": msg_id, "level": "info", "line": line}))
        self.jobs[msg_id] = job

        def progress(stage: str, fraction: float, message: str) -> None:
            self.notify("job/progress", {"id": msg_id, "stage": stage,
                                         "fraction": round(fraction, 4), "message": message})

        def run() -> Any:
            try:
                if job.cancelled:
                    raise JsonRpcRequestCancelled()
                with loom.cancellable(job):
                    return work(job, progress)
            except loom.Cancelled:
                raise JsonRpcRequestCancelled() from None
            except JsonRpcException:
                raise
            except Exception as exc:
                if job.cancelled:
                    raise JsonRpcRequestCancelled() from None
                log.exception("request %s failed", msg_id)
                raise classify(exc) from exc
            finally:
                self.jobs.pop(msg_id, None)

        return run

    # -- methods --

    def engine_info(self, params: Any = None) -> dict[str, Any]:
        _no_params("engine.info", params)
        return {
            "engine": __version__,
            "protocol": PROTOCOL,
            "python": platform.python_version(),
            "loom": {"commit": None, "backend": "docker"},
            "ffmpeg": shutil.which(export.ffmpeg_path()),
            "home": str(config.home()),
        }

    def engine_shutdown(self, params: Any = None) -> dict[str, Any]:
        _no_params("engine.shutdown", params)
        for job in list(self.jobs.values()):
            job.cancel()
        self.stopping = True
        return {"ok": True}

    def graph_build(self, params: Any) -> Callable[[], Any]:
        left = _object("graph.build", params)
        key = _feed_key(left.pop("key", None))
        force = _flag(left, "force")
        _no_extra("graph.build", left)

        def work(job: loom.Job, progress: Progress) -> dict[str, Any]:
            paths = pipeline.schematize(key, force=force, progress=progress)
            octi = LineGraph.from_geojson(paths["octi"])
            pipeline.require_edges(key, octi)
            stages = {stage: _stage_summary(LineGraph.from_geojson(path))
                      for stage, path in paths.items()}
            ok, total = octilinearity(octi.reproject(to_mercator))
            stages["octi"]["octilinear"] = ok / total if total else 0.0
            return {"stages": stages, "paths": {s: str(p) for s, p in paths.items()}}

        return self._job(work)

    def map_build(self, params: Any) -> Callable[[], Any]:
        left = _object("map.build", params)
        key = _feed_key(left.pop("key", None))
        date = _date(left.pop("date", None))
        out = _token(left.pop("out")) if "out" in left else None
        width = _positive(left, "width", 1800.0)
        line_order = _strings(left, "line_order")
        force = _flag(left, "force")
        _no_extra("map.build", left)
        folder = config.out_dir() / out if out else None

        def work(job: loom.Job, progress: Progress) -> dict[str, Any]:
            result = pipeline.run(key, date=date, width=width, line_order=line_order,
                                  force=force, out_dir=folder, progress=progress)
            where = folder or config.out_dir()
            return {
                "date": result.date.isoformat(),
                "files": {"svg": str(where / f"{key}.svg"),
                          "html": str(where / f"{key}.html"),
                          "positions": str(where / f"{key}.positions.json")},
                "summary": result.summary(),
                "diagnostics": _diagnostics(result),
            }

        return self._job(work)


def _stage_summary(graph: LineGraph) -> dict[str, Any]:
    stations = len(graph.stations)
    return {"nodes": len(graph.nodes), "stations": stations,
            "junctions": len(graph.nodes) - stations, "edges": len(graph.edges),
            "lines": list(graph.labels)}


def _diagnostics(result: pipeline.Result) -> dict[str, Any]:
    """``Result.summary()`` as data, in the same order."""
    ok, total = octilinearity(result.graph)
    match, anim = result.match, result.animation
    stations = len(result.graph.stations)
    return {
        "stations": stations,
        "junctions": len(result.graph.nodes) - stations,
        "edges": len(result.graph.edges),
        "lines": list(result.graph.labels),
        "octilinear": ok / total if total else 0.0,
        "stops": {"matched": len(match.stop_to_node),
                  "total": len(match.stop_to_node) + len(match.unmatched),
                  "by": {"station_id": match.by_id, "parent_station": match.by_parent,
                         "name": match.by_name},
                  "unmatched": list(match.unmatched[:8])},
        "trips": {"total": len(result.trips), "paths": len(anim.paths),
                  "unrouted": len(anim.unrouted)},
        "degraded": {"skipped_calls": anim.trips_with_skipped_calls,
                     "borrowed_track": anim.trips_with_borrowed_track},
        "labels_dropped": len(result.render.dropped_labels),
        "peak_concurrent": result.peak_concurrent(),
    }


# ------------------------------------------------------------------- transport

# Codes the library logs with a full traceback at ERROR although they are the
# protocol working as designed: bad parameters, an unknown method, a feed
# error with its hint, a cancellation. Anything else it logs still shows.
EXPECTED_CODES = frozenset({-32601, -32602, ENGINE_ERROR, JsonRpcRequestCancelled.CODE})


class _QuietExpectedErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, JsonRpcException) and exc.code in EXPECTED_CODES)


def serve(rfile: IO[bytes], wfile: IO[bytes]) -> None:
    """Answer requests read from ``rfile`` on ``wfile`` until it closes or
    ``engine.shutdown`` is answered. Nothing the server started outlives it."""
    logging.getLogger("pylsp_jsonrpc.endpoint").addFilter(_QuietExpectedErrors())
    writer = JsonRpcStreamWriter(wfile)
    endpoint = EngineEndpoint(writer.write)
    reader = JsonRpcStreamReader(rfile)

    def consume(message: dict[str, Any]) -> None:
        endpoint.consume(message)
        if endpoint.stopping:
            reader.close()

    try:
        reader.listen(consume)
    finally:
        endpoint.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schematic.serve",
        description="Serve the engine over JSON-RPC 2.0 on stdin and stdout.")
    parser.add_argument("--schema", action="store_true",
                        help="print the protocol's JSON Schema and exit")
    args = parser.parse_args(argv)
    if args.schema:
        sys.stdout.write(json.dumps(schema(), indent=2) + "\n")
        return 0

    level = os.environ.get("SCHEMATIC_LOG", "info").upper()
    if level not in logging.getLevelNamesMapping():
        level = "INFO"
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    # stdout is the protocol stream. Anything else the engine prints -- the
    # site's notes, a stray debug line -- goes to stderr with the logs rather
    # than into the middle of a frame.
    sys.stdout = sys.stderr
    log.info("engine %s, protocol %d, home %s", __version__, PROTOCOL, config.home())
    serve(rfile, wfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
