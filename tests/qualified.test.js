'use strict';

const assert = require('assert');
const QualifiedCore = require('../qualified-core.js');

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`PASS: ${name}`);
  } catch (err) {
    failed++;
    console.log(`FAIL: ${name}`);
    console.log(`  ${err.message}`);
  }
}

function lead(dealId, date, qualified, opts) {
  opts = opts || {};
  return {
    deal_id: dealId,
    created_at: date === null ? null : `${date} 12:00:00`,
    qualified: qualified,
    campaign_id: opts.campaign_id || null,
    adset_id: opts.adset_id || null,
    ad_id: opts.ad_id || null,
    stage: opts.stage || '',
    country: opts.country || '',
  };
}

function makeQualifiedBatch(prefix, count, date, opts) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push(lead(`${prefix}-${i}`, date, true, opts));
  }
  return out;
}

const HISTORICAL_SINCE = '2026-05-25';
const HISTORICAL_UNTIL = '2026-06-21';
const HISTORICAL_DATE = '2026-06-10'; // any single date inside the historical period

const CORRECTIONS = {
  periods: [
    {
      since: HISTORICAL_SINCE,
      until: HISTORICAL_UNTIL,
      qualified_total: 146,
      raw_detected: 25,
      adjustment: 121,
      note: 'CRM verified historical total',
    },
  ],
  unresolved: [
    { amount: 38, status: 'needs_period_confirmation' },
  ],
};

// TEST 1: exact-match historical range -> verified total, not raw + correction.
test('TEST 1: 25.05-21.06 exact range returns verified 146, not 25', () => {
  const leads = makeQualifiedBatch('t1', 25, HISTORICAL_DATE, { campaign_id: 'camp-1' });
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'all' });
  assert.strictEqual(result.rawQualified, 25);
  assert.strictEqual(result.normalizedQualified, 146);
  assert.strictEqual(result.correctionApplied, true);
  assert.strictEqual(result.appliedPeriods.length, 1);
});

// TEST 2: all-time = ordinary sum of normalized periods, no double counting.
test('TEST 2: all-time sums before + verified period + after = 176, not 10+25+121+146', () => {
  const before = makeQualifiedBatch('t2-before', 10, '2026-04-01', {});
  const period = makeQualifiedBatch('t2-period', 25, HISTORICAL_DATE, { campaign_id: 'camp-1' });
  const after = makeQualifiedBatch('t2-after', 20, '2026-07-15', {});
  const leads = [...before, ...period, ...after];
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, '2026-03-01', '2026-08-19', { flow: 'all' });
  assert.strictEqual(result.normalizedQualified, 176);
  assert.notStrictEqual(result.normalizedQualified, 10 + 25 + 121 + 146);
  assert.notStrictEqual(result.normalizedQualified, 10 + 146 + 121 + 20);
});

// TEST 3: partial overlap (start cut off) -> raw CRM, no correction, warning surfaced.
test('TEST 3: 01.06-21.06 partial overlap falls back to raw CRM with a warning', () => {
  const leads = makeQualifiedBatch('t3', 25, HISTORICAL_DATE, {});
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, '2026-06-01', '2026-06-21', { flow: 'all' });
  assert.strictEqual(result.correctionApplied, false);
  assert.strictEqual(result.normalizedQualified, result.rawQualified);
  assert.strictEqual(result.normalizedQualified, 25);
  assert.ok(result.partialOverlapWarnings.length > 0);
});

// TEST 4: partial overlap (end cut off) -> raw CRM, no correction, warning surfaced.
test('TEST 4: 25.05-15.06 partial overlap falls back to raw CRM with a warning', () => {
  const leads = makeQualifiedBatch('t4', 25, HISTORICAL_DATE, {});
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, '2026-06-15', { flow: 'all' });
  assert.strictEqual(result.correctionApplied, false);
  assert.strictEqual(result.normalizedQualified, result.rawQualified);
  assert.strictEqual(result.normalizedQualified, 25);
  assert.ok(result.partialOverlapWarnings.length > 0);
});

