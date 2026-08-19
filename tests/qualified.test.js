'use strict';

/**
 * Node test suite for the normalized qualified-leads layer.
 *
 * Run with: node tests/qualified.test.js
 *
 * Imports the exact same qualified-core.js that index.html loads in the
 * browser — there is no reimplementation of the normalization logic here,
 * so a passing test suite means the production code behaves correctly.
 *
 * The fixture below reproduces the real numbers from the issue diagnosis
 * for 25.05.2026–21.06.2026 (209 total leads, 25 raw "квал", 2 raw
 * "не квал", 182 empty) plus a handful of leads outside that period, so
 * "All Time" / before / after / partial-overlap behaviour can be checked
 * precisely instead of against made-up numbers.
 */

const assert = require('assert');
const path = require('path');
const QualifiedCore = require(path.join(__dirname, '..', 'qualified-core.js'));

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log('  ok - ' + name);
}

function lead(overrides) {
  return Object.assign({
    deal_id: null,
    created_at: null,
    stage: 'Первый контакт',
    country: 'Test',
    qualified: null,
    ad_id: null,
    campaign_id: null,
    adset_id: null,
    match_type: null,
    match_confidence: null,
  }, overrides);
}

function repeat(n, factory) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(factory(i));
  return out;
}

// ---------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------

let dealSeq = 0;
function nextDealId() { dealSeq += 1; return 'D' + dealSeq; }

const CAMPAIGN_LANG = { campRU: 'RU', campEN: 'EN' };

const periodLeads = [
  // 2026-05-25 .. 2026-06-21 (the historical period), exactly matching the
  // issue's diagnosis: 209 total, 25 raw "квал", 2 raw "не квал", 182 empty.
  ...repeat(5, () => lead({ deal_id: nextDealId(), created_at: '2026-05-27 10:00:00', qualified: true, campaign_id: 'campRU', adset_id: 'adsetRU1' })),
  ...repeat(5, () => lead({ deal_id: nextDealId(), created_at: '2026-06-02 10:00:00', qualified: true, campaign_id: 'campRU', adset_id: 'adsetRU1' })),
  ...repeat(5, () => lead({ deal_id: nextDealId(), created_at: '2026-06-05 10:00:00', qualified: true })), // real, unmatched (no campaign)
  ...repeat(5, () => lead({ deal_id: nextDealId(), created_at: '2026-06-15 10:00:00', qualified: true, campaign_id: 'campEN', adset_id: 'adsetEN1' })),
  ...repeat(5, () => lead({ deal_id: nextDealId(), created_at: '2026-06-20 10:00:00', qualified: true })), // real, unmatched (no campaign)
  ...repeat(2, () => lead({ deal_id: nextDealId(), created_at: '2026-06-08 10:00:00', qualified: false })),
  ...repeat(182, () => lead({ deal_id: nextDealId(), created_at: '2026-06-10 10:00:00', qualified: null })),
];

const beforePeriodLeads = [
  ...repeat(3, () => lead({ deal_id: nextDealId(), created_at: '2026-05-01 10:00:00', qualified: true })),
  ...repeat(2, () => lead({ deal_id: nextDealId(), created_at: '2026-05-01 10:00:00', qualified: null })),
];

const afterPeriodLeads = [
  ...repeat(2, () => lead({ deal_id: nextDealId(), created_at: '2026-07-01 10:00:00', qualified: true })),
  ...repeat(2, () => lead({ deal_id: nextDealId(), created_at: '2026-07-01 10:00:00', qualified: false })),
];

const ALL_LEADS = [...beforePeriodLeads, ...periodLeads, ...afterPeriodLeads];

assert.strictEqual(periodLeads.length, 209, 'fixture sanity: 209 leads in the historical period');
assert.strictEqual(periodLeads.filter(l => l.qualified === true).length, 25, 'fixture sanity: 25 raw qualified');
assert.strictEqual(periodLeads.filter(l => l.qualified === false).length, 2, 'fixture sanity: 2 raw not-qualified');
assert.strictEqual(periodLeads.filter(l => l.qualified === null).length, 182, 'fixture sanity: 182 pending');

const CORRECTIONS = {
  periods: [
    {
      start: '2026-05-25',
      end: '2026-06-21',
      qualified_total: 146,
      source: 'manual_verified',
      crm_detected: 25,
      adjustment: 121,
      note: 'Verified historical qualified lead count.',
    },
  ],
};

const PERIOD_SINCE = '2026-05-25';
const PERIOD_UNTIL = '2026-06-21';

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

console.log('Normalized qualified layer — test suite\n');

test('TEST 1: 25.05.2026 -> 21.06.2026 normalizes to 146', () => {
  const r = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  assert.strictEqual(r.normalizedQualified, 146);
});

test('TEST 2: crmDetected=25, verified=146, adjustment=121, normalized=146', () => {
  const r = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  assert.strictEqual(r.crmDetected, 25);
  assert.strictEqual(r.historicalVerified, 146);
  assert.strictEqual(r.historicalAdjustment, 121);
  assert.strictEqual(r.normalizedQualified, 146);
});

