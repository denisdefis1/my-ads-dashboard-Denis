/**
 * Normalized qualified-leads layer.
 *
 * Single source of truth for turning raw CRM leads + historical verified
 * corrections into one "qualified" number, used by every section of the
 * dashboard (main cards, all time, custom ranges, campaigns, RU/EN,
 * audiences, creatives, data-quality diagnostics).
 *
 * Pure functions only (no DOM/browser globals) so this file can be loaded
 * both by index.html (<script src="qualified-core.js">) and by the Node
 * test suite (tests/qualified.test.js) without any duplicated logic.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.QualifiedCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Dedupe CRM leads by deal_id — a deal_id must only ever be counted once,
  // no matter how many times it appears in the raw export.
  function dedupeCrmLeads(leads) {
    const seen = new Set();
    const duplicateDealIds = new Set();
    const result = [];
    (leads || []).forEach((lead) => {
      const id = lead && lead.deal_id;
      if (id) {
        if (seen.has(id)) {
          duplicateDealIds.add(id);
          return;
        }
        seen.add(id);
      }
      result.push(lead);
    });
    return { leads: result, duplicateDealIds: Array.from(duplicateDealIds) };
  }

  // Historical verified periods, e.g. data/qual_corrections.json:
  //   { "periods": [{ "start": "2026-05-25", "end": "2026-06-21", "qualified_total": 146, ... }] }
  // Only well-formed entries (start/end/qualified_total present, start <= end)
  // are used — anything else (e.g. unresolved corrections with no confirmed
  // date range) is intentionally ignored by the normalization layer so we
  // never guess a distribution.
  function getHistoricalPeriods(corrections) {
    const periods = (corrections && Array.isArray(corrections.periods)) ? corrections.periods : [];
    return periods.filter((p) => p && p.start && p.end && typeof p.qualified_total === 'number' && p.start <= p.end);
  }

  function inDateRange(dateStr, since, until) {
    if (!dateStr) return false;
    const d = dateStr.slice(0, 10);
    return d >= since && d <= until;
  }

  // Raw (non-normalized) CRM counts for a range. This is the "honest"
  // CRM classifier output — never modified to inflate qualified counts.
  function rawCrmLeadsInRange(leads, since, until, flow, campaignLangById) {
    const rows = (leads || []).filter((l) => {
      if (!inDateRange(l.created_at, since, until)) return false;
      if (flow && flow !== 'all') {
        return (campaignLangById || {})[l.campaign_id] === flow;
      }
      return true;
    });
    return {
      total: rows.length,
      qualified: rows.filter((l) => l.qualified === true).length,
      notQualified: rows.filter((l) => l.qualified === false).length,
      pending: rows.filter((l) => l.qualified === null).length,
      leads: rows,
    };
  }

  // How a historical period relates to the queried range:
  //  - 'none'    no overlap at all
  //  - 'covered' the period is fully inside the range (this also covers an
  //              exact match, since a period equal to the range is trivially
  //              "inside" it) — safe to swap the CRM count for that period
  //              with the verified total.
  //  - 'partial' any other overlap (the range is a strict sub-range of the
  //              period, or the two only partially intersect) — NOT safe to
  //              apply the verified total, since that would require making
  //              up a day-by-day distribution.
  function periodOverlap(period, since, until) {
    const overlaps = period.start <= until && since <= period.end;
    if (!overlaps) return 'none';
    const fullyCoveredByRange = period.start >= since && period.end <= until;
    return fullyCoveredByRange ? 'covered' : 'partial';
  }

  // CRM DATA + HISTORICAL VERIFIED QUALIFIED DATA = NORMALIZED QUALIFIED DATA
  //
  // For every historical period fully covered by [since, until], the raw CRM
  // qualified count for that period's own dates is replaced (not added to)
  // by period.qualified_total. Everything outside covered periods keeps the
  // real CRM qualified count. Partial overlaps never touch the number — they
  // only raise a warning — which is exactly what keeps this safe for "All
  // Time" (a big range that fully covers every historical period exactly
  // once) and for custom ranges of any size.
  function getNormalizedQualifiedData(leads, corrections, since, until, flow, campaignLangById) {
    const rawAll = rawCrmLeadsInRange(leads, since, until, null, null);
    const applyHistorical = !flow || flow === 'all';
    const rawFlow = applyHistorical ? rawAll : rawCrmLeadsInRange(leads, since, until, flow, campaignLangById);

    const result = {
      since,
      until,
      flow: flow || 'all',
      crmDetected: rawFlow.qualified,
      crmTotal: rawFlow.total,
      notQualified: rawFlow.notQualified,
      pending: rawFlow.pending,
      historicalVerified: 0,
      historicalAdjustment: 0,
      normalizedQualified: rawFlow.qualified,
      coveredPeriods: [],
      partialPeriods: [],
      warnings: [],
    };

    if (!applyHistorical) return result;

    const periods = getHistoricalPeriods(corrections);
    let verifiedSum = 0;
    let liveInCoveredPeriods = 0;

    periods.forEach((p) => {
      const kind = periodOverlap(p, since, until);
      if (kind === 'none') return;
      if (kind === 'covered') {
        const live = rawCrmLeadsInRange(leads, p.start, p.end, null, null).qualified;
        verifiedSum += p.qualified_total;
        liveInCoveredPeriods += live;
        result.coveredPeriods.push({
          start: p.start,
          end: p.end,
          qualifiedTotal: p.qualified_total,
          crmDetectedLive: live,
          crmDetectedRecorded: p.crm_detected,
          adjustment: p.qualified_total - live,
          source: p.source,
          note: p.note,
        });
      } else {
        result.partialPeriods.push({
          start: p.start,
          end: p.end,
          qualifiedTotal: p.qualified_total,
          source: p.source,
          note: p.note,
        });
        result.warnings.push(
          'Historical verified data for ' + p.start + ' – ' + p.end + ' (' + p.qualified_total +
          ') exists but the selected range ' + since + ' – ' + until +
          ' overlaps it only partially — real CRM data was used instead of a fabricated proportional split.'
        );
      }
    });

    result.historicalVerified = verifiedSum;
    result.historicalAdjustment = verifiedSum - liveInCoveredPeriods;
    result.normalizedQualified = (rawAll.qualified - liveInCoveredPeriods) + verifiedSum;
    return result;
  }

  // Reconciles the normalized total against real CRM leads that carry
  // idField (campaign_id / adset_id / ad_id). Historical qualified that
  // can't be tied to an entity (no campaign_id etc.) is never spread across
  // entities at random — it always shows up as `unmatched`.
  function qualifiedBreakdown(leads, corrections, since, until, idField, campaignLangById) {
    const normalized = getNormalizedQualifiedData(leads, corrections, since, until, 'all', campaignLangById);
    const matched = (leads || []).filter((l) => l.qualified === true && inDateRange(l.created_at, since, until) && l[idField]).length;
    return {
      total: normalized.normalizedQualified,
      crmDetected: normalized.crmDetected,
      matched: matched,
      unmatched: Math.max(0, normalized.normalizedQualified - matched),
    };
  }

  return {
    dedupeCrmLeads: dedupeCrmLeads,
    getHistoricalPeriods: getHistoricalPeriods,
    rawCrmLeadsInRange: rawCrmLeadsInRange,
    periodOverlap: periodOverlap,
    getNormalizedQualifiedData: getNormalizedQualifiedData,
    qualifiedBreakdown: qualifiedBreakdown,
    inDateRange: inDateRange,
  };
});
