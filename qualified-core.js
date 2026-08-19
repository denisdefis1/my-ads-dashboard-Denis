/**
 * Single normalized source of Qualified Leads for the whole dashboard.
 *
 * CRM (raw "квал" rows) -> historical verification (data/qual_corrections.json)
 * -> normalized qualified dataset (this module) -> UI.
 *
 * Nothing outside this file should compute a "Qualified" number from CRM.leads
 * directly. Every place in index.html that shows Qualified must go through
 * getNormalizedQualifiedData() (global/period totals) or qualifiedByEntity()
 * (campaign/adset/ad breakdowns) so there is exactly one counting rule.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.QualifiedCore = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function leadDateStr(lead) {
    if (!lead || !lead.created_at) return null;
    return String(lead.created_at).slice(0, 10);
  }

  function isQualified(lead) {
    return !!lead && lead.qualified === true;
  }

  function inRange(dateStr, since, until) {
    return dateStr !== null && dateStr >= since && dateStr <= until;
  }

  function matchesFlow(lead, flow, campaignLangById) {
    if (!flow || flow === 'all') return true;
    if (!campaignLangById) return true;
    return campaignLangById[lead.campaign_id] === flow;
  }

  /**
   * Same deal_id must never be counted twice, regardless of how many times
   * it shows up in the raw CRM export (duplicate rows, re-fetches, etc.).
   * Leads without a deal_id are kept as-is (nothing to dedupe against).
   */
  function dedupeByDealId(leads) {
    const seen = new Set();
    const out = [];
    for (let i = 0; i < leads.length; i++) {
      const lead = leads[i];
      const id = lead && lead.deal_id;
      if (id) {
        if (seen.has(id)) continue;
        seen.add(id);
      }
      out.push(lead);
    }
    return out;
  }

  function leadsInRange(leads, since, until, flow, campaignLangById) {
    const filtered = (leads || []).filter(function (l) {
      const d = leadDateStr(l);
      return inRange(d, since, until) && matchesFlow(l, flow, campaignLangById);
    });
    return dedupeByDealId(filtered);
  }

  function rawQualifiedCount(leads, since, until, flow, campaignLangById) {
    return leadsInRange(leads, since, until, flow, campaignLangById).filter(isQualified).length;
  }

  function periodsOverlap(a, b) {
    return a.since <= b.until && a.until >= b.since;
  }

  /**
   * Historical correction periods must never overlap - otherwise the same
   * calendar days could be verified twice and silently double-counted.
   * Overlapping periods are excluded from application entirely (fail safe
   * to raw CRM for those dates) and surfaced as validation errors.
   */
  function validateCorrectionPeriods(periods) {
    const valid = [];
    const errors = [];
    const excluded = new Set();
    for (let i = 0; i < periods.length; i++) {
      for (let j = i + 1; j < periods.length; j++) {
        if (periodsOverlap(periods[i], periods[j])) {
          errors.push(
            'Historical correction periods overlap: ' +
            periods[i].since + '–' + periods[i].until + ' and ' +
            periods[j].since + '–' + periods[j].until +
            '. Both are excluded from application to avoid double counting.'
          );
          excluded.add(i);
          excluded.add(j);
        }
      }
    }
    periods.forEach(function (p, i) {
      if (!excluded.has(i)) valid.push(p);
    });
    return { valid: valid, errors: errors };
  }

  function loadCorrections(correctionsData) {
    const periods = (correctionsData && Array.isArray(correctionsData.periods)) ? correctionsData.periods : [];
    const unresolved = (correctionsData && Array.isArray(correctionsData.unresolved)) ? correctionsData.unresolved : [];
    const validated = validateCorrectionPeriods(periods);
    return {
      periods: periods,
      validPeriods: validated.valid,
      validationErrors: validated.errors,
      unresolved: unresolved,
    };
  }

  function fullyContains(range, period) {
    return period.since >= range.since && period.until <= range.until;
  }

  function overlapsPartially(range, period) {
    return periodsOverlap(range, period) && !fullyContains(range, period);
  }

  /**
   * The single source of Qualified for a date range (+ optional flow).
   *
   * Rules:
   *  - flow !== 'all': corrections are period-level totals with no verified
   *    per-flow split, so they are never applied to a flow-filtered slice -
   *    raw CRM count is returned as-is.
   *  - A correction period fully contained in [since,until]: its raw
   *    qualified count is replaced by qualified_total (not added on top).
   *  - A correction period that only partially overlaps [since,until]: never
   *    applied (no proportional/day-based split) - raw CRM is used and a
   *    warning is returned instead.
   *  - "All time" is just an ordinary range (since = start of history), so
   *    it goes through the exact same code path - no special-cased hardcode.
   */
  function getNormalizedQualifiedData(leads, correctionsData, since, until, opts) {
    opts = opts || {};
    const flow = opts.flow || 'all';
    const campaignLangById = opts.campaignLangById || {};
    const loaded = loadCorrections(correctionsData);

    const rawQualified = rawQualifiedCount(leads, since, until, flow, campaignLangById);

    const result = {
      since: since,
      until: until,
      flow: flow,
      rawQualified: rawQualified,
      normalizedQualified: rawQualified,
      correctionApplied: false,
      appliedPeriods: [],
      partialOverlapWarnings: [],
      validationErrors: loaded.validationErrors,
      unresolved: loaded.unresolved,
    };

    if (flow !== 'all') {
      return result;
    }

    const range = { since: since, until: until };
    const fully = loaded.validPeriods.filter(function (p) { return fullyContains(range, p); });
    const partial = loaded.validPeriods.filter(function (p) { return overlapsPartially(range, p); });

    partial.forEach(function (p) {
      result.partialOverlapWarnings.push(
        'Selected range partially overlaps verified historical period ' + p.since + '–' + p.until +
        '. Using raw CRM count for the requested range instead of the verified total ' +
        '(no artificial day-based split of the correction).'
      );
    });

    if (!fully.length) {
      return result;
    }

    let coveredRaw = 0;
    fully.forEach(function (p) {
      coveredRaw += rawQualifiedCount(leads, p.since, p.until, 'all', campaignLangById);
    });

    const outsideRaw = rawQualified - coveredRaw;
    const verifiedTotal = fully.reduce(function (sum, p) { return sum + p.qualified_total; }, 0);

    result.normalizedQualified = outsideRaw + verifiedTotal;
    result.correctionApplied = true;
    result.appliedPeriods = fully;

    return result;
  }

  /**
   * Raw qualified leads in range that carry a real campaign/ad match -
   * i.e. what entity breakdowns (campaigns/adsets/ads/RU/EN) are allowed to
   * show. This never includes any historical correction amount: a period
   * total like 146 is a verified aggregate, not a set of individually
   * attributable leads, so it cannot be assigned to specific campaigns.
   */
  function attributedQualifiedTotal(leads, since, until) {
    return leadsInRange(leads, since, until, 'all', {}).filter(function (l) {
      return isQualified(l) && (l.ad_id || l.campaign_id);
    }).length;
  }

  /**
   * "Historical qualified without advertising attribution": the gap between
   * the normalized (verified) global total and what can actually be traced
   * to a campaign/adset/ad. Always >= 0 by construction. Used for the data
   * quality panel, never distributed onto individual entities.
   */
  function unattributedHistoricalCount(leads, correctionsData, since, until, opts) {
    const normalized = getNormalizedQualifiedData(leads, correctionsData, since, until, opts);
    const attributed = attributedQualifiedTotal(leads, since, until);
    return Math.max(0, normalized.normalizedQualified - attributed);
  }

  function qualifiedByEntity(leads, since, until, idField, flow, campaignLangById) {
    const rows = leadsInRange(leads, since, until, flow, campaignLangById);
    const map = {};
    rows.forEach(function (l) {
      if (!isQualified(l) || !l[idField]) return;
      map[l[idField]] = (map[l[idField]] || 0) + 1;
    });
    return map;
  }

  function countByEntityAndStage(leads, since, until, idField, stageName, flow, campaignLangById) {
    const rows = leadsInRange(leads, since, until, flow, campaignLangById);
    const map = {};
    rows.forEach(function (l) {
      if (l.stage !== stageName || !l[idField]) return;
      map[l[idField]] = (map[l[idField]] || 0) + 1;
    });
    return map;
  }

  return {
    leadDateStr: leadDateStr,
    isQualified: isQualified,
    dedupeByDealId: dedupeByDealId,
    leadsInRange: leadsInRange,
    rawQualifiedCount: rawQualifiedCount,
    validateCorrectionPeriods: validateCorrectionPeriods,
    loadCorrections: loadCorrections,
    getNormalizedQualifiedData: getNormalizedQualifiedData,
    attributedQualifiedTotal: attributedQualifiedTotal,
    unattributedHistoricalCount: unattributedHistoricalCount,
    qualifiedByEntity: qualifiedByEntity,
    countByEntityAndStage: countByEntityAndStage,
  };
}));
