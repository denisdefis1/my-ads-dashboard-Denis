'use strict';
const assert = require('assert');
const path = require('path');
const QualifiedCore = require(path.join(__dirname, '..', 'qualified-core.js'));

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`ok - ${name}`);
  } catch (err) {
    failed++;
    console.error(`FAIL - ${name}`);
    console.error(err);
  }
}

function lead(overrides) {
  return Object.assign({
    deal_id: '1',
    created_at: null,
    qualified: null,
    campaign_id: null,
    ad_id: null,
    adset_id: null,
  }, overrides);
}

// --- crmLeadsInRange ---------------------------------------------------

test('crmLeadsInRange: counts qualified/notQualified/pending within range', () => {
  const leads = [
    lead({ deal_id: '1', created_at: '2026-08-05 10:00:00', qualified: true }),
    lead({ deal_id: '2', created_at: '2026-08-10 10:00:00', qualified: false }),
    lead({ deal_id: '3', created_at: '2026-08-15 10:00:00', qualified: null }),
    lead({ deal_id: '4', created_at: '2026-07-31 10:00:00', qualified: true }), // out of range
    lead({ deal_id: '5', created_at: '2026-09-01 10:00:00', qualified: true }), // out of range
  ];
  const stats = QualifiedCore.crmLeadsInRange(leads, '2026-08-01', '2026-08-31');
  assert.strictEqual(stats.total, 3);
  assert.strictEqual(stats.qualified, 1);
  assert.strictEqual(stats.notQualified, 1);
  assert.strictEqual(stats.pending, 1);
});

test('crmLeadsInRange: a qualified lead with created_at null is excluded from every range (root cause of issue #19 lives upstream in fetch_crm.py date parsing, not here)', () => {
  const leads = [lead({ deal_id: '1', created_at: null, qualified: true })];
  const stats = QualifiedCore.crmLeadsInRange(leads, '2026-01-01', '2026-12-31');
  assert.strictEqual(stats.qualified, 0);
});

test('crmLeadsInRange: filters by flow using campaignLangById', () => {
  const leads = [
    lead({ deal_id: '1', created_at: '2026-08-05 10:00:00', qualified: true, campaign_id: 'ru1' }),
    lead({ deal_id: '2', created_at: '2026-08-05 10:00:00', qualified: true, campaign_id: 'en1' }),
  ];
  const campaignLangById = { ru1: 'RU', en1: 'EN' };
  const ruStats = QualifiedCore.crmLeadsInRange(leads, '2026-08-01', '2026-08-31', { flow: 'RU', campaignLangById });
  assert.strictEqual(ruStats.qualified, 1);
  const allStats = QualifiedCore.crmLeadsInRange(leads, '2026-08-01', '2026-08-31', { flow: 'all', campaignLangById });
  assert.strictEqual(allStats.qualified, 2);
});

test('crmLeadsInRange: no leads returns empty stats, not a crash', () => {
  const stats = QualifiedCore.crmLeadsInRange([], '2026-08-01', '2026-08-31');
  assert.deepStrictEqual(stats, { total: 0, qualified: 0, notQualified: 0, pending: 0 });
});

// --- applyQualCorrections ----------------------------------------------

