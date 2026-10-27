import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SENDER = Path(__file__).resolve().parents[1] / 'send_ec_test_email.ps1'


def package():
    cases = []
    for account, prca, request_id, category in [
        ('90586438', '90586688', 'MBR-0011485', 'DIAGNOSTIC'),
        ('62040942', '62040942', 'MBR-0011502', 'GLOVES - NITRILE'),
    ]:
        cases.append({'account': account, 'prca': prca, 'request_id': request_id,
                      'category': category, 'current_tier': None, 'suggested_tier': None,
                      'analyst_tier': 7, 'status': 'INPUT_UNAVAILABLE',
                      'annualized_gp_change': None, 'annualized_internal_cost_change': None,
                      'annualized_customer_spend_change': None,
                      'rationale': 'Synthetic unit test <script>not executed</script>',
                      'exceptions': ['UNIT_TEST_NOT_REAL_ANALYSIS']})
    return {'mode': 'EC_TWO_CASE_TEST', 'recipient': 'jake.tenkate@mckesson.com', 'cases': cases}


@unittest.skipUnless(shutil.which('powershell'), 'Windows PowerShell required')
class PowerShellEmailTests(unittest.TestCase):
    def invoke(self, path, *switches):
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File',
                                 str(SENDER), '-PackagePath', str(path), *switches],
                                capture_output=True, text=True, timeout=30)
        error_text = ' '.join(result.stderr.split())
        if 'running scripts is disabled' in error_text or 'PSSecurityException' in error_text:
            self.skipTest('PowerShell execution policy blocks script runtime tests; policy is not bypassed')
        return result

    def test_preview_is_repeatable_literal_safe_and_never_creates_outlook_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package.json'
            path.write_text(json.dumps(package()), encoding='utf-8')
            for _ in range(2):
                result = self.invoke(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('VALIDATED_PREVIEW_ONLY', result.stdout)
            body = (path.parent / 'email_preview.html').read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;', body)
            self.assertIn('Unavailable', body)
            self.assertFalse((path.parent / 'outlook_receipt.json').exists())

    def test_wrong_recipient_and_wrong_request_are_rejected(self):
        for change in ('recipient', 'request'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                data = package()
                if change == 'recipient':
                    data['recipient'] = 'other@example.invalid'
                else:
                    data['cases'][0]['request_id'] = 'NOT_AUTHORIZED'
                path = Path(directory) / 'package.json'
                path.write_text(json.dumps(data), encoding='utf-8')
                result = self.invoke(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((path.parent / 'email_preview.html').exists())

    def test_send_whatif_has_no_outlook_or_preview_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package.json'
            path.write_text(json.dumps(package()), encoding='utf-8')
            result = self.invoke(path, '-Send', '-WhatIf')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((path.parent / 'email_preview.html').exists())
            self.assertFalse((path.parent / 'outlook_receipt.json').exists())


if __name__ == '__main__':
    unittest.main()