from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from paths import get_ticket_workspace_dir

logger = logging.getLogger(__name__)


class WorkspaceSecurityError(ValueError):
    """Raised when a workspace file reference attempts path traversal."""


class WorkspaceManager:
    """Manages per-ticket scratchpad workspace files and query operations."""

    def __init__(
        self,
        ticket_id: str | None = None,
        workspace_dir: Path | str | None = None,
    ) -> None:
        self.ticket_id = ticket_id or ""
        if workspace_dir is not None:
            self.workspace_dir = Path(workspace_dir).resolve()
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.workspace_dir = get_ticket_workspace_dir(self.ticket_id).resolve()

    def resolve_path(self, file_ref: str) -> Path:
        """Resolve a workspace:// URI or relative filename to an absolute path.

        Prevents path traversal outside of the workspace directory.
        """
        cleaned = file_ref.strip()
        if cleaned.startswith("workspace://"):
            cleaned = cleaned[len("workspace://") :]

        target_path = (self.workspace_dir / cleaned).resolve()
        try:
            target_path.relative_to(self.workspace_dir)
        except ValueError as e:
            raise WorkspaceSecurityError(
                f"Access denied: file reference '{file_ref}' resolves outside workspace"
            ) from e

        return target_path

    def save_file(
        self,
        filename: str,
        content: str | bytes,
        overwrite: bool = True,
    ) -> tuple[str, Path]:
        """Save text or binary content into the workspace.

        Returns (file_ref, resolved_path).
        """
        path = self.resolve_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not overwrite and path.exists():
            base_stem = path.stem
            suffix = path.suffix
            counter = 1
            while path.exists():
                path = path.parent / f"{base_stem}_{counter}{suffix}"
                counter += 1

        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

        rel_name = str(path.relative_to(self.workspace_dir))
        return f"workspace://{rel_name}", path

    def list_files(self) -> list[dict[str, Any]]:
        """List all files in the ticket workspace with metadata."""
        if not self.workspace_dir.exists():
            return []

        results = []
        for p in sorted(self.workspace_dir.rglob("*")):
            if p.is_file():
                rel_path = str(p.relative_to(self.workspace_dir))
                size_bytes = p.stat().st_size
                ext = p.suffix.lstrip(".").lower()
                results.append(
                    {
                        "filename": rel_path,
                        "file_ref": f"workspace://{rel_path}",
                        "size_bytes": size_bytes,
                        "format": ext or "text",
                        "mtime": p.stat().st_mtime,
                    }
                )
        return results

    def jq_query(
        self,
        file_ref: str,
        query: str,
        limit: int = 50,
        max_bytes: int = 16384,
    ) -> dict[str, Any]:
        """Execute a jq filter against a JSON file in the workspace.

        Args:
            file_ref: workspace:// file reference or relative path
            query: jq filter string (e.g. '.uperf_100 | keys')
            limit: maximum items if result is a list
            max_bytes: maximum byte length of formatted result before truncating
        """
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        jq_bin = shutil.which("jq")
        if jq_bin:
            try:
                proc = subprocess.run(
                    [jq_bin, query, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode != 0:
                    return {
                        "status": "error",
                        "error": f"jq error (exit {proc.returncode}): {proc.stderr.strip()}",
                    }
                raw_out = proc.stdout.strip()
            except subprocess.TimeoutExpired:
                return {
                    "status": "error",
                    "error": "jq query timed out after 10s",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"Failed to execute jq: {e}",
                }
        else:
            # Fallback: simple python json query if jq CLI is missing
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if query in (".", ""):
                    raw_out = json.dumps(data)
                elif query.startswith("."):
                    key = query.lstrip(".")
                    raw_out = json.dumps(data.get(key, None))
                else:
                    return {
                        "status": "error",
                        "error": "jq executable not found on system and fallback only supports top-level keys",
                    }
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"JSON parsing failed: {e}",
                }

        try:
            parsed = json.loads(raw_out)
            truncated = False
            total_count = None

            if isinstance(parsed, list):
                total_count = len(parsed)
                if len(parsed) > limit:
                    parsed = parsed[:limit]
                    truncated = True
            elif isinstance(parsed, dict) and len(raw_out) > max_bytes:
                truncated = True

            return {
                "status": "ok",
                "file_ref": file_ref,
                "query": query,
                "result": parsed,
                "truncated": truncated,
                "total_items": total_count,
            }
        except json.JSONDecodeError:
            # Could be stream of objects or scalar values
            lines = raw_out.splitlines()
            if len(lines) > limit:
                return {
                    "status": "ok",
                    "file_ref": file_ref,
                    "query": query,
                    "result": "\n".join(lines[:limit]),
                    "truncated": True,
                    "total_items": len(lines),
                }
            return {
                "status": "ok",
                "file_ref": file_ref,
                "query": query,
                "result": raw_out,
                "truncated": False,
                "total_items": len(lines),
            }

    def grep_file(
        self,
        file_ref: str,
        pattern: str,
        max_lines: int = 50,
        context_lines: int = 0,
        case_insensitive: bool = True,
    ) -> dict[str, Any]:
        """Search a workspace text file for regex or string matches."""
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return {
                "status": "error",
                "error": f"Invalid regex pattern '{pattern}': {e}",
            }

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return {
                "status": "error",
                "error": f"Failed reading file: {e}",
            }

        matches: list[dict[str, Any]] = []
        matching_indices = [i for i, line in enumerate(lines) if compiled.search(line)]
        total_matches = len(matching_indices)

        emitted_indices = set()
        for idx in matching_indices[:max_lines]:
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            for i in range(start, end):
                if i not in emitted_indices:
                    emitted_indices.add(i)
                    matches.append(
                        {
                            "line_number": i + 1,
                            "content": lines[i].rstrip("\r\n"),
                            "is_match": i == idx,
                        }
                    )

        matches.sort(key=lambda x: x["line_number"])

        return {
            "status": "ok",
            "file_ref": file_ref,
            "pattern": pattern,
            "total_matches": total_matches,
            "matches_returned": len([m for m in matches if m["is_match"]]),
            "lines": matches,
            "truncated": total_matches > max_lines,
        }

    def read_file_slice(
        self,
        file_ref: str,
        offset_bytes: int = 0,
        max_bytes: int = 4096,
        start_line: int = 1,
        max_lines: int | None = None,
    ) -> dict[str, Any]:
        """Read a slice of a workspace file by byte offset or line range."""
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        total_bytes = path.stat().st_size

        if max_lines is not None:
            # Line-oriented reading
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, start_idx + max_lines)
            slice_lines = lines[start_idx:end_idx]
            content = "".join(slice_lines)
            return {
                "status": "ok",
                "file_ref": file_ref,
                "content": content,
                "start_line": start_line,
                "lines_returned": len(slice_lines),
                "total_lines": total_lines,
                "eof": end_idx >= total_lines,
                "next_start_line": end_idx + 1 if end_idx < total_lines else None,
            }

        # Byte-oriented reading
        with open(path, "rb") as f:
            f.seek(offset_bytes)
            data = f.read(max_bytes)

        text = data.decode("utf-8", errors="replace")
        next_offset = offset_bytes + len(data)
        return {
            "status": "ok",
            "file_ref": file_ref,
            "content": text,
            "offset_bytes": offset_bytes,
            "bytes_returned": len(data),
            "total_bytes": total_bytes,
            "eof": next_offset >= total_bytes,
            "next_offset_bytes": next_offset if next_offset < total_bytes else None,
        }

    @staticmethod
    def generate_preview(filename: str, content: str | bytes) -> dict[str, Any]:
        """Generate a compact schema/head preview for spilled tool output (<100 tokens)."""
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        size_bytes = len(raw_bytes)
        ext = Path(filename).suffix.lstrip(".").lower()

        if ext == "json" or (
            isinstance(content, str) and content.strip().startswith(("{", "["))
        ):
            try:
                parsed = json.loads(
                    content if isinstance(content, str) else raw_bytes.decode("utf-8")
                )
                if isinstance(parsed, dict):
                    keys = list(parsed.keys())
                    preview_info: dict[str, Any] = {
                        "format": "json",
                        "type": "object",
                        "size_bytes": size_bytes,
                        "keys": keys[:15],
                        "total_keys": len(keys),
                    }
                    # If single key with nested dict/list, summarize subkeys
                    if len(keys) == 1:
                        sub = parsed[keys[0]]
                        if isinstance(sub, dict):
                            preview_info["subkeys"] = list(sub.keys())[:15]
                        elif isinstance(sub, list):
                            preview_info["array_length"] = len(sub)
                    return preview_info
                elif isinstance(parsed, list):
                    return {
                        "format": "json",
                        "type": "array",
                        "size_bytes": size_bytes,
                        "length": len(parsed),
                        "sample_item_keys": list(parsed[0].keys())[:10]
                        if (parsed and isinstance(parsed[0], dict))
                        else None,
                    }
            except Exception:
                pass

        # Text fallback preview
        text = (
            content
            if isinstance(content, str)
            else raw_bytes[:1024].decode("utf-8", errors="replace")
        )
        lines = text.splitlines()
        return {
            "format": ext or "text",
            "type": "text",
            "size_bytes": size_bytes,
            "total_lines_approx": len(raw_bytes.split(b"\n")),
            "head_preview": lines[:3],
        }

    def generate_chart(
        self,
        file_ref: str,
        title: str = "Performance Metric Chart",
        chart_type: str = "bar",
        harness: str | None = None,
        output_name: str | None = None,
        x_field: str | None = None,
        y_field: str | None = None,
        group_by: str | None = None,
        metric: str | None = None,
        breakout: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        jq_filter: str | None = None,
    ) -> dict[str, Any]:
        """Generate a declarative Chart.js/Recharts specification from a workspace file and save it to workspace://charts/.

        Returns a dictionary containing the chart spec, file_ref, and preview metadata.
        """
        path = self.resolve_path(file_ref)
        if not path.exists():
            return {
                "status": "error",
                "error": f"File '{file_ref}' does not exist in workspace",
            }

        raw_text = path.read_text(encoding="utf-8", errors="replace")
        data: Any = None
        if path.suffix.lower() == ".json":
            try:
                if jq_filter and shutil.which("jq"):
                    proc = subprocess.run(
                        ["jq", "-c", jq_filter],
                        input=raw_text.encode("utf-8"),
                        capture_output=True,
                        timeout=5,
                    )
                    if proc.returncode == 0:
                        data = json.loads(proc.stdout.decode("utf-8"))
                    else:
                        data = json.loads(raw_text)
                else:
                    data = json.loads(raw_text)
            except Exception as e:
                logger.warning(
                    f"Failed to parse JSON for chart generation from {file_ref}: {e}"
                )
                data = raw_text
        elif path.suffix.lower() == ".csv":
            data = raw_text
        else:
            try:
                data = json.loads(raw_text)
            except Exception:
                data = raw_text

        from providers.workspace.charts import get_chart_registry

        registry = get_chart_registry()
        spec = registry.generate_chart_spec(
            data,
            harness=harness,
            title=title,
            chart_type=chart_type,
            x_field=x_field,
            y_field=y_field,
            group_by=group_by,
            metric=metric,
            breakout=breakout,
            unit=unit,
            max_points=max_points,
            source_file=file_ref,
        )

        if not output_name:
            safe_title = re.sub(r"[^a-zA-Z0-9_]+", "_", title.lower()).strip("_")
            output_name = f"charts/{safe_title or 'chart'}.json"
        elif not output_name.endswith(".json"):
            output_name = f"{output_name}.json"
        if not output_name.startswith("charts/"):
            output_name = f"charts/{output_name}"

        spec_dict = spec.to_dict()
        chart_ref, _ = self.save_file(output_name, json.dumps(spec_dict, indent=2))

        return {
            "status": "ok",
            "chart_ref": chart_ref,
            "chart_data": spec_dict,
            "summary": f"Generated {spec.type} chart '{spec.title}' with {len(spec.labels)} labels and {len(spec.datasets)} datasets.",
        }
