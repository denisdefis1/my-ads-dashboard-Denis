import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_crm  # noqa: E402  (needs sys.path tweak above)

# Строим невидимые символы через chr(), а не вставляем их как литералы в
# исходник — иначе они незаметны в code review (та же ловушка, из-за которой
# сломался парсинг дат в issue #19).
ZERO_WIDTH_SPACE = chr(0x200b)
BOM = chr(0xfeff)
NBSP = chr(0xa0)


def make_row(date='', deal_id='1', name='Test Name', stage='', country='',
             referer='', utm_content='', utm_id='', utm_term='', qual=''):
    row = [''] * 16
    row[0] = date
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


class CleanInvisibleTests(unittest.TestCase):
    def test_strips_zero_width_space(self):
        self.assertEqual(fetch_crm.clean_invisible('квал' + ZERO_WIDTH_SPACE), 'квал')

    def test_strips_bom_and_nbsp(self):
        # clean_invisible удаляет невидимые символы целиком (не заменяет
        # пробелом), поэтому NBSP между датой и временем исчезает вместе с ним.
        self.assertEqual(fetch_crm.clean_invisible(BOM + '19.08.2026' + NBSP + '12:34'), '19.08.202612:34')

    def test_none_becomes_empty(self):
        self.assertEqual(fetch_crm.clean_invisible(None), '')

    def test_plain_text_untouched(self):
        self.assertEqual(fetch_crm.clean_invisible('не квал'), 'не квал')


class ParseCrmDateTests(unittest.TestCase):
    def test_dd_mm_yyyy_with_seconds(self):
        d = fetch_crm.parse_crm_date('19.08.2026 12:34:56')
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day, d.hour, d.minute, d.second), (2026, 8, 19, 12, 34, 56))

    def test_dd_mm_yyyy_without_seconds(self):
        d = fetch_crm.parse_crm_date('19.08.2026 12:34')
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day, d.hour, d.minute), (2026, 8, 19, 12, 34))

    def test_iso_with_seconds(self):
        d = fetch_crm.parse_crm_date('2026-08-19 12:34:56')
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day), (2026, 8, 19))

    def test_iso_without_seconds(self):
        d = fetch_crm.parse_crm_date('2026-08-19 12:34')
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day), (2026, 8, 19))

    def test_invisible_characters_do_not_break_parsing(self):
        d = fetch_crm.parse_crm_date(ZERO_WIDTH_SPACE + '19.08.2026 12:34' + ZERO_WIDTH_SPACE)
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day, d.hour, d.minute), (2026, 8, 19, 12, 34))

    def test_empty_and_garbage_return_none(self):
        self.assertIsNone(fetch_crm.parse_crm_date(''))
        self.assertIsNone(fetch_crm.parse_crm_date('not a date'))
        self.assertIsNone(fetch_crm.parse_crm_date(None))


class QualificationClassificationTests(unittest.TestCase):
    """Секция 3 issue #19: qualified должен определяться только точным
    совпадением после clean_invisible().strip().lower() — без fuzzy matching,
    которое могло бы превратить 'не квал' в qualified."""

    def test_exact_qual_is_true(self):
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='квал')])
        self.assertIs(result['leads'][0]['qualified'], True)

    def test_exact_not_qual_is_false(self):
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='не квал')])
        self.assertIs(result['leads'][0]['qualified'], False)

    def test_empty_is_none(self):
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='')])
        self.assertIsNone(result['leads'][0]['qualified'])

    def test_not_qual_never_becomes_true_via_fuzzy_containment(self):
        # 'не квал' содержит 'квал' как подстроку — важно, что классификация
        # идёт по точному совпадению, а не по "содержит 'квал'".
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='не квал')])
        self.assertIsNot(result['leads'][0]['qualified'], True)

    def test_invisible_characters_in_qual_column_still_classify_correctly(self):
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='квал' + ZERO_WIDTH_SPACE)])
        self.assertIs(result['leads'][0]['qualified'], True)

    def test_unexpected_value_is_none_not_fuzzy_matched(self):
        result = fetch_crm.parse_crm_rows([make_row(deal_id='1', qual='квал?')])
        self.assertIsNone(result['leads'][0]['qualified'])
        self.assertIn(repr('квал?'), result['suspicious_qual_values'])