test('applyQualCorrections: replace mode swaps raw count for verified value when the correction period is exactly the queried range (issue #19 section 9: 25 -> 146, not 25+146)', () => {
  const leads = [];
  for (let i = 0; i < 25; i++) {
    leads.push(lead({ deal_id: String(i), created_at: '2026-06-01 10:00:00', qualified: true }));
  }
  const corrections = [
    { since: '2026-05-25', until: '2026-06-21', mode: 'replace', verified_qualified: 146 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-05-25', '2026-06-21');
  assert.strictEqual(raw.qualified, 25);
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-05-25', '2026-06-21', raw);
  assert.strictEqual(corrected.qualified, 146);
});

test('applyQualCorrections: correction period fully inside a wider "all time" range still applies exactly once (issue #19 section 11)', () => {
  const leads = [];
  for (let i = 0; i < 25; i++) {
    leads.push(lead({ deal_id: `period-${i}`, created_at: '2026-06-01 10:00:00', qualified: true }));
  }
  leads.push(lead({ deal_id: 'outside', created_at: '2026-08-05 10:00:00', qualified: true }));
  const corrections = [
    { since: '2026-05-25', until: '2026-06-21', mode: 'replace', verified_qualified: 146 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-01-01', '2026-12-31');
  assert.strictEqual(raw.qualified, 26); // 25 in the correction period + 1 outside
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-01-01', '2026-12-31', raw);
  // 26 raw - 25 raw-in-period + 146 verified = 147 (the 1 outside lead is untouched)
  assert.strictEqual(corrected.qualified, 147);
});

test('applyQualCorrections: correction NOT applied when queried range does not fully contain the correction period (no invented partial credit)', () => {
  const leads = [lead({ deal_id: '1', created_at: '2026-06-10 10:00:00', qualified: true })];
  const corrections = [
    { since: '2026-05-25', until: '2026-06-21', mode: 'replace', verified_qualified: 146 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-06-05', '2026-06-15');
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-06-05', '2026-06-15', raw);
  assert.strictEqual(corrected.qualified, raw.qualified);
});

test('applyQualCorrections: August gets no historical correction (issue #19 section 8 — August must show only real CRM data)', () => {
  const leads = [lead({ deal_id: '1', created_at: '2026-08-10 10:00:00', qualified: true })];
  const corrections = [
    { since: '2026-05-25', until: '2026-06-21', mode: 'replace', verified_qualified: 146 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-08-01', '2026-08-31');
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-08-01', '2026-08-31', raw);
  assert.strictEqual(corrected.qualified, 1);
  assert.strictEqual(corrected.qualified, raw.qualified);
});

test('applyQualCorrections: add mode applies its own amount once, independent from a replace correction in another period (issue #19 section 10: +38 must not merge with +121)', () => {
  const leads = [lead({ deal_id: '1', created_at: '2026-01-05 10:00:00', qualified: true })];
  const corrections = [
    { since: '2026-05-25', until: '2026-06-21', mode: 'replace', verified_qualified: 146 },
    { since: '2026-01-01', until: '2026-01-31', mode: 'add', amount: 38 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-01-01', '2026-01-31');
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-01-01', '2026-01-31', raw);
  assert.strictEqual(corrected.qualified, 1 + 38);
});

test('applyQualCorrections: a correction without since/until (unresolved period) is never applied — never invent dates', () => {
  const leads = [lead({ deal_id: '1', created_at: '2026-08-10 10:00:00', qualified: true })];
  const corrections = [
    { since: null, until: null, mode: 'add', amount: 38 },
  ];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-01-01', '2026-12-31');
  const corrected = QualifiedCore.applyQualCorrections(leads, corrections, '2026-01-01', '2026-12-31', raw);
  assert.strictEqual(corrected.qualified, raw.qualified);
});

test('applyQualCorrections: no corrections is a no-op', () => {
  const leads = [lead({ deal_id: '1', created_at: '2026-08-10 10:00:00', qualified: true })];
  const raw = QualifiedCore.crmLeadsInRange(leads, '2026-08-01', '2026-08-31');
  const corrected = QualifiedCore.applyQualCorrections(leads, [], '2026-08-01', '2026-08-31', raw);
  assert.strictEqual(corrected.qualified, raw.qualified);
});

// --- crmQualifiedByEntity ------------------------------------------------

test('crmQualifiedByEntity: counts only qualified===true, grouped by the given id field, within range', () => {
  const leads = [
    lead({ deal_id: '1', created_at: '2026-08-05 10:00:00', qualified: true, ad_id: 'adA' }),
    lead({ deal_id: '2', created_at: '2026-08-06 10:00:00', qualified: true, ad_id: 'adA' }),
    lead({ deal_id: '3', created_at: '2026-08-06 10:00:00', qualified: false, ad_id: 'adA' }),
    lead({ deal_id: '4', created_at: '2026-08-06 10:00:00', qualified: true, ad_id: 'adB' }),
    lead({ deal_id: '5', created_at: '2026-08-06 10:00:00', qualified: true, ad_id: null }),
  ];
  const map = QualifiedCore.crmQualifiedByEntity(leads, '2026-08-01', '2026-08-31', 'ad_id');
  assert.deepStrictEqual(map, { adA: 2, adB: 1 });
});

test('crmQualifiedByEntity: empty leads returns empty map, not a crash', () => {
  assert.deepStrictEqual(QualifiedCore.crmQualifiedByEntity(null, '2026-08-01', '2026-08-31', 'ad_id'), {});
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
