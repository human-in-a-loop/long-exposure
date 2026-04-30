"""Per-backend renderer modules for the unified `figure` CLI.

Each renderer is a thin wrapper: locate the binary or import the
library, invoke with sensible defaults, capture errors. The figure
CLI dispatches by subcommand to the matching renderer.

Renderer contract:
  - Each module exposes `render(args) -> int` returning a Unix-style
    exit code (0 = success).
  - Renderers print "[figure]   <message>" lines to stdout and route
    errors to stderr via the figure CLI's helper.
  - On missing dependency, print a clear actionable error and return
    a non-zero exit code rather than tracebacking.
"""