test('TEST 3: no double counting — segments sum exactly to the All Time total, calls are idempotent', () => {
  const beforeSeg = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-05-01', '2026-05-24', 'all', CAMPAIGN_LANG);
  const periodSeg = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  const afterSeg = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-06-22', '2026-07-01', 'all', CAMPAIGN_LANG);
  const allTime = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-05-01', '2026-07-01', 'all', CAMPAIGN_LANG);

  assert.strictEqual(beforeSeg.normalizedQualified + periodSeg.normalizedQualified + afterSeg.normalizedQualified, allTime.normalizedQualified);

  const call1 = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  const call2 = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  assert.deepStrictEqual(call1, call2, 'repeated calls must be pure/idempotent, never accumulate');
});

test('TEST 4: All Time uses normalized data (sum of normalized segments, not raw + global adjustment)', () => {
  const allTime = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-05-01', '2026-07-01', 'all', CAMPAIGN_LANG);
  // 3 real (before) + 146 verified (period) + 2 real (after) = 151
  assert.strictEqual(allTime.normalizedQualified, 151);
  // Must NOT equal raw-all-time (30) plus the old global +159/+121 adjustments.
  assert.notStrictEqual(allTime.normalizedQualified, 30 + 159);
  assert.notStrictEqual(allTime.normalizedQualified, 30 + 121);
});

test('TEST 5: historical correction does not apply after 21.06', () => {
  const r = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-06-22', '2026-07-05', 'all', CAMPAIGN_LANG);
  assert.strictEqual(r.coveredPeriods.length, 0);
  assert.strictEqual(r.historicalAdjustment, 0);
  assert.strictEqual(r.normalizedQualified, 2); // real leads on 2026-07-01
});

test('TEST 6: historical correction does not apply before 25.05', () => {
  const r = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-05-01', '2026-05-24', 'all', CAMPAIGN_LANG);
  assert.strictEqual(r.coveredPeriods.length, 0);
  assert.strictEqual(r.historicalAdjustment, 0);
  assert.strictEqual(r.normalizedQualified, 3); // real leads on 2026-05-01
});

test('TEST 7: partial overlap does NOT prorate 146 — falls back to real CRM data + warning', () => {
  const r = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, '2026-06-01', '2026-06-10', 'all', CAMPAIGN_LANG);
  assert.strictEqual(r.partialPeriods.length, 1);
  assert.ok(r.warnings.length >= 1);
  assert.strictEqual(r.normalizedQualified, 10); // real qualified leads created 06-02 and 06-05
  const forbiddenProportion = Math.round(146 / 28 * 10);
  assert.notStrictEqual(r.normalizedQualified, forbiddenProportion, 'must never invent a proportional split');
});

test('TEST 8: unmatched historical qualified is never assigned to a random campaign', () => {
  const b = QualifiedCore.qualifiedBreakdown(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'campaign_id', CAMPAIGN_LANG);
  assert.strictEqual(b.total, 146);
  assert.strictEqual(b.matched, 15); // 5 campRU (05-27) + 5 campRU (06-02) + 5 campEN (06-15)
  assert.strictEqual(b.unmatched, 131); // 10 real unmatched + 121 historical adjustment
});

test('TEST 9: unmatched historical qualified is never assigned to a random RU/EN flow', () => {
  const ru = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'RU', CAMPAIGN_LANG);
  const en = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'EN', CAMPAIGN_LANG);
  const all = QualifiedCore.getNormalizedQualifiedData(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'all', CAMPAIGN_LANG);
  assert.strictEqual(ru.normalizedQualified, 10); // real RU-matched leads only, no historical layer applied
  assert.strictEqual(en.normalizedQualified, 5); // real EN-matched leads only
  assert.notStrictEqual(ru.normalizedQualified + en.normalizedQualified, all.normalizedQualified);
});

test('TEST 10: unmatched historical qualified is never assigned to a random adset', () => {
  const b = QualifiedCore.qualifiedBreakdown(ALL_LEADS, CORRECTIONS, PERIOD_SINCE, PERIOD_UNTIL, 'adset_id', CAMPAIGN_LANG);
  assert.strictEqual(b.total, 146);
  assert.strictEqual(b.matched, 15);
  assert.strictEqual(b.unmatched, 131);
});

test('TEST 11: video creative preview — not applicable, feature does not exist in this codebase', () => {
  // fetch_report.py / index.html contain no video_id / video_url / <video>
  // handling at all in this repository, so there is nothing this change
  // could regress. Recorded here so the acceptance-criteria list stays
  // complete and honest rather than silently skipped.
  assert.ok(true);
});

test('TEST 12: duplicate deal_id is counted only once', () => {
  const dupFixture = [
    lead({ deal_id: 'DUP1', created_at: '2026-06-01 10:00:00', qualified: true }),
    lead({ deal_id: 'DUP1', created_at: '2026-06-01 10:00:00', qualified: true }),
    lead({ deal_id: 'DUP2', created_at: '2026-06-01 10:00:00', qualified: true }),
  ];
  const raw = QualifiedCore.rawCrmLeadsInRange(dupFixture, '2026-06-01', '2026-06-01', null, null);
  assert.strictEqual(raw.qualified, 3, 'sanity: without dedup the raw fixture has 3 rows');

  const deduped = QualifiedCore.dedupeCrmLeads(dupFixture);
  assert.strictEqual(deduped.leads.length, 2);
  assert.deepStrictEqual(deduped.duplicateDealIds, ['DUP1']);

  const dedupedRaw = QualifiedCore.rawCrmLeadsInRange(deduped.leads, '2026-06-01', '2026-06-01', null, null);
  assert.strictEqual(dedupedRaw.qualified, 2, 'DUP1 must only be counted once');
});

console.log('\n' + passed + ' tests passed.');
