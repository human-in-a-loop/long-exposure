"""Shared file primitives for the staged end-of-run agents.

The final reporter (`reporting.py`) and final auditor (`auditing.py`) run the
same staged protocol over different artifacts, so they need the same
building blocks: atomic writes, "did this stage actually change the file"
signatures, commit markers, delta-baseline detection, and run-mode
sidecars. Those helpers previously existed as verbatim copies in both
modules (plus a third atomic-write in `exploration.py`), which is exactly
the kind of duplication that drifts silently. They live here now; the
callers keep their private aliases so no call site changed.

Stdlib only, and deliberately free of long-exposure imports beyond
`limits`, so any module can import it without circularity.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from long_exposure.limits import DELTA_DETECT_MIN_BYTES


def file_signature(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) for change detection, or None when absent.

    The staged agents compare this before and after a stage to tell "the
    agent wrote the file" from "the file was already there".
    """
    try:
        st = Path(path).stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


_write_seq = itertools.count()


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via a unique sibling temp file + os.replace.

    The temp name carries pid, thread id and a process-local counter, so
    neither two processes (concurrent fan-out clones, a run plus a
    standalone re-render) nor two threads (`launch --manager` polls while
    the loop runs) can collide on one `.tmp` path. A shared temp name is
    not merely untidy: writer A's `os.replace` would publish whatever
    writer B had flushed so far, i.e. a truncated artifact under the real
    filename. The name still ends in `.tmp` so the curator's hard-exclude
    suffixes catch anything left behind by a crash mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}"
        f".{next(_write_seq)}.tmp"
    )
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray temp behind on failure (including
        # KeyboardInterrupt mid-write, which the run-control path can raise).
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def marker_metadata(marker_path: Path) -> dict | None:
    """Parsed commit-marker JSON, `{}` when unreadable, None when absent.

    The three-way return is load-bearing: None means "no prior committed
    pass" (fresh mode), while `{}` means "a pass committed but the marker is
    unparsable" (still a delta baseline).
    """
    marker_path = Path(marker_path)
    if not marker_path.exists():
        return None
    try:
        data = json.loads(marker_path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def committed_baseline(path: Path, marker_path: Path) -> tuple[bool, str, float | None]:
    """Detect a delta baseline, preferring explicit commit markers.

    Returns `(delta_mode, detection_source, boundary_ts)`. The legacy
    size heuristic covers workspaces written before markers existed.
    """
    path, marker_path = Path(path), Path(marker_path)
    marker = marker_metadata(marker_path)
    if marker is not None and path.exists():
        ts = marker.get("committed_at")
        try:
            boundary = (
                datetime.fromisoformat(str(ts)).timestamp()
                if ts else marker_path.stat().st_mtime
            )
        except (OSError, ValueError):
            boundary = None
        return True, "marker", boundary
    try:
        if path.exists() and path.stat().st_size > DELTA_DETECT_MIN_BYTES:
            return True, "legacy_size", None
    except OSError:
        pass
    return False, "none", None


def write_commit_marker(
    marker_path: Path,
    *,
    run_id: str | None,
    mode: str,
    token_count: int,
    label: str = "Commit marker",
) -> None:
    """Record that this pass produced committed output. Best-effort."""
    payload = {
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": mode,
        "input_tokens": int(token_count),
    }
    try:
        atomic_write_text(marker_path, json.dumps(payload, indent=2) + "\n")
    except OSError as e:
        print(f"[long-exposure]   {label} write skipped: {e}", flush=True)


def write_run_mode(path: Path, payload: dict) -> None:
    """Write the stage's run-mode sidecar. Best-effort, never raises."""
    try:
        atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def estimate_delta_tokens(candidates: Iterable[Path | str], boundary_ts: float | None) -> int:
    """~tokens in files modified after `boundary_ts` (4 chars per token).

    `boundary_ts=None` means no usable baseline, so nothing counts as new.
    """
    if boundary_ts is None:
        return 0
    chars = 0
    for raw in candidates:
        p = Path(raw)
        try:
            if p.stat().st_mtime > boundary_ts:
                chars += len(p.read_text())
        except OSError:
            continue
    return chars // 4
