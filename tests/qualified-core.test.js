const assert = require('assert');
const {
  crmDateBounds,
  isFullHistoryRange,
  rawQualifiedInRange,
  qualifiedInRange,
} = require('../qualified-core.js');

function lead(overrides) {
  return Object.assign({ deal_id: '1', created_at: null, qualified: null, campaign_id: null, ad_id: null, adset_id: null }, overrides);
}

function run(name, fn) {
  fn();
  console.log(`ok - ${name}`);
}

run('crmDateBounds ignores leads without created_at', () => {
  const leads = [
    lead({ created_at: '2026-05-01 10:00:00' }),
    lead({ created_at: '2026-08-19 10:00:00' }),
    lead({ created_at: null }),
  ];
  const bounds = crmDateBounds(leads);
  assert.strictEqual(bounds.earliest, '2026-05-01');
  assert.strictEqual(bounds.latest, '2026-08-19');
});

run('crmDateBounds returns null when nothing has a date', () => {
  assert.strictEqual(crmDateBounds([lead({ created_at: null })]), null);
});

run('rawQualifiedInRange excludes qualified leads with no created_at (the original bug)', () => {
  const leads = [
    lead({ deal_id: 'a', created_at: '2026-08-05 10:00:00', qualified: true }),
    lead({ deal_id: 'b', created_at: null, qualified: true }),
    lead({ deal_id: 'c', created_at: '2026-08-06 10:00:00', qualified: false }),
  ];
  const raw = rawQualifiedInRange(leads, '2026-08-01', '2026-08-31', 'all', {});
  assert.strictEqual(raw, 1);
});

run('rawQualifiedInRange respects language flow attribution', () => {
  const leads = [
    lead({ deal_id: 'a', created_at: '2026-08-05 10:00:00', qualified: true, campaign_id: 'c1' }),
    lead({ deal_id: 'b', created_at: '2026-08-05 10:00:00', qualified: true, campaign_id: 'c2' }),
  ];
  const campaignLangById = { c1: 'EN', c2: 'RU' };
  assert.strictEqual(rawQualifiedInRange(leads, '2026-08-01', '2026-08-31', 'EN', campaignLangById), 1);
  assert.strictEqual(rawQualifiedInRange(leads, '2026-08-01', '2026-08-31', 'RU', campaignLangById), 1);
  assert.strictEqual(rawQualifiedInRange(leads, '2026-08-01', '2026-08-31', 'all', campaignLangById), 2);
});

run('override correction replaces the raw count when the range exactly matches the historical period', () => {
  const leads = [];
  for (let i = 0; i < 25; i++) {
    leads.push(lead({ deal_id: 'p' + i, created_at: '2026-06-01 10:00:00', qualified: true }));
  }
  const corrections = [{ id: 'c1', type: 'override', period_since: '2026-05-25', period_until: '2026-06-21', verified_qualified: 146 }];
  const result = qualifiedInRange(leads, '2026-05-25', '2026-06-21', 'all', {}, corrections);
  assert.strictEqual(result.raw, 25);
  assert.strictEqual(result.qualified, 146);
  assert.deepStrictEqual(result.appliedCorrectionIds, ['c1']);
});

run('override correction does not leak into a disjoint range', () => {
  const leads = [
    lead({ deal_id: 'p1', created_at: '2026-06-01 10:00:00', qualified: true }),
    lead({ deal_id: 'p2', created_at: '2026-08-05 10:00:00', qualified: true }),
  ];
  const corrections = [{ id: 'c1', type: 'override', period_since: '2026-05-25', period_until: '2026-06-21', verified_qualified: 146 }];
  const result = qualifiedInRange(leads, '2026-08-01', '2026-08-31', 'all', {}, corrections);
  assert.strictEqual(result.raw, 1);
  assert.strictEqual(result.qualified, 1);
  assert.deepStrictEqual(result.appliedCorrectionIds, []);
});

run('addend correction (unresolved-period +38) only applies to a full-history range, never a partial one', () => {
  const leads = [
    lead({ deal_id: 'x', created_at: '2026-04-01 10:00:00', qualified: true }),
    lead({ deal_id: 'y', created_at: '2026-08-01 10:00:00', qualified: true }),
  ];
  const corrections = [{ id: 'c2', type: 'addend', amount: 38, scope: 'all_time_only', period_since: null, period_until: null }];

  const allTime = qualifiedInRange(leads, '2026-04-01', '2026-08-19', 'all', {}, corrections);
  assert.strictEqual(allTime.raw, 2);
  assert.strictEqual(allTime.qualified, 2 + 38);
  assert.deepStrictEqual(allTime.appliedCorrectionIds, ['c2']);

  const partial = qualifiedInRange(leads, '2026-08-01', '2026-08-19', 'all', {}, corrections);
  assert.strictEqual(partial.qualified, partial.raw);
  assert.deepStrictEqual(partial.appliedCorrectionIds, []);
});

run('corrections never bleed into a language-filtered (RU/EN) view', () => {
  const leads = [lead({ deal_id: 'z', created_at: '2026-08-01 10:00:00', qualified: true, campaign_id: 'camp1' })];
  const campaignLangById = { camp1: 'EN' };
  const corrections = [{ id: 'c2', type: 'addend', amount: 38, scope: 'all_time_only', period_since: null, period_until: null }];
  const result = qualifiedInRange(leads, '2026-08-01', '2026-08-19', 'EN', campaignLangById, corrections);
  assert.strictEqual(result.qualified, 1);
  assert.deepStrictEqual(result.appliedCorrectionIds, []);
});

run('isFullHistoryRange is true only when the query range fully covers CRM history', () => {
  const leads = [
    lead({ created_at: '2026-03-09 00:00:00' }),
    lead({ created_at: '2026-08-19 00:00:00' }),
  ];
  assert.strictEqual(isFullHistoryRange(leads, '2026-03-09', '2026-08-19'), true);
  assert.strictEqual(isFullHistoryRange(leads, '2026-03-10', '2026-08-19'), false);
  assert.strictEqual(isFullHistoryRange(leads, '2026-03-09', '2026-08-18'), false);
});

console.log('All qualified-core tests passed.');
