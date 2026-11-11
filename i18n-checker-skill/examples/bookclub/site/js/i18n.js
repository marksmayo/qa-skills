// A tiny loader: picks ?lang=, loads locales/<lang>.json, fills [data-i18n] elements. Supports {name}
// placeholders and ICU plurals through Intl.PluralRules; a missing key shows the key itself.
window.i18n = (function () {
  const lang = new URLSearchParams(location.search).get('lang') || 'en';
  let messages = {};
  function lookup(key) { return key.split('.').reduce((o, k) => (o || {})[k], messages); }
  function format(msg, args) {
    msg = msg.replace(/\{(\w+), plural, ((?:[^{}]|\{[^{}]*\})*)\}/g, function (_, name, body) {
      const n = Number(args[name]); const branches = {};
      body.replace(/(=\d+|\w+) \{([^{}]*)\}/g, function (_, k, v) { branches[k] = v; });
      const cat = new Intl.PluralRules(lang).select(n);
      const chosen = branches['=' + n] || branches[cat] || branches.other;
      return chosen.replace(/#/g, String(n));
    });
    return msg.replace(/\{(\w+)\}/g, function (m, name) { return name in args ? args[name] : m; });
  }
  function t(key, args) { const m = lookup(key); return typeof m === 'string' ? format(m, args || {}) : key; }
  const ready = fetch('locales/' + lang + '.json').then(r => r.json()).then(function (m) {
    messages = m;
    document.documentElement.lang = lang;
    document.documentElement.dir = (lang === 'ar' || lang === 'he') ? 'rtl' : 'ltr';
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      el.textContent = t(el.dataset.i18n, JSON.parse(el.dataset.i18nArgs || '{}'));
    });
  });
  return {t, ready, lang};
})();
