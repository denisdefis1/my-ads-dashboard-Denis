# Run with: python3 -m unittest tests.test_fetch_crm -v  (from the repo root)
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch_crm import (  # noqa: E402
    parse_leads,
    compute_data_quality,
    validate_before_write,
)


def make_row(created_at, deal_id, name='Иван Иванов', stage='', country='', qual='квал',
             referer='', utm_content='', utm_id='', utm_term=''):
    row = [''] * 16
    row[0] = created_at
    row[1] = deal_id
    row[4] = name
    row[5] = stage
    row[6] = country
    row[7] = referer
    row[9] = utm_content
    row[11] = utm_id
    row[12] = utm_term
    row[14] = qual
    return row


class ParseLeadsTests(unittest.TestCase):
    def test_dedup_by_deal_id_keeps_last_and_counts_duplicate(self):
        rows = [
            make_row('25.05.2026 10:00:00', '1001', qual='квал'),
            make_row('25.05.2026 11:00:00', '1001', qual='не квал'),  # same deal, later status wins
            make_row('26.05.2026 09:00:00', '1002', qual='квал'),
        ]
        leads, duplicate_rows, _ = parse_leads(rows)
        self.assertEqual(len(leads), 2, "duplicate deal_id must not double-count as two leads")
        self.assertEqual(duplicate_rows, 1)
        by_id = {l['deal_id']: l for l in leads}
        self.assertIs(by_id['1001']['qualified'], False, "the last occurrence of a duplicated deal_id should win")

    def test_qual_column_classification(self):
        rows = [
            make_row('25.05.2026 10:00:00', '1', qual='квал'),
            make_row('25.05.2026 10:00:00', '2', qual='не квал'),
            make_row('25.05.2026 10:00:00', '3', qual=''),
            make_row('25.05.2026 10:00:00', '4', qual='какой-то мусор'),
        ]
        leads, _, suspicious = parse_leads(rows)
        by_id = {l['deal_id']: l for l in leads}
        self.assertIs(by_id['1']['qualified'], True)
        self.assertIs(by_id['2']['qualified'], False)
        self.assertIsNone(by_id['3']['qualified'])
        self.assertIsNone(by_id['4']['qualified'], "unrecognized values must fall back to pending, not qualified")
        self.assertIn(repr('какой-то мусор'), suspicious)

    def test_invisible_characters_do_not_break_classification(self):
        # Zero-width space (U+200B) inserted inside the "qual" cell value —
        # exact string comparison would silently misclassify this as pending.
        qual_with_zwsp = 'квал' + chr(0x200B)
        rows = [make_row('25.05.2026 10:00:00', '1', qual=qual_with_zwsp)]
        leads, _, _ = parse_leads(rows)
        self.assertIs(leads[0]['qualified'], True)

    def test_date_parsing_handles_missing_seconds(self):
        rows = [make_row('25.05.2026 10:00', '1')]
        leads, _, _ = parse_leads(rows)
        self.assertIsNotNone(leads[0]['created_at'])
        self.assertEqual(leads[0]['created_at'].year, 2026)

    def test_unparseable_date_yields_pending_date_not_a_crash(self):
        rows = [make_row('not-a-date', '1')]
        leads, _, _ = parse_leads(rows)
        self.assertIsNone(leads[0]['created_at'])

    def test_rows_missing_deal_id_are_skipped(self):
        rows = [make_row('25.05.2026 10:00:00', '')]
        leads, _, _ = parse_leads(rows)
        self.assertEqual(leads, [])


class DataQualityTests(unittest.TestCase):
    def test_compute_data_quality_counts(self):
        leads = [
            {'qualified': True, 'created_at': '2026-08-05 10:00:00'},
            {'qualified': True, 'created_at': '2026-08-06 10:00:00'},
            {'qualified': True, 'created_at': None},
            {'qualified': False, 'created_at': '2026-08-01 10:00:00'},
            {'qualified': None, 'created_at': '2026-08-02 10:00:00'},
        ]
        dq = compute_data_quality(
            leads,
            google_sheets_total_rows=10,
            duplicate_rows=2,
            suspicious_qual_values=set(),
            output_timestamp='2026-08-19T00:00:00Z',
        )
        self.assertEqual(dq['qualified'], 3)
        self.assertEqual(dq['not_qualified'], 1)
        self.assertEqual(dq['pending'], 1)
        self.assertEqual(dq['qualified_with_date'], 2)
        self.assertEqual(dq['qualified_without_date'], 1)
        self.assertEqual(dq['august_2026_qualified'], 2)
        self.assertEqual(dq['duplicates_removed'], 2)
        self.assertEqual(dq['latest_created_at'], '2026-08-06 10:00:00')


class ValidateBeforeWriteTests(unittest.TestCase):
    def test_rejects_too_few_leads(self):
        with self.assertRaises(ValueError):
            validate_before_write(total=5, match_rate=90)

    def test_rejects_low_match_rate(self):
        with self.assertRaises(ValueError):
            validate_before_write(total=100, match_rate=10)

    def test_accepts_healthy_dataset(self):
        validate_before_write(total=100, match_rate=90)  # must not raise


if __name__ == '__main__':
    unittest.main()
