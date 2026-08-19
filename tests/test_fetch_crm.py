import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import fetch_crm  # noqa: E402  (import after sys.path fix-up)

# Built from codepoints rather than embedded as literal characters, to avoid
# putting invisible/bidi characters directly in source (see fetch_crm.py's
# own comment about "Trojan Source" / CVE-2021-42574).
ZERO_WIDTH_SPACE = chr(0x200B)
BOM = chr(0xFEFF)
NBSP = chr(0xA0)


class CleanInvisibleTests(unittest.TestCase):
    def test_strips_zero_width_space(self):
        self.assertEqual(fetch_crm.clean_invisible('квал' + ZERO_WIDTH_SPACE), 'квал')

    def test_strips_bom_and_replaces_nbsp_with_space_and_trims(self):
        # BOM is dropped entirely; NBSP becomes a normal space so it keeps
        # separating "date" from "time" instead of gluing them together.
        raw = BOM + '  19.08.2026' + NBSP + '14:30:00  '
        self.assertEqual(fetch_crm.clean_invisible(raw), '19.08.2026 14:30:00')

    def test_handles_none(self):
        self.assertEqual(fetch_crm.clean_invisible(None), '')

    def test_handles_empty_string(self):
        self.assertEqual(fetch_crm.clean_invisible(''), '')


class ParseCrmDateTests(unittest.TestCase):
    def test_dot_format_with_seconds(self):
        self.assertEqual(fetch_crm.parse_crm_date('19.08.2026 14:30:00'), datetime(2026, 8, 19, 14, 30, 0))

    def test_dot_format_without_seconds(self):
        self.assertEqual(fetch_crm.parse_crm_date('19.08.2026 14:30'), datetime(2026, 8, 19, 14, 30, 0))

    def test_iso_format_with_seconds(self):
        self.assertEqual(fetch_crm.parse_crm_date('2026-08-19 14:30:00'), datetime(2026, 8, 19, 14, 30, 0))

    def test_iso_format_without_seconds(self):
        self.assertEqual(fetch_crm.parse_crm_date('2026-08-19 14:30'), datetime(2026, 8, 19, 14, 30, 0))

    def test_cleans_invisible_characters_before_parsing(self):
        # This is the exact bug class that caused 6 qualified leads to be
        # silently dropped from date-range calculations: a zero-width space
        # or BOM riding along in the Google Sheets cell value broke the old
        # strict strptime() call.
        raw = ZERO_WIDTH_SPACE + '19.08.2026 14:30:00' + BOM
        self.assertEqual(fetch_crm.parse_crm_date(raw), datetime(2026, 8, 19, 14, 30, 0))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(fetch_crm.parse_crm_date('  19.08.2026 14:30:00  '), datetime(2026, 8, 19, 14, 30, 0))

    def test_returns_none_for_empty_value(self):
        self.assertIsNone(fetch_crm.parse_crm_date(''))

    def test_returns_none_for_none_value(self):
        self.assertIsNone(fetch_crm.parse_crm_date(None))

    def test_returns_none_for_garbage_without_crashing(self):
        self.assertIsNone(fetch_crm.parse_crm_date('not a date'))

    def test_never_fabricates_a_date_for_unparseable_input(self):
        # A partially date-like but invalid string must stay None, not be
        # coerced into some nearby valid date.
        self.assertIsNone(fetch_crm.parse_crm_date('32.13.2026 99:99:99'))


class CleanIdTests(unittest.TestCase):
    def test_rejects_placeholder_tokens(self):
        self.assertIsNone(fetch_crm.clean_id('{{ad.id}}'))

    def test_rejects_non_numeric(self):
        self.assertIsNone(fetch_crm.clean_id('abc123'))

    def test_accepts_numeric_id(self):
        self.assertEqual(fetch_crm.clean_id(' 12345 '), '12345')

    def test_handles_empty(self):
        self.assertIsNone(fetch_crm.clean_id(''))
        self.assertIsNone(fetch_crm.clean_id(None))


