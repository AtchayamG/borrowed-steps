"""Assemble accepted source and static assets into a NEW local staging directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TEMPLATES = {"app.py", ".python-version", "requirements.txt", "vercel.json"}
STATIC_SUFFIXES = {
    ".html",
    ".css",
    ".js",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
}


def checked_path(path: Path) -> Path:
    """Reject links/reparse points in the original path, including ancestors."""
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Symbolic links are not permitted")
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Reparse points are not permitted")
    return path.resolve()


def _validate_safe_output_dir(output_dir: Path, repo_root: Path) -> None:
    repo = checked_path(repo_root)
    output = checked_path(output_dir)
    if output.parent != repo / "deploy" / "vercel" or not output.name.startswith(
        ("output", "staging")
    ):
        raise ValueError(
            "Output must be a direct output*/staging* child of deploy/vercel"
        )
    if output.exists():
        raise ValueError(
            "Output already exists; choose a fresh directory (nothing is deleted)"
        )


def tree_files(directory: Path, suffixes: set[str]) -> list[Path]:
    directory = checked_path(directory)
    if not directory.is_dir():
        raise ValueError("Required input directory is missing")
    result = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in [*dirs, *files]:
            checked_path(Path(root) / name)
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        if any(name.startswith(".") or name == "node_modules" for name in dirs):
            raise ValueError("Unexpected input directory")
        for name in sorted(files):
            path = Path(root) / name
            if (
                name.startswith(".")
                or path.suffix.lower() not in suffixes
                or not path.is_file()
            ):
                raise ValueError(
                    "Unexpected input file; source/static allowlist refused assembly"
                )
            result.append(path)
    return sorted(result)


def build_frontend(web_dir: Path) -> None:
    """Always install the exact existing lock before building."""
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise RuntimeError("npm is required")
    for arguments in (["ci"], ["run", "build"]):
        subprocess.run([npm, *arguments], cwd=web_dir, check=True, timeout=300)
    if not (web_dir / "dist" / "index.html").is_file():
        raise RuntimeError("Frontend build did not produce index.html")


def assemble_package(
    *,
    repo_root: Path,
    output_dir: Path,
    template_dir: Path,
    web_dir: Path,
    agent_source_dir: Path,
) -> dict[str, Any]:
    _validate_safe_output_dir(output_dir, repo_root)
    repo = checked_path(repo_root)
    expected = [
        repo / "deploy/vercel",
        repo / "apps/web",
        repo / "services/agent/src/borrowed_steps",
    ]
    for supplied, fixed in zip(
        (template_dir, web_dir, agent_source_dir), expected, strict=True
    ):
        if checked_path(supplied) != fixed:
            raise ValueError("Inputs must be the fixed source paths in this repository")
    dist = web_dir / "dist"
    if not (dist / "index.html").is_file():
        raise ValueError("Build the frontend first")
    copies = [
        (p, Path("borrowed_steps") / p.relative_to(agent_source_dir))
        for p in tree_files(agent_source_dir, {".py"})
    ]
    copies += [
        (p, Path("public") / p.relative_to(dist))
        for p in tree_files(dist, STATIC_SUFFIXES)
    ]
    for name in sorted(TEMPLATES):
        source = checked_path(template_dir / name)
        if not source.is_file():
            raise ValueError("Required template file is missing")
        copies.append((source, Path(name)))
    # All inputs checked BEFORE output creation. Existing outputs are never changed.
    output_dir.mkdir(exist_ok=False)
    for source, relative in copies:
        checked_path(source)
        destination = output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    return generate_manifest(output_dir)


def generate_manifest(package_dir: Path) -> dict[str, Any]:
    """Hash every permitted file; reject unexpected files instead of ignoring them."""
    checked_path(package_dir)
    files_list: list[dict[str, Any]] = []
    counts = dict.fromkeys(("source", "config", "static"), 0)
    sizes = counts.copy()
    for root, dirs, files in os.walk(package_dir, followlinks=False):
        for name in [*dirs, *files]:
            checked_path(Path(root) / name)
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(package_dir)
            rel = relative.as_posix()
            if rel == "manifest.json":
                continue
            if rel in TEMPLATES:
                category = "source" if rel == "app.py" else "config"
            elif relative.parts[0] == "borrowed_steps" and path.suffix == ".py":
                category = "source"
            elif (
                relative.parts[0] == "public" and path.suffix.lower() in STATIC_SUFFIXES
            ):
                category = "static"
            else:
                raise ValueError("Unexpected file in staged package")
            if rel not in TEMPLATES and any(
                p.startswith(".") or p in {"__pycache__", "node_modules"}
                for p in relative.parts
            ):
                raise ValueError("Forbidden path in staged package")
            content = path.read_bytes()
            counts[category] += 1
            sizes[category] += len(content)
            files_list.append(
                {
                    "path": rel,
                    "category": category,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    return {
        "total_files": len(files_list),
        "total_uncompressed_bytes": sum(sizes.values()),
        "categories": {"counts": counts, "bytes": sizes},
        "files": sorted(files_list, key=lambda f: f["path"]),
    }


def verify_manifest(package_dir: Path) -> dict[str, Any]:
    manifest = json.loads(
        (checked_path(package_dir / "manifest.json")).read_text(encoding="utf-8")
    )
    actual = generate_manifest(package_dir)
    if manifest != actual:
        raise ValueError(
            "Manifest differs from complete disk paths, hashes, sizes or totals"
        )
    return actual


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    own_repo = Path(__file__).resolve().parent.parent
    parser.add_argument("--repo-root", type=Path, default=own_repo)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse already-built assets; does not prove a fresh build",
    )
    parser.add_argument("--verify-repeat", action="store_true")
    args = parser.parse_args(argv)
    repo = checked_path(args.repo_root)
    if repo != own_repo:
        parser.error("This CLI only assembles its own repository")
    output = args.output_dir or repo / "deploy/vercel" / ("output_" + uuid.uuid4().hex)
    _validate_safe_output_dir(output, repo)
    if not args.skip_build:
        build_frontend(repo / "apps/web")
    inputs = {
        "repo_root": repo,
        "template_dir": repo / "deploy/vercel",
        "web_dir": repo / "apps/web",
        "agent_source_dir": repo / "services/agent/src/borrowed_steps",
    }
    manifest = assemble_package(output_dir=output, **inputs)
    with (output / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    if args.verify_repeat:
        second = repo / "deploy/vercel" / ("output_repeat_" + uuid.uuid4().hex)
        if assemble_package(output_dir=second, **inputs) != manifest:
            raise RuntimeError("Repeated assembly differs")
        print("Repeat assembly matches; retained:", second)
    print(
        json.dumps(
            {
                "output": str(output),
                "files": manifest["total_files"],
                "source_assets_bytes": manifest["total_uncompressed_bytes"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
