import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts.update_report import report_document, validate_document
from scripts.update_status import main, public_document
from scripts.update_plan import Deferred


class PublicUpdateStatusTest(unittest.TestCase):
    def report(self, **overrides):
        values = dict(baseline_release_id=None, planned_release_id=None, confirmed_release_id=None,
                      changed=[], metadata_only=[], deferred=[], published=False, fatal=None,
                      dry_run=False, assessed=['alpha', 'beta'])
        values.update(overrides)
        return validate_document(report_document(**values))

    def transform(self, report):
        return public_document(report, '123', '2026-11-13T00:00:00Z')

    def test_assessed_noop_and_confirmed_updates(self):
        self.assertEqual(self.transform(self.report())['packages']['alpha']['outcome'], 'checked')
        report = self.report(changed=['alpha'], published=True, confirmed_release_id='release')
        self.assertEqual(self.transform(report)['packages']['alpha']['outcome'], 'updated')
        self.assertEqual(self.transform(report)['packages']['beta']['outcome'], 'checked')

    def test_failure_never_exposes_private_diagnostics_or_green(self):
        report = self.report(fatal='secret https://example.test/private?token=x')
        document = self.transform(report)
        self.assertEqual(document['outcome'], 'failure')
        self.assertEqual(document['packages'], {})
        self.assertNotIn('secret', str(document))
        self.assertEqual(self.transform(None)['packages'], {})
        self.assertEqual(self.transform(self.report(assessed=None))['packages'], {})
        self.assertEqual(self.transform(self.report(changed=['alpha']))['packages'], {})

    def test_deferred_exposes_only_stage(self):
        report = self.report(deferred=[Deferred('alpha', 'discovery', 'private detail', '1.0')])
        document = self.transform(report)
        self.assertEqual(document['outcome'], 'partial')
        self.assertEqual(document['packages']['alpha'], {'outcome': 'deferred', 'stage': 'discovery'})
        self.assertNotIn('private', str(document))

    def test_preview_never_uploads_and_dry_report_cannot_publish(self):
        with patch('scripts.update_status.load_report', return_value=self.report()), \
             patch('scripts.update_status._s3_client') as client, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--diagnostics', 'unused', '--run-id', '123']), 0)
            client.assert_not_called()
        with patch('scripts.update_status.load_report', return_value=self.report(dry_run=True)), \
             patch('scripts.update_status._s3_client') as client, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--diagnostics', 'unused', '--run-id', '123', '--publish']), 1)
            client.assert_not_called()

    def test_invalid_utf8_publishes_neutral_failure(self):
        error = UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid byte')
        with patch('pathlib.Path.read_text', side_effect=error), \
             patch.dict('os.environ', {'APT_S3_BUCKET': 'test'}), \
             patch('scripts.update_status._s3_client') as client:
            self.assertEqual(main(['--diagnostics', 'unused', '--run-id', '123', '--publish']), 0)
            request = client.return_value.put_object.call_args.kwargs
        document = json.loads(request['Body'])
        self.assertEqual(request['Key'], 'status/update.json')
        self.assertEqual(document['outcome'], 'failure')
        self.assertEqual(document['packages'], {})

    def test_untrusted_run_id_rejected(self):
        with self.assertRaises(ValueError):
            self.transform_with_id('123?token=secret')

    def transform_with_id(self, run_id):
        return public_document(self.report(), run_id, '2026-11-13T00:00:00Z')


if __name__ == '__main__':
    unittest.main()