def make_lead(deal_id, created_at=None, qualified=None, campaign_id=None, adset_id=None, ad_id=None, raw_date=''):
    return {
        "deal_id": deal_id,
        "created_at": created_at,
        "_raw_date": raw_date,
        "qualified": qualified,
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "ad_id": ad_id,
    }


class BuildDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.reference_date = datetime(2026, 8, 19)

    def test_counts_qualified_with_and_without_date(self):
        leads = [
            make_lead('1', created_at='2026-08-05 10:00:00', qualified=True),
            make_lead('2', created_at=None, qualified=True, raw_date=''),
            make_lead('3', created_at='2026-08-06 10:00:00', qualified=False),
            make_lead('4', created_at=None, qualified=None),
        ]
        dq = fetch_crm.build_data_quality(leads, csv_row_count=4, unparseable_rows=[], reference_date=self.reference_date)
        self.assertEqual(dq['crm_leads'], 4)
        self.assertEqual(dq['qualified'], 2)
        self.assertEqual(dq['qualified_with_date'], 1)
        self.assertEqual(dq['qualified_without_date'], 1)
        self.assertEqual(dq['not_qualified'], 1)
        self.assertEqual(dq['pending'], 1)
        self.assertEqual([l['deal_id'] for l in dq['qualified_without_date_leads']], ['2'])

    def test_august_qualified_filters_by_month_and_qualified_flag(self):
        leads = [
            make_lead('1', created_at='2026-08-01 00:00:00', qualified=True),
            make_lead('2', created_at='2026-08-31 23:59:59', qualified=True),
            make_lead('3', created_at='2026-09-01 00:00:00', qualified=True),  # just outside August
            make_lead('4', created_at='2026-07-31 23:59:59', qualified=True),  # just before August
            make_lead('5', created_at='2026-08-15 00:00:00', qualified=False),  # not qualified
        ]
        dq = fetch_crm.build_data_quality(leads, csv_row_count=5, unparseable_rows=[], reference_date=self.reference_date)
        self.assertEqual(dq['reference_month_label'], 'AUGUST 2026')
        self.assertEqual(len(dq['month_qualified']), 2)
        self.assertEqual([l['deal_id'] for l in dq['month_qualified']], ['1', '2'])

    def test_duplicates_detected_by_deal_id(self):
        leads = [
            make_lead('1', created_at='2026-08-01 00:00:00', qualified=True),
            make_lead('1', created_at='2026-08-02 00:00:00', qualified=True),
            make_lead('2', created_at='2026-08-01 00:00:00', qualified=True),
        ]
        dq = fetch_crm.build_data_quality(leads, csv_row_count=3, unparseable_rows=[], reference_date=self.reference_date)
        self.assertEqual(dq['duplicate_deal_ids'], ['1'])

    def test_latest_created_at_tracks_most_recent_dated_lead(self):
        leads = [
            make_lead('1', created_at='2026-08-01 00:00:00', qualified=True),
            make_lead('2', created_at='2026-08-19 09:00:00', qualified=False),
            make_lead('3', created_at=None, qualified=True),
        ]
        dq = fetch_crm.build_data_quality(leads, csv_row_count=3, unparseable_rows=[], reference_date=self.reference_date)
        self.assertEqual(dq['latest_created_at'], '2026-08-19 09:00:00')
        self.assertEqual(dq['latest_qualified_created_at'], '2026-08-01 00:00:00')

    def test_rows_with_unparseable_date_are_reported_but_do_not_crash(self):
        unparseable = [{"deal_id": "9", "raw_date": "not-a-date"}]
        dq = fetch_crm.build_data_quality([], csv_row_count=1, unparseable_rows=unparseable, reference_date=self.reference_date)
        self.assertEqual(dq['rows_with_unparseable_date'], 1)


if __name__ == '__main__':
    unittest.main()
