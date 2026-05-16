"""Deterministic Markdown/PDF formatting for long-exposure reports."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


REPORT_FONT_SIZE = "10pt"


REPORT_HEADER_TEX = r"""
% --- Body & font setup ----------------------------------------------------
\usepackage{fvextra}
\usepackage{microtype}
\setlength{\emergencystretch}{3em}
\usepackage{xurl}

% --- Floats and figures ---------------------------------------------------
\usepackage{float}
\usepackage{placeins}
\usepackage{caption}
\usepackage{graphicx}
\captionsetup{font=small, labelfont=bf, format=plain,
              justification=raggedright, singlelinecheck=false}
\floatplacement{figure}{!htbp}
\setkeys{Gin}{width=\linewidth, keepaspectratio}
\usepackage{titlesec}
\titlespacing*{\section}{0pt}{1.6\baselineskip}{0.6\baselineskip}
\titlespacing*{\subsection}{0pt}{1.2\baselineskip}{0.4\baselineskip}
\let\oldsection\section
\renewcommand{\section}{\FloatBarrier\oldsection}

% --- Code blocks ----------------------------------------------------------
\usepackage{xcolor}
\usepackage{etoolbox}
\fvset{fontsize=\footnotesize, frame=single, framesep=2mm,
       rulecolor=\color{gray!40},
       breaklines=true, breakanywhere=true,
       breaksymbolleft={\tiny\textcolor{gray}{\ensuremath{\hookrightarrow}}}}
\makeatletter
\AtBeginDocument{%
  \@ifundefined{Highlighting}{}{%
    \RecustomVerbatimEnvironment{Highlighting}{Verbatim}{%
      commandchars=\\\{\},
      fontsize=\footnotesize,
      breaklines=true, breakanywhere=true,
      breaksymbolleft={\tiny\textcolor{gray}{\ensuremath{\hookrightarrow}}}
    }%
  }%
}
\makeatother

% --- Spacing, title block, hyperref --------------------------------------
\setlength{\parskip}{0.5\baselineskip plus 2pt}
\setlength{\parindent}{0pt}
\usepackage{titling}
\setlength{\droptitle}{-1.5em}
\pretitle{\begin{center}\large\bfseries}
\posttitle{\par\end{center}\vskip 0.5em}
\usepackage{hyperref}
\hypersetup{colorlinks=true, linkcolor=black,
            urlcolor=blue!50!black, citecolor=blue!50!black,
            pdfborder={0 0 0}}

