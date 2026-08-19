/**
 * qualified-core.js
 *
 * Normalized "qualified leads" logic shared by the whole dashboard, so that
 * every preset, every custom date range and "Всё время" go through exactly
 * the same rules. There is no separate "all-time" code path and no
 * preset-specific hardcoding — "Всё время" is just a date range like any
 * other, wide enough to fully contain the verified historical period.
 *
 * A "historical correction" is a period where the CRM's automatic "Квал"
 * detection is known to be wrong and a human has manually verified the real
 * total. It is applied through the exact same range-comparison logic used
 * everywhere else:
 *
 *   - exact match (selected range === correction period)      -> verified total
 *   - selected range fully contains the correction period      -> raw count
 *     outside the period + verified total for the period (used once)
 *   - selected range partially overlaps the correction period  -> raw CRM
 *     count is kept as-is and a warning is surfaced (no invented
 *     proportional correction)
 *   - no overlap                                                -> raw CRM count
 *
 * Corrections are passed in by the caller (see qual_corrections.json) —
 * this module never hardcodes a specific number itself.
 */
(function (root, factory) {
  if (typeof module === 'object' && typeof module.exports === 'object') {
    module.exports = factory();
  } else {
    root.QualifiedCore = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function dateOnly(value) {
    return value ? value.slice(0, 10) : null;
  }

  function rangesEqual(aSince, aUntil, bSince, bUntil) {
    return aSince === bSince && aUntil === bUntil;
  }

  function fullyContains(outerSince, outerUntil, innerSince, innerUntil) {
    return outerSince <= innerSince && outerUntil >= innerUntil;
  }

  function rangesOverlap(aSince, aUntil, bSince, bUntil) {
    return aSince <= bUntil && bSince <= aUntil;
  }

  /**
   * Throws if any two correction periods overlap each other — corrections
   * must never be allowed to stack on the same lead, so an ambiguous
   * configuration is a hard error rather than a silent double-count.
   */
  function validateCorrections(corrections) {
    const list = corrections || [];
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const a = list[i];
        const b = list[j];
        if (rangesOverlap(a.since, a.until, b.since, b.until)) {
          throw new Error(
            `Historical corrections overlap: ${a.since}..${a.until} and ${b.since}..${b.until}. ` +
            `Overlapping correction periods are not allowed because it would be ambiguous which one applies.`
          );
        }
      }
    }
    return true;
  }

  function filterLeadsInRange(leads, since, until, filterFn) {
    return leads.filter(function (l) {
      const d = dateOnly(l.created_at);
      if (!d) return false;
      if (d < since || d > until) return false;
      if (filterFn && !filterFn(l)) return false;
      return true;
    });
  }

  /**
   * Computes qualified/notQualified/pending for a date range, applying
   * verified historical corrections exactly once and never inventing a
   * proportional value for a partial overlap.
   *
   * @param {Array} leads - CRM leads, each with created_at ('YYYY-MM-DD...'
   *   or null) and qualified (true|false|null).
   * @param {string} since - inclusive 'YYYY-MM-DD'
   * @param {string} until - inclusive 'YYYY-MM-DD'
   * @param {Array} corrections - list of {since, until, verified_qualified, note}
   * @param {Object} [opts]
   * @param {Function} [opts.filter] - additional predicate(lead) => bool (e.g. flow filter)
   * @param {boolean} [opts.applyCorrections=true] - set to false when the
   *   range is filtered to a specific flow (RU/EN) — verified historical
   *   totals have no confirmed per-flow attribution and must not be applied.
   */
  function getQualifiedStats(leads, since, until, corrections, opts) {
    opts = opts || {};
    const filterFn = opts.filter || null;
    const applyCorrections = opts.applyCorrections !== false;
    const list = corrections || [];

    if (applyCorrections) {
      validateCorrections(list);
    }

    const inRange = filterLeadsInRange(leads, since, until, filterFn);
    const rawQualified = inRange.filter(function (l) { return l.qualified === true; }).length;

    const result = {
      total: inRange.length,
      qualified: rawQualified,
      notQualified: inRange.filter(function (l) { return l.qualified === false; }).length,
      pending: inRange.filter(function (l) { return l.qualified === null; }).length,
      correctionsApplied: [],
      warning: null,
    };

    if (!applyCorrections) {
      return result;
    }

    let qualified = rawQualified;

    list.forEach(function (period) {
      const exact = rangesEqual(since, until, period.since, period.until);
      const containsPeriod = fullyContains(since, until, period.since, period.until);
      const overlapsPeriod = rangesOverlap(since, until, period.since, period.until);

      if (exact) {
        qualified = period.verified_qualified;
        result.correctionsApplied.push({ since: period.since, until: period.until, mode: 'exact', verifiedQualified: period.verified_qualified });
        return;
      }

      if (containsPeriod) {
        const insideRaw = filterLeadsInRange(leads, period.since, period.until, filterFn)
          .filter(function (l) { return l.qualified === true; }).length;
        qualified = qualified - insideRaw + period.verified_qualified;
        result.correctionsApplied.push({
          since: period.since,
          until: period.until,
          mode: 'contains',
          rawInsidePeriod: insideRaw,
          verifiedQualified: period.verified_qualified,
        });
        return;
      }

      if (overlapsPeriod) {
        result.warning = 'Диапазон частично пересекается с верифицированным историческим периодом ' +
          period.since + ' – ' + period.until + ' (' + period.verified_qualified + ' квалов). ' +
          'Коррекция не применена — показано необработанное значение CRM за выбранный диапазон, ' +
          'чтобы не изобретать пропорциональную оценку.';
      }
    });

    result.qualified = qualified;
    return result;
  }

  return {
    getQualifiedStats: getQualifiedStats,
    validateCorrections: validateCorrections,
    filterLeadsInRange: filterLeadsInRange,
    rangesEqual: rangesEqual,
    fullyContains: fullyContains,
    rangesOverlap: rangesOverlap,
  };
}));
