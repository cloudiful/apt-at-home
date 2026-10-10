"""Canonical release manifest artifact and its signed-Release digest binding."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.common import sha256_bytes
from scripts.release_manifest import (
    MANIFEST_NAME,
    SHA256_FIELD,
    SIZE_FIELD,
    canonical_bytes,
    check_binding,
    check_binding_bytes,
    load_manifest_data,
    manifest_binding_fields,
    parse_binding,
    validated_document,
    write_manifest_artifact,
    write_manifest_document,
)

SOURCE = Path(__file__).resolve().parent.parent / "packaging" / "manifest.json"


class CanonicalManifestTest(unittest.TestCase):
    def test_canonical_bytes_are_stable_and_preserve_source_order(self) -> None:
        data = load_manifest_data(SOURCE)
        payload = canonical_bytes(data)
        self.assertEqual(payload, canonical_bytes(json.loads(SOURCE.read_text(encoding="utf-8"))))
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b"\t", payload)
        self.assertEqual(payload.decode("utf-8"), json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def test_artifact_copies_the_source_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = write_manifest_artifact(source=SOURCE, destination=Path(tmp) / "nested" / "manifest.json")
            self.assertEqual(artifact.read_bytes(), canonical_bytes(load_manifest_data(SOURCE)))
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8")), json.loads(SOURCE.read_text(encoding="utf-8")))


class BindingTest(unittest.TestCase):
    def _artifact(self, root: Path) -> Path:
        return write_manifest_artifact(source=SOURCE, destination=root / "manifest.json")

    def _release(self, path: Path) -> str:
        return f"Origin: apt-at-home\n{path.read_text(encoding='utf-8')}"

    def test_fields_describe_the_artifact_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(Path(tmp))
            fields = dict(manifest_binding_fields(artifact))
            self.assertEqual(fields[SHA256_FIELD], sha256_bytes(artifact.read_bytes()))
            self.assertEqual(fields[SIZE_FIELD], str(artifact.stat().st_size))

    def test_matching_binding_verifies_and_absent_binding_is_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            release = "Origin: apt-at-home\n" + "".join(
                f"{name}: {value}\n" for name, value in manifest_binding_fields(artifact)
            )
            digest, size = parse_binding(release)
            self.assertEqual(digest, sha256_bytes(artifact.read_bytes()))
            self.assertEqual(size, str(artifact.stat().st_size))
            self.assertTrue(check_binding(release_text=release, manifest_path=artifact))
            self.assertFalse(check_binding(release_text="Origin: apt-at-home\n", manifest_path=artifact))

    def test_partial_binding_always_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            fields = dict(manifest_binding_fields(artifact))
            for missing in (SHA256_FIELD, SIZE_FIELD):
                with self.subTest(missing=missing):
                    partial = "Origin: apt-at-home\n" + "".join(
                        f"{name}: {value}\n" for name, value in fields.items() if name != missing
                    )
                    with self.assertRaises(ValueError):
                        check_binding(release_text=partial, manifest_path=artifact)

    def test_tampered_missing_and_mismatched_artifact_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            release = "Origin: apt-at-home\n" + "".join(
                f"{name}: {value}\n" for name, value in manifest_binding_fields(artifact)
            )
            artifact.write_bytes(artifact.read_bytes().replace(b'"maintainer"', b'"maintainer "'))
            with self.assertRaises(ValueError):
                check_binding(release_text=release, manifest_path=artifact)
            artifact.unlink()
            with self.assertRaises(ValueError):
                check_binding(release_text=release, manifest_path=artifact)

    def test_size_only_mismatch_is_reported_against_the_declared_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = self._artifact(root)
            fields = dict(manifest_binding_fields(artifact))
            declared = f"Origin: apt-at-home\n{SHA256_FIELD}: {fields[SHA256_FIELD]}\n{SIZE_FIELD}: {int(fields[SIZE_FIELD]) + 7}\n"
            with self.assertRaises(ValueError) as ctx:
                check_binding(release_text=declared, manifest_path=artifact)
            self.assertIn("size mismatch", str(ctx.exception))


class RemoteBindingTest(unittest.TestCase):
    """The restore path validates the same binding from bytes, not from a file."""

    def _release(self, payload: bytes) -> str:
        fields = [
            (SHA256_FIELD, sha256_bytes(payload)),
            (SIZE_FIELD, str(len(payload))),
        ]
        return "Origin: apt-at-home\n" + "".join(f"{k}: {v}\n" for k, v in fields)

    def test_bytes_and_path_agree(self) -> None:
        payload = canonical_bytes(load_manifest_data(SOURCE))
        release = self._release(payload)
        self.assertTrue(check_binding_bytes(release_text=release, payload=payload))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / MANIFEST_NAME
            path.write_bytes(payload)
            self.assertTrue(check_binding(release_text=release, manifest_path=path))
            with self.assertRaises(ValueError):
                check_binding(release_text=release, manifest_path=Path(tmp) / "absent.json")

    def test_tampered_or_absent_bytes_fail(self) -> None:
        payload = canonical_bytes(load_manifest_data(SOURCE))
        release = self._release(payload)
        with self.assertRaises(ValueError):
            check_binding_bytes(release_text=release, payload=payload + b"\n")
        with self.assertRaises(ValueError):
            check_binding_bytes(release_text=release, payload=None)

    def test_validated_document_round_trips_the_committed_manifest(self) -> None:
        data, manifest = validated_document(SOURCE)
        self.assertEqual(data, json.loads(SOURCE.read_text(encoding="utf-8")))
        self.assertEqual(sorted(p.name for p in manifest.packages), ["starship", "zellij"])
        self.assertEqual(canonical_bytes(data), canonical_bytes(load_manifest_data(SOURCE)))

    def test_document_writer_matches_the_artifact_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from_file = write_manifest_artifact(source=SOURCE, destination=root / "a" / MANIFEST_NAME)
            from_memory = write_manifest_document(
                data=load_manifest_data(SOURCE), destination=root / "b" / MANIFEST_NAME
            )
            self.assertEqual(from_file.read_bytes(), from_memory.read_bytes())


if __name__ == "__main__":
    unittest.main()
