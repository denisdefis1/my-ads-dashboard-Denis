// Run with: node tests/qualified-core.test.js
// No dependencies beyond Node's built-in assert module.
const assert = require('node:assert/strict');
const path = require('node:path');
const QualifiedCore = require(path.join(__dirname, '..', 'qualified-core.js'));

const HISTORICAL = [
  { since: '2026-05-25', until: '2026-06-21', verified_qualified: 146, note: 'test period' },
];

function lead(id, date, qualified, extra) {
  return Object.assign({ deal_id: id, created_at: date, qualified, ad_id: null, campaign_id: null }, extra || {});
}

// Synthetic CRM dataset:
// - 25 raw qualified leads inside the verified historical period (matches
//   the issue's "CRM automatically detected only 25" for 2026-05-25..2026-06-21)
// - 4 qualified leads in August 2026 (outside the historical period)
// - 1 unmatched qualified lead (no ad_id/campaign_id) in August
// - 2 qualified leads in an unrelated month (March)
// - 1 not-qualified and 1 pending lead for variety
const leads = [];
for (let i = 0; i < 25; i++) {
  leads.push(lead('hist-' + i, '2026-06-0' + (1 + (i % 9)) + ' 10:00:00', true));
}
for (let i = 0; i < 4; i++) {
  leads.push(lead('aug-' + i, '2026-08-0' + (i + 1) + ' 10:00:00', true));
}
leads.push(lead('unmatched-1', '2026-08-02 12:00:00', true));
leads.push(lead('other-1', '2026-03-10 10:00:00', true));
leads.push(lead('other-2', '2026-03-11 10:00:00', true));
leads.push(lead('notqual-1', '2026-08-03 10:00:00', false));
leads.push(lead('pending-1', '2026-08-04 10:00:00', null));

let passed = 0;
function check(name, condition) {
  assert.ok(condition, name);
  passed++;
  console.log('PASS: ' + name);
}

// 1. Exact historical range => 146, applied exactly once (not 25, not 25+121, not 159)
{
  const stats = QualifiedCore.getQualifiedStats(leads, '2026-05-25', '2026-06-21', HISTORICAL);
  check('historical period returns exactly 146', stats.qualified === 146);
  check(
    'historical period correction applied exactly once',
    stats.correctionsApplied.length === 1 && stats.correctionsApplied[0].mode === 'exact'
  );
}

// 2. All-time (range fully containing the historical period) => raw outside + 146, no double count
{
  const since = '2026-01-01';
  const until = '2026-12-31';
  const rawOutside = leads.filter(l =>
    l.qualified === true && (l.created_at < HISTORICAL[0].since || l.created_at > HISTORICAL[0].until)
  ).length;
  const stats = QualifiedCore.getQualifiedStats(leads, since, until, HISTORICAL);
  check('all-time does not double count the historical period', stats.qualified === rawOutside + 146);
  check(
    'all-time correction applied exactly once',
    stats.correctionsApplied.length === 1 && stats.correctionsApplied[0].mode === 'contains'
  );
}

// 3. August range (outside historical period) => real raw CRM data, no correction
{
  const stats = QualifiedCore.getQualifiedStats(leads, '2026-08-01', '2026-08-31', HISTORICAL);
  check('August uses real raw CRM data (4 dated + 1 unmatched)', stats.qualified === 5);
  check('August range triggers no correction', stats.correctionsApplied.length === 0 && stats.warning === null);
}

// 4. Range fully outside historical period => raw CRM count
{
  const stats = QualifiedCore.getQualifiedStats(leads, '2026-03-01', '2026-03-31', HISTORICAL);
  check('range outside historical period uses raw count', stats.qualified === 2 && stats.correctionsApplied.length === 0);
}

// 5. Partial overlap => raw count kept, warning surfaced, no invented proportional value
{
  const stats = QualifiedCore.getQualifiedStats(leads, '2026-06-10', '2026-07-10', HISTORICAL);
  const rawPartial = leads.filter(l =>
    l.qualified === true && l.created_at >= '2026-06-10' && l.created_at <= '2026-07-10'
  ).length;
  check('partial overlap keeps raw value, no invented proportion', stats.qualified === rawPartial);
  check('partial overlap surfaces a warning', typeof stats.warning === 'string' && stats.warning.length > 0);
  check('partial overlap does not record a correction as applied', stats.correctionsApplied.length === 0);
}

// 6. Main KPI counts unmatched qualified leads; entity/attribution views must not
{
  const since = '2026-08-01';
  const until = '2026-08-31';
  const mainKpi = QualifiedCore.getQualifiedStats(leads, since, until, HISTORICAL);
  const attributedOnly = QualifiedCore.getQualifiedStats(leads, since, until, HISTORICAL, {
    filter: l => !!(l.ad_id || l.campaign_id),
  });
  check('main KPI includes unmatched qualified leads', mainKpi.qualified > attributedOnly.qualified);
}

// 7. Overlapping correction periods are rejected, never silently stacked
{
  const overlapping = [
    { since: '2026-05-25', until: '2026-06-21', verified_qualified: 146 },
    { since: '2026-06-10', until: '2026-06-30', verified_qualified: 50 },
  ];
  assert.throws(
    () => QualifiedCore.getQualifiedStats(leads, '2026-01-01', '2026-12-31', overlapping),
    /overlap/i
  );
  passed++;
  console.log('PASS: overlapping correction periods are rejected');
}

// 8. Flow-filtered ranges never receive the historical correction (no confirmed per-flow attribution)
{
  const flowLeads = leads.map(l => Object.assign({}, l, { campaign_id: 'camp-ru' }));
  const stats = QualifiedCore.getQualifiedStats(flowLeads, '2026-05-25', '2026-06-21', HISTORICAL, {
    filter: l => l.campaign_id === 'camp-ru',
    applyCorrections: false,
  });
  check('flow-filtered exact historical range does not get the correction', stats.qualified === 25);
}

console.log(`\n${passed} assertions passed.`);