% --- Body-text underscore line-break -------------------------------------
\AtBeginDocument{%
  \DeclareRobustCommand{\_}{\textunderscore\penalty\exhyphenpenalty}%
}
""".strip() + "\n"


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+\.svg)(\s+\"[^\"]*\")?\)",
    re.IGNORECASE,
)


def _quote_yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def infer_report_title(markdown: str, fallback: str) -> str:
    """Infer a stable title from the first H1, else use fallback."""
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("# "):
            title = line[2:].strip().strip("#").strip()
            if title:
                return title
    return fallback


def _normalize_heading_spacing(markdown: str) -> str:
    """Ensure headings are separated from previous block content."""
    lines = markdown.splitlines()
    out: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        if (
            not in_fence
            and _ATX_HEADING_RE.match(line)
            and out
            and out[-1].strip()
        ):
            out.append("")
        out.append(line.rstrip())
    return "\n".join(out)


def normalize_report_markdown(
    markdown: str,
    *,
    fallback_title: str,
    date: str | None = None,
) -> str:
    """Return report Markdown with deterministic frontmatter and spacing.

    The function is intentionally conservative: it does not rewrite tables,
    headings, prose, or code content. It only adds/updates metadata that Pandoc
    consumes and inserts blank lines before ATX headings outside fenced blocks.
    """
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    date = date or datetime.now(timezone.utc).date().isoformat()

    metadata: dict = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1)) or {}
            if isinstance(parsed, dict):
                metadata = dict(parsed)
        except yaml.YAMLError:
            metadata = {}
        body = text[match.end():].lstrip("\n")

    metadata["title"] = str(metadata.get("title") or infer_report_title(body, fallback_title))
    metadata.setdefault("date", date)
    metadata["toc"] = True
    metadata["toc-depth"] = 2
    metadata["numbersections"] = False
    metadata["fontsize"] = REPORT_FONT_SIZE

    ordered_keys = ["title", "date", "toc", "toc-depth", "numbersections", "fontsize"]
    fm_lines = ["---"]
    for key in ordered_keys:
        value = metadata.pop(key)
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = _quote_yaml(str(value))
        fm_lines.append(f"{key}: {rendered}")
    for key in sorted(metadata):
        value = metadata[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = _quote_yaml(str(value))
        fm_lines.append(f"{key}: {rendered}")
    fm_lines.extend(["---", ""])

    normalized_body = _normalize_heading_spacing(body).strip()
    return "\n".join(fm_lines) + normalized_body + "\n"


def normalize_report_file(path: Path, fallback_title: str) -> None:
    path = Path(path)
    normalized = normalize_report_markdown(
        path.read_text(),
        fallback_title=fallback_title,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(normalized)
    os.replace(tmp, path)


def _is_remote_ref(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target))


def _resolve_local_ref(target: str, md_path: Path, resource_root: Path) -> Path | None:
    if _is_remote_ref(target):
        return None
    raw = Path(target)
    candidates = [raw] if raw.is_absolute() else [
        md_path.parent / raw,
        resource_root / raw,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def sanitize_markdown_for_pdf(markdown: str, *, md_path: Path, resource_root: Path) -> str:
    """Return Markdown safe for the pandoc -> tectonic PDF path.

    Tectonic's LaTeX SVG path depends on external converters such as
    ``rsvg-convert`` and on generated ``*_svg-tex.pdf`` sidecars. Those are not
    a stable baseline dependency for long-exposure runs. For local SVG image
    embeds, prefer a same-stem PNG if the worker already produced one; otherwise
    degrade the image to a normal artifact link so PDF rendering still succeeds
    and the source Markdown remains authoritative.
    """
    md_path = Path(md_path)
    resource_root = Path(resource_root)
    out: list[str] = []
    in_fence = False

    def replace_image(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        target = match.group(2)
        title = match.group(3) or ""
        local = _resolve_local_ref(target, md_path, resource_root)
        if local is not None:
            png = local.with_suffix(".png")
            if png.exists():
                raw_target = Path(target)
                png_target = str(raw_target.with_suffix(".png"))
                return f"![{alt}]({png_target}{title})"
        label = alt or Path(target).name
        return f"**Figure artifact:** {label}. SVG source: [`{target}`]({target})."

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_MARKDOWN_IMAGE_RE.sub(replace_image, line))
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


def build_pandoc_report_command(
    md_path: Path,
    pdf_path: Path,
    header_path: Path,
    *,
    resource_root: Path,
) -> list[str]:
    resource_path = os.pathsep.join(
        dict.fromkeys([str(resource_root), str(md_path.parent), "."])
    )
    return [
        "pandoc", str(md_path),
        "--from", "markdown+tex_math_single_backslash+tex_math_dollars+raw_tex+autolink_bare_uris",
        "-o", str(pdf_path),
        "--pdf-engine=tectonic",
        "--resource-path", resource_path,
        "-V", "geometry:margin=1in",
        "-V", f"fontsize={REPORT_FONT_SIZE}",
        "-V", "documentclass=article",
        "-V", "mainfont=DejaVu Serif",
        "-V", "monofont=DejaVu Sans Mono",
        "-V", "monofontoptions=Scale=0.82",
        "-V", "colorlinks=true",
        "-V", "linestretch=1.05",
        "-H", str(header_path),
        "--toc",
        "--toc-depth=2",
        "--highlight-style=tango",
    ]


def render_report_pdf(
    md_path: Path,
    pdf_path: Path,
    *,
    cwd: str | Path,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Render a normalized report Markdown file with the standard style."""
    md_path = Path(md_path)
    pdf_path = Path(pdf_path)
    cwd_path = Path(cwd)
    temp_md_path: Path | None = None
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".tex",
        dir=str(cwd_path) if cwd_path.is_dir() else None,
        delete=False,
    ) as fh:
        fh.write(REPORT_HEADER_TEX)
        header_path = Path(fh.name)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix=f"{md_path.stem}_pdf_",
            dir=str(md_path.parent) if md_path.parent.is_dir() else None,
            delete=False,
        ) as md_fh:
            md_fh.write(
                sanitize_markdown_for_pdf(
                    md_path.read_text(),
                    md_path=md_path,
                    resource_root=cwd_path,
                )
            )
            temp_md_path = Path(md_fh.name)
        cmd = build_pandoc_report_command(
            temp_md_path,
            pdf_path,
            header_path,
            resource_root=cwd_path,
        )
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd_path),
        )
    finally:
        try:
            header_path.unlink()
        except OSError:
            pass
        if temp_md_path is not None:
            try:
                temp_md_path.unlink()
            except OSError:
                pass
