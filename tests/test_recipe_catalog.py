from __future__ import annotations

import unittest

from scripts.build import _BUNDLE_STAGERS
from scripts.catalog import RECIPES
from scripts.kandev_bundle import BUNDLE_TYPE as KANDEV_BUNDLE_TYPE
from scripts.manifest import load_manifest
from scripts.pi_bundle import BUNDLE_TYPE as PI_BUNDLE_TYPE


class RecipeCatalogTest(unittest.TestCase):
    def test_every_reviewed_bundle_type_has_a_stager(self) -> None:
        self.assertEqual(set(_BUNDLE_STAGERS), {KANDEV_BUNDLE_TYPE, PI_BUNDLE_TYPE})
        for name, recipe in RECIPES.items():
            if recipe.bundle_type:
                self.assertIn(recipe.bundle_type, _BUNDLE_STAGERS, msg=name)

    def test_every_manifest_bundle_target_names_a_reviewed_stager(self) -> None:
        for package in load_manifest().packages:
            for arch, target in package.targets.items():
                if target.bundle_type:
                    self.assertIn(target.bundle_type, _BUNDLE_STAGERS, msg=f"{package.name} {arch}")

    def test_kandev_keeps_its_reviewed_bundle_dispatch(self) -> None:
        kandev = next(pkg for pkg in load_manifest().packages if pkg.name == "kandev")
        self.assertEqual(kandev.targets["amd64"].bundle_type, KANDEV_BUNDLE_TYPE)
        self.assertEqual(kandev.targets["arm64"].bundle_type, KANDEV_BUNDLE_TYPE)


if __name__ == "__main__":
    unittest.main()
