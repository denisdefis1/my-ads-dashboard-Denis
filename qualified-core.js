/*
 * Single canonical "Qualified" calculation engine, shared between the
 * dashboard (index.html, loaded via <script src="qualified-core.js">) and
 * the Node test suite (tests/qualified-core.test.js, via require()).
 *
 * Every view in the dashboard (main KPI, "Всё время", month, custom ranges,
 * weekly comparison, campaigns, adsets, ads, RU/EN, countries) must compute
 * "Qualified" through qualifiedInRange() below — there must be no second,
 * divergent counting path.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.QualifiedCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function leadDate(lead) {
    return lead && lead.created_at ? lead.created_at.slice(0, 10) : null;
  }

  // Historical verified leads without a confirmed campaign/ad attribution
  // (l.campaign_id / l.ad_id absent) intentionally stay eligible for the
  // main KPI here — attribution filtering (by idField/flow) happens only
  // where the caller explicitly passes a flow or asks for per-entity counts.
  function crmDateBounds(leads) {
    var earliest = null;
    var latest = null;
    (leads || []).forEach(function (l) {
      var d = leadDate(l);
      if (!d) return;
      if (earliest === null || d < earliest) earliest = d;
      if (latest === null || d > latest) latest = d;
    });
    if (earliest === null) return null;
    return { earliest: earliest, latest: latest };
  }

  // "Всё время" must not be a hardcoded number or a magic presetKey==='all'
  // check — it is defined as: does this query range cover the full CRM
  // history? That is true for the "Всё время" preset today, and stays true
  // automatically as new leads arrive, without touching this logic.
  function isFullHistoryRange(leads, since, until) {
    var bounds = crmDateBounds(leads);
    if (!bounds) return false;
    return since <= bounds.earliest && until >= bounds.latest;
  }

  function rawQualifiedInRange(leads, since, until, flow, campaignLangById) {
    return (leads || []).filter(function (l) {
      var d = leadDate(l);
      if (!d || d < since || d > until) return false;
      if (l.qualified !== true) return false;
      if (flow && flow !== 'all') {
        return !!campaignLangById && campaignLangById[l.campaign_id] === flow;
      }
      return true;
    }).length;
  }

  // Corrections are never attributed to a language flow — they only ever
  // apply to the unfiltered ('all') view, exactly like real CRM leads
  // without confirmed campaign/ad attribution are never force-assigned to
  // a campaign.
  function applyCorrections(rawQualified, leads, since, until, flow, corrections) {
    var appliedCorrectionIds = [];
    if (flow && flow !== 'all') {
      return { qualified: rawQualified, appliedCorrectionIds: appliedCorrectionIds };
    }
    var qualified = rawQualified;
    (corrections || []).forEach(function (c) {
      if (c.type === 'override' && c.period_since && c.period_until) {
        // Only applied when the requested range fully contains the
        // correction period — we don't have a daily breakdown of the
        // verified count, so partial overlaps intentionally fall back to
        // the raw CRM count instead of guessing a proportional split.
        if (since <= c.period_since && until >= c.period_until) {
          var rawInPeriod = rawQualifiedInRange(leads, c.period_since, c.period_until, 'all', null);
          qualified += (c.verified_qualified - rawInPeriod);
          appliedCorrectionIds.push(c.id);
        }
      } else if (c.type === 'addend' && c.scope === 'all_time_only') {
        if (isFullHistoryRange(leads, since, until)) {
          qualified += c.amount;
          appliedCorrectionIds.push(c.id);
        }
      }
    });
    return { qualified: qualified, appliedCorrectionIds: appliedCorrectionIds };
  }

  function qualifiedInRange(leads, since, until, flow, campaignLangById, corrections) {
    var raw = rawQualifiedInRange(leads, since, until, flow, campaignLangById);
    var corrected = applyCorrections(raw, leads, since, until, flow, corrections);
    return { raw: raw, qualified: corrected.qualified, appliedCorrectionIds: corrected.appliedCorrectionIds };
  }

  return {
    crmDateBounds: crmDateBounds,
    isFullHistoryRange: isFullHistoryRange,
    rawQualifiedInRange: rawQualifiedInRange,
    applyCorrections: applyCorrections,
    qualifiedInRange: qualifiedInRange,
  };
});
