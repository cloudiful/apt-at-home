"""The personal-use fork's version mapping: its tag, its version, and their order.

A fork release tag is `vX.Y.Z-cloudiful.N` and the Debian upstream version it
publishes is `X.Y.Z+cloudiful.N`. The mapping is narrow: only the reviewed
fork's own label is accepted, only a positive revision counts, every other
package keeps its plain stable parser, and the order agrees with the Debian
version comparison of the same versions.
"""

from __future__ import annotations

import subprocess
import unittest

from scripts.catalog import (
    fork_label_for,
    fork_tag_from_version,
    fork_version_from_tag,
    tag_for,
)
from scripts.upstream_assets import parse_stable_tag, version_key

FORK = "forgejo-cli"
OTHER = "zellij"


class ForkTagMappingTest(unittest.TestCase):
    """One fork tag names one Debian upstream version."""

    def test_a_fork_tag_maps_to_its_debian_version(self) -> None:
        self.assertEqual(
            fork_version_from_tag(FORK, "0.7.0-cloudiful.2"), "0.7.0+cloudiful.2"
        )
        self.assertEqual(
            fork_tag_from_version(FORK, "0.7.0+cloudiful.2"), "0.7.0-cloudiful.2"
        )

    def test_the_tag_prefix_is_added_by_the_catalog(self) -> None:
        self.assertEqual(tag_for(FORK, "0.7.0+cloudiful.2"), "v0.7.0-cloudiful.2")
        self.assertEqual(tag_for(FORK, "0.7.0+cloudiful.10"), "v0.7.0-cloudiful.10")
        self.assertEqual(parse_stable_tag(FORK, "v0.7.0-cloudiful.2"), "0.7.0+cloudiful.2")

    def test_only_the_reviewed_fork_label_is_accepted(self) -> None:
        for tag in (
            "0.7.0",
            "0.7.0-rc1",
            "0.7.0-beta.1",
            "0.7.0-cloudiful",
            "0.7.0-cloudiful.0",
            "0.7.0-cloudiful.x",
            "0.7.0-other.1",
            "0.7-cloudiful.1",
        ):
            with self.subTest(tag=tag):
                self.assertIsNone(fork_version_from_tag(FORK, tag))

    def test_only_the_reviewed_fork_label_is_accepted_in_a_version(self) -> None:
        for version in (
            "0.7.0",
            "0.7.0+rc1",
            "0.7.0+cloudiful",
            "0.7.0+cloudiful.0",
            "0.7.0+cloudiful.x",
            "0.7.0+other.1",
            "0.7+cloudiful.1",
        ):
            with self.subTest(version=version):
                self.assertIsNone(fork_tag_from_version(FORK, version))

    def test_another_package_never_reads_a_fork_suffix(self) -> None:
        self.assertEqual(fork_label_for(OTHER), "")
        self.assertIsNone(fork_version_from_tag(OTHER, "0.7.0-cloudiful.2"))
        self.assertIsNone(fork_tag_from_version(OTHER, "0.7.0+cloudiful.2"))
        with self.assertRaisesRegex(ValueError, "exact stable"):
            parse_stable_tag(OTHER, "v0.7.0-cloudiful.2")

    def test_the_fork_recipe_refuses_a_plain_upstream_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "fork version tag"):
            parse_stable_tag(FORK, "v0.7.0")

    def test_the_fork_recipe_refuses_a_prerelease_tag(self) -> None:
        for tag in ("v0.7.0-rc1", "v0.7.0-beta.1"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(ValueError, "fork version tag"):
                    parse_stable_tag(FORK, tag)

    def test_a_fork_version_is_not_a_stable_upstream_version(self) -> None:
        for version in ("0.7.0+rc1", "0.7.0+other.1", "0.7.0+cloudiful.0"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    version_key(version)


class ForkVersionOrderTest(unittest.TestCase):
    """A fork revision orders above its base and below the next release."""

    def test_a_fork_revision_is_newer_than_the_release_it_forked(self) -> None:
        self.assertGreater(version_key("0.7.0+cloudiful.2"), version_key("0.7.0"))

    def test_a_fork_revision_is_older_than_the_next_upstream_release(self) -> None:
        self.assertLess(version_key("0.7.0+cloudiful.2"), version_key("0.7.1"))

    def test_fork_revisions_are_ordered_numerically(self) -> None:
        self.assertGreater(
            version_key("0.7.0+cloudiful.10"), version_key("0.7.0+cloudiful.2")
        )
        self.assertLess(
            version_key("0.7.0+cloudiful.2"), version_key("0.7.0+cloudiful.10")
        )

    def _dpkg_compares(self, left: str, relation: str, right: str) -> bool:
        """Whether the Debian version comparison holds for two versions."""
        proc = subprocess.run(
            ["dpkg", "--compare-versions", left, relation, right],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return proc.returncode == 0

    def test_the_order_agrees_with_the_debian_version_comparison(self) -> None:
        cases = [
            ("0.7.0+cloudiful.2-1", "0.7.0-1"),
            ("0.7.0+cloudiful.10-1", "0.7.0+cloudiful.2-1"),
            ("0.7.1-1", "0.7.0+cloudiful.10-1"),
        ]
        for newer, older in cases:
            with self.subTest(newer=newer, older=older):
                self.assertTrue(self._dpkg_compares(newer, "gt", older))
                self.assertTrue(self._dpkg_compares(older, "lt", newer))
                self.assertGreater(
                    version_key(newer.removesuffix("-1")), version_key(older.removesuffix("-1"))
                )


if __name__ == "__main__":
    unittest.main()
