"""The updater compares every exact tagged license file with its reviewed copy."""

import unittest

from scripts.license_sources import check_license_sources, vendor_license_sources
from scripts.upstream_assets import license_urls_for


class FakeHttp:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requests: list[tuple[str, int]] = []

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requests.append((url, max_bytes))
        return self.payloads[url]


class LicenseSourcesTest(unittest.TestCase):
    def test_uv_checks_primary_and_supplemental_bytes_in_recipe_order(self) -> None:
        vendored = vendor_license_sources("uv")
        urls = license_urls_for("uv", "0.13.0")
        client = FakeHttp(dict(zip(urls, vendored.values(), strict=True)))

        primary, extras = check_license_sources(client, "uv", "0.13.0")

        self.assertEqual((primary, extras), (urls[0], urls[1:]))
        self.assertEqual([url for url, _ in client.requests], list(urls))
        self.assertTrue(all(limit == 1024 * 1024 for _, limit in client.requests))

    def test_changed_supplemental_bytes_fail_closed(self) -> None:
        urls = license_urls_for("uv", "0.13.0")
        vendored = vendor_license_sources("uv")
        payloads = dict(zip(urls, vendored.values(), strict=True))
        payloads[urls[1]] = b"not the reviewed Apache license"

        with self.assertRaisesRegex(ValueError, "supplemental license LICENSE-APACHE bytes changed"):
            check_license_sources(FakeHttp(payloads), "uv", "0.13.0")

    def test_missing_or_extra_local_provenance_is_rejected_before_fetch(self) -> None:
        client = FakeHttp({})
        with self.assertRaisesRegex(ValueError, "vendored license files"):
            check_license_sources(client, "uv", "0.13.0", vendored={})
        self.assertEqual(client.requests, [])

    def test_a_codeberg_package_reads_its_licenses_from_its_own_source(self) -> None:
        vendored = vendor_license_sources("forgejo-cli")
        urls = license_urls_for("forgejo-cli", "v0.7.0")
        self.assertEqual(
            urls,
            (
                "https://codeberg.org/forgejo-contrib/forgejo-cli/raw/tag/v0.7.0/LICENSE-MIT",
                "https://codeberg.org/forgejo-contrib/forgejo-cli/raw/tag/v0.7.0/LICENSE-APACHE",
            ),
        )
        client = FakeHttp(dict(zip(urls, vendored.values(), strict=True)))

        primary, extras = check_license_sources(client, "forgejo-cli", "v0.7.0")

        self.assertEqual((primary, extras), (urls[0], urls[1:]))
        self.assertEqual([url for url, _ in client.requests], list(urls))

    def test_changed_codeberg_license_bytes_fail_closed(self) -> None:
        urls = license_urls_for("forgejo-cli", "v0.7.0")
        vendored = vendor_license_sources("forgejo-cli")
        payloads = dict(zip(urls, vendored.values(), strict=True))
        payloads[urls[0]] = b"a different license text"

        with self.assertRaisesRegex(ValueError, "license bytes changed"):
            check_license_sources(FakeHttp(payloads), "forgejo-cli", "v0.7.0")


if __name__ == "__main__":
    unittest.main()