// TEST 5: historical period sliced by flow=RU must never distribute the correction.
test('TEST 5: 25.05-21.06 + flow=RU never distributes the historical correction', () => {
  const ruLeads = makeQualifiedBatch('t5-ru', 4, HISTORICAL_DATE, { campaign_id: 'camp-ru' });
  const enLeads = makeQualifiedBatch('t5-en', 21, HISTORICAL_DATE, { campaign_id: 'camp-en' });
  const leads = [...ruLeads, ...enLeads];
  const campaignLangById = { 'camp-ru': 'RU', 'camp-en': 'EN' };
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'RU', campaignLangById });
  assert.strictEqual(result.correctionApplied, false);
  assert.strictEqual(result.normalizedQualified, 4);
  assert.notStrictEqual(result.normalizedQualified, 146);
});

// TEST 6: unmatched historical qualified must stay in the global KPI, only entity attribution excludes it.
test('TEST 6: unmatched historical qualified stays in global KPI, excluded only from entity attribution', () => {
  const matched = makeQualifiedBatch('t6-matched', 10, HISTORICAL_DATE, { campaign_id: 'camp-1', ad_id: 'ad-1' });
  const unmatched = makeQualifiedBatch('t6-unmatched', 15, HISTORICAL_DATE, {});
  const leads = [...matched, ...unmatched];
  const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'all' });
  assert.strictEqual(result.normalizedQualified, 146);

  const attributed = QualifiedCore.attributedQualifiedTotal(leads, HISTORICAL_SINCE, HISTORICAL_UNTIL);
  assert.strictEqual(attributed, 10);

  const unattributed = QualifiedCore.unattributedHistoricalCount(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'all' });
  assert.strictEqual(unattributed, 146 - 10);
});

// TEST 7: duplicate deal_id must only be counted once.
test('TEST 7: duplicate deal_id is not counted twice', () => {
  const leads = [
    lead('dup-1', '2026-07-01', true, {}),
    lead('dup-1', '2026-07-01', true, {}), // exact duplicate row
  ];
  const count = QualifiedCore.rawQualifiedCount(leads, '2026-07-01', '2026-07-01', 'all', {});
  assert.strictEqual(count, 1);
});

// TEST 8: empty created_at must not throw or corrupt an all-time computation.
// Range deliberately starts right after the historical correction period so
// this test isolates null-date handling from the correction-application path
// (which is already covered by TEST 1/TEST 2).
test('TEST 8: empty created_at does not break all-time', () => {
  const leads = [
    lead('noDate-1', null, true, {}),
    lead('withDate-1', '2026-07-01', true, {}),
  ];
  assert.doesNotThrow(() => {
    const result = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, '2026-06-22', '2026-12-31', { flow: 'all' });
    assert.strictEqual(result.normalizedQualified, 1);
  });
});

// TEST 9: overlapping correction periods must be rejected, not silently double counted.
test('TEST 9: overlapping correction periods produce a validation error and are not applied', () => {
  const overlapping = {
    periods: [
      { since: '2026-05-25', until: '2026-06-21', qualified_total: 146, raw_detected: 25, adjustment: 121 },
      { since: '2026-06-15', until: '2026-06-30', qualified_total: 50, raw_detected: 5, adjustment: 45 },
    ],
    unresolved: [],
  };
  const leads = makeQualifiedBatch('t9', 30, HISTORICAL_DATE, {});
  const result = QualifiedCore.getNormalizedQualifiedData(leads, overlapping, '2026-05-25', '2026-06-30', { flow: 'all' });
  assert.ok(result.validationErrors.length > 0);
  assert.strictEqual(result.correctionApplied, false);
  assert.strictEqual(result.normalizedQualified, result.rawQualified);
});

// TEST 10: correction is applied exactly once, and repeated calls stay idempotent.
test('TEST 10: correction applies exactly once and is idempotent across repeated calls', () => {
  const leads = makeQualifiedBatch('t10', 25, HISTORICAL_DATE, { campaign_id: 'camp-1' });
  const leadsLengthBefore = leads.length;
  const first = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'all' });
  const second = QualifiedCore.getNormalizedQualifiedData(leads, CORRECTIONS, HISTORICAL_SINCE, HISTORICAL_UNTIL, { flow: 'all' });
  assert.strictEqual(first.normalizedQualified, 146);
  assert.strictEqual(second.normalizedQualified, 146);
  assert.strictEqual(first.appliedPeriods.length, 1);
  assert.strictEqual(leads.length, leadsLengthBefore); // input must not be mutated
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
