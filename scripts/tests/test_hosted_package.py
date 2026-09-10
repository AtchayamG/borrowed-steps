"""Regression checks for packaging safety and evidence integrity (standard library)."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assemble_hosted import (
    TEMPLATES,
    _validate_safe_output_dir,
    assemble_package,
    checked_path,
    verify_manifest,
)
from verify_hosted_package import validate_pg_url


class PackageSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bs017-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.template = self.repo / "deploy/vercel"
        self.source = self.repo / "services/agent/src/borrowed_steps"
        self.web = self.repo / "apps/web"
        for directory in (self.template, self.source, self.web / "dist"):
            directory.mkdir(parents=True)
        for name in TEMPLATES:
            (self.template / name).write_text("template", encoding="utf-8")
        (self.source / "__init__.py").write_text("# source\n", encoding="utf-8")
        (self.web / "dist/index.html").write_text("<html></html>", encoding="utf-8")
        self.output = self.template / "output_test"

    def assemble(self) -> dict[str, object]:
        return assemble_package(
            repo_root=self.repo,
            output_dir=self.output,
            template_dir=self.template,
            web_dir=self.web,
            agent_source_dir=self.source,
        )

    def stage(self) -> dict[str, object]:
        manifest = self.assemble()
        (self.output / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    def test_outputs_outside_or_in_source_refused(self) -> None:
        for output in (
            self.repo.parent / "output_other",
            self.repo / "scripts",
            self.template,
            self.source,
            self.template / "other",
            self.template / "output_nested/child",
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                _validate_safe_output_dir(output, self.repo)

    def test_existing_output_is_untouched(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("user data")
        with self.assertRaises(ValueError):
            self.assemble()
        self.assertEqual(sentinel.read_text(), "user data")

    def test_existing_empty_output_refused(self) -> None:
        self.output.mkdir()
        with self.assertRaises(ValueError):
            self.assemble()

    def test_source_secret_refused_before_output_created(self) -> None:
        (self.source / ".env").write_text("synthetic sentinel")
        with self.assertRaises(ValueError):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_source_database_refused(self) -> None:
        (self.source / "runtime.db").write_bytes(b"fixture")
        with self.assertRaises(ValueError):
            self.assemble()

    def test_static_secret_refused(self) -> None:
        (self.web / "dist/.env").write_text("fixture")
        with self.assertRaises(ValueError):
            self.assemble()

    def test_alternate_input_refused(self) -> None:
        with self.assertRaises(ValueError):
            assemble_package(
                repo_root=self.repo,
                output_dir=self.output,
                template_dir=self.template,
                web_dir=self.web,
                agent_source_dir=self.repo.parent,
            )

    def test_missing_template_refused_before_output_created(self) -> None:
        (self.template / "app.py").unlink()
        with self.assertRaises(ValueError):
            self.assemble()
        self.assertFalse(self.output.exists())

    def test_reparse_ancestor_refused(self) -> None:
        original = Path.lstat
        junction = self.template

        def fake_lstat(path: Path) -> object:
            if path == junction:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return original(path)

        with patch.object(Path, "lstat", fake_lstat), self.assertRaises(ValueError):
            checked_path(self.output)

    def test_symbolic_link_refused(self) -> None:
        with (
            patch.object(Path, "is_symlink", return_value=True),
            self.assertRaises(ValueError),
        ):
            checked_path(self.source)

    def test_source_bytes_and_repeat_match(self) -> None:
        first = self.stage()
        self.assertEqual(
            (self.output / "borrowed_steps/__init__.py").read_bytes(),
            (self.source / "__init__.py").read_bytes(),
        )
        self.assertEqual(verify_manifest(self.output), first)
        self.output = self.template / "output_second"
        self.assertEqual(self.assemble(), first)

    def test_same_size_tamper_rejected(self) -> None:
        self.stage()
        (self.output / "app.py").write_text("tampered")
        with self.assertRaises(ValueError):
            verify_manifest(self.output)

    def test_extra_permitted_file_rejected(self) -> None:
        self.stage()
        (self.output / "borrowed_steps/extra.py").write_text("# extra")
        with self.assertRaises(ValueError):
            verify_manifest(self.output)

    def test_extra_secret_rejected(self) -> None:
        self.stage()
        (self.output / ".env").write_text("fixture")
        with self.assertRaises(ValueError):
            verify_manifest(self.output)

    def test_manifest_traversal_and_totals_rejected(self) -> None:
        baseline = self.stage()
        for change in (
            {**baseline, "total_files": 999},
            {**baseline, "files": [{"path": "../../outside"}]},
            {**baseline, "files": []},
        ):
            (self.output / "manifest.json").write_text(json.dumps(change))
            with self.assertRaises(ValueError):
                verify_manifest(self.output)

    def test_nonlocal_or_option_database_url_refused(self) -> None:
        for value in (
            "postgresql://db.example:5432/postgres",
            "postgresql://localhost:5432/postgres",
            "postgresql://127.0.0.1/postgres",
            "postgresql://127.0.0.1:5432/postgres?host=db.example",
            "postgresql://127.0.0.1:5432/production",
            "postgresql://127.0.0.1:bad/postgres",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_pg_url(value)

    def test_numeric_loopback_database_url_allowed(self) -> None:
        validate_pg_url("postgresql://synthetic@127.0.0.1:55439/postgres")
        validate_pg_url("postgresql://synthetic@[::1]:55439/postgres")


if __name__ == "__main__":
    unittest.main()
