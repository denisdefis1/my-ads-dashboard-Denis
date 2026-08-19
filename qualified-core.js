// Единственный источник нормализованного подсчёта Qualified для всего
// дашборда: главный KPI, "Всё время", месяц, custom range, weekly
// comparison, campaigns/adsets/ads/creatives, RU/EN, страны, funnel.
// Не дублировать эту логику в index.html — все места должны звать эти
// функции. Каждая функция принимает данные явными аргументами (без обращения
// к глобальным CRM/campaignLangById), поэтому пригодна для юнит-тестов без
// DOM и без сети — см. tests/qualified-core.test.js.
(function (root) {
  function emptyStats() {
    return { total: 0, qualified: 0, notQualified: 0, pending: 0 };
  }

  // leads: CRM.leads (массив объектов с created_at 'YYYY-MM-DD HH:MM:SS'|null,
  // qualified true|false|null, campaign_id).
  // opts.flow: 'all'|'RU'|'EN'|... — при не-'all' фильтрует по языку кампании
  // через opts.campaignLangById[campaign_id].
  function crmLeadsInRange(leads, since, until, opts) {
    if (!leads) return emptyStats();
    opts = opts || {};
    const flow = opts.flow;
    const campaignLangById = opts.campaignLangById || {};
    const rows = leads.filter(l => {
      if (!l.created_at) return false;
      const d = l.created_at.slice(0, 10);
      if (d < since || d > until) return false;
      if (flow && flow !== 'all') return campaignLangById[l.campaign_id] === flow;
      return true;
    });
    return {
      total: rows.length,
      qualified: rows.filter(l => l.qualified === true).length,
      notQualified: rows.filter(l => l.qualified === false).length,
      pending: rows.filter(l => l.qualified === null).length,
    };
  }

  // corrections: qual_corrections.json.corrections — каждая применяется,
  // только если её период [since, until] целиком укладывается в запрошенный
  // диапазон. Так "Всё время" получает те же поправки, что и точный выбор
  // периода корректировки, без отдельного хардкода под конкретный preset.
  // mode 'replace' заменяет сырой CRM-подсчёт за период корректировки на
  // verified_qualified (а не складывает); mode 'add' прибавляет amount.
  // Каждая корректировка применяется ровно один раз.
  function applyQualCorrections(leads, corrections, since, until, stats) {
    let qualified = stats.qualified;
    (corrections || []).forEach(c => {
      if (!c.since || !c.until) return;
      if (c.since < since || c.until > until) return;
      if (c.mode === 'replace') {
        const rawInPeriod = crmLeadsInRange(leads, c.since, c.until, { flow: 'all' }).qualified;
        qualified = qualified - rawInPeriod + c.verified_qualified;
      } else if (c.mode === 'add') {
        qualified += c.amount;
      }
    });
    return Object.assign({}, stats, { qualified });
  }

  function crmQualifiedByEntity(leads, since, until, idField) {
    const map = {};
    if (!leads) return map;
    leads.forEach(l => {
      if (!l.created_at || !l[idField]) return;
      const d = l.created_at.slice(0, 10);
      if (d < since || d > until) return;
      if (l.qualified !== true) return;
      map[l[idField]] = (map[l[idField]] || 0) + 1;
    });
    return map;
  }

  const QualifiedCore = { crmLeadsInRange, applyQualCorrections, crmQualifiedByEntity };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = QualifiedCore;
  } else {
    root.QualifiedCore = QualifiedCore;
  }
})(typeof window !== 'undefined' ? window : globalThis);