class DateLossDiagnosticsTests(unittest.TestCase):
    """Секция 4/18 issue #19: qualified-лид с неразбираемой датой не должен
    молча исчезать — он обязан быть учтён в диагностике."""

    def test_qualified_lead_with_unparseable_date_is_counted_not_dropped(self):
        rows = [make_row(deal_id='1', date='not-a-real-date', qual='квал')]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertEqual(len(result['leads']), 1)
        self.assertIsNone(result['leads'][0]['created_at'])
        self.assertIs(result['leads'][0]['qualified'], True)
        self.assertEqual(result['rows_with_unparseable_date'], 1)
        self.assertEqual(result['qualified_rows_with_unparseable_date'], 1)

    def test_not_qualified_lead_with_unparseable_date_not_counted_as_qualified_unparseable(self):
        rows = [make_row(deal_id='1', date='not-a-real-date', qual='не квал')]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertEqual(result['rows_with_unparseable_date'], 1)
        self.assertEqual(result['qualified_rows_with_unparseable_date'], 0)

    def test_empty_date_is_not_counted_as_unparseable(self):
        # Пустая дата — это "нет данных", а не "не смогли распарсить".
        rows = [make_row(deal_id='1', date='', qual='квал')]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertEqual(result['rows_with_unparseable_date'], 0)

    def test_valid_date_parses_and_is_not_flagged(self):
        rows = [make_row(deal_id='1', date='19.08.2026 10:00:00', qual='квал')]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertIsNotNone(result['leads'][0]['created_at'])
        self.assertEqual(result['rows_with_unparseable_date'], 0)


class DeduplicationTests(unittest.TestCase):
    def test_duplicate_deal_id_is_counted_and_skipped(self):
        rows = [
            make_row(deal_id='42', qual='квал'),
            make_row(deal_id='42', qual='не квал'),
        ]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertEqual(len(result['leads']), 1)
        self.assertEqual(result['duplicates'], 1)
        # первое вхождение побеждает
        self.assertIs(result['leads'][0]['qualified'], True)

    def test_rows_without_deal_id_are_skipped_not_counted_as_duplicates(self):
        rows = [make_row(deal_id=''), make_row(deal_id='')]
        result = fetch_crm.parse_crm_rows(rows)
        self.assertEqual(len(result['leads']), 0)
        self.assertEqual(result['duplicates'], 0)


class BuildDataQualityTests(unittest.TestCase):
    def test_counts_and_no_silent_loss(self):
        rows = [
            make_row(deal_id='1', date='19.08.2026 10:00:00', qual='квал'),
            make_row(deal_id='2', date='not-a-date', qual='квал'),
            make_row(deal_id='3', date='19.08.2026 11:00:00', qual='не квал'),
            make_row(deal_id='4', date='', qual=''),
        ]
        parsed = fetch_crm.parse_crm_rows(rows)
        dq = fetch_crm.build_data_quality(
            parsed['leads'], 'http://example.com', 200, len(rows),
            parsed['duplicates'], parsed['rows_with_unparseable_date'],
            parsed['qualified_rows_with_unparseable_date'],
        )
        self.assertEqual(dq['total_crm_leads'], 4)
        self.assertEqual(dq['qualified'], 2)
        self.assertEqual(dq['not_qualified'], 1)
        self.assertEqual(dq['pending'], 1)
        self.assertEqual(dq['qualified_with_date'], 1)
        self.assertEqual(dq['qualified_without_date'], 1)
        self.assertEqual(dq['rows_with_unparseable_date'], 1)
        self.assertEqual(dq['qualified_rows_with_unparseable_date'], 1)
        # Никто не потерян молча: with_date + without_date == qualified.
        self.assertEqual(dq['qualified_with_date'] + dq['qualified_without_date'], dq['qualified'])


if __name__ == '__main__':
    unittest.main()
