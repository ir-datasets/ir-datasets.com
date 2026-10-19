/* The "Use It" card on a benchmark page: when a facet has alternative tables,
 * the card carries a dropdown per such facet (macro use_it_section) and this
 * rebuilds the Python snippet from the selections -- the same snippet
 * app._python_snippet renders server-side for the all-defaults case. */
(function () {
  var holder = document.querySelector('[data-use-it-config]');
  if (!holder) return;
  var config = JSON.parse(holder.getAttribute('data-use-it-config'));
  var code = document.querySelector('#use-it pre code.language-python');
  var selects = holder.querySelectorAll('select[data-use-it-facet]');

  function build() {
    var chosen = {}, args = [];
    selects.forEach(function (sel) { chosen[sel.getAttribute('data-use-it-facet')] = sel.value; });
    config.facets.forEach(function (f) {
      if (chosen[f.entity] && chosen[f.entity] !== f.options[0].name)
        args.push(f.entity + '=' + JSON.stringify(chosen[f.entity]).replace(/"/g, "'"));
    });
    var lines = ['import ir_datasets.v2',
      'ds = ir_datasets.v2.load(' + ["'" + config.name + "'"].concat(args).join(', ') + ')'];
    config.facets.forEach(function (f) {
      var name = chosen[f.entity] || f.options[0].name;
      var opt = f.options.filter(function (o) { return o.name === name; })[0];
      lines.push('for ' + f.singular + ' in ds.' + f.entity + ':');
      lines.push('    ...' + (opt.fields.length ? '  # ' + f.singular + '.[' + opt.fields.join(', ') + ']' : ''));
    });
    return lines.join('\n');
  }

  selects.forEach(function (sel) {
    sel.addEventListener('change', function () {
      code.removeAttribute('data-highlighted');
      code.textContent = build();
      if (window.hljs) hljs.highlightElement(code);
    });
  });
})();

/* The pill row above the example: show the chosen variant's pane. The choice (and the
 * example picked inside a variant) is remembered across pages -- a variant this page
 * doesn't have simply falls back to the first. localStorage may be unavailable
 * (private windows, blocked site data): then nothing is remembered. */
(function () {
  function recall(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function remember(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* not remembered */ } }

  function showVariant(card, bar, id) {
    var pills = bar.querySelectorAll('[data-variant]');
    if (!Array.prototype.some.call(pills, function (p) { return p.getAttribute('data-variant') === id; })) return false;
    pills.forEach(function (p) {
      var on = p.getAttribute('data-variant') === id;
      p.classList.toggle('active', on);
      p.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    card.querySelectorAll('[data-variant-pane]').forEach(function (pane) {
      pane.hidden = pane.getAttribute('data-variant-pane') !== id;
    });
    return true;
  }

  function showExample(pane, id) {
    var examples = pane.querySelectorAll('[data-example]');
    if (!Array.prototype.some.call(examples, function (pre) { return pre.getAttribute('data-example') === id; })) return false;
    examples.forEach(function (pre) { pre.hidden = pre.getAttribute('data-example') !== id; });
    var select = pane.querySelector('[data-example-select]');
    if (select) select.value = id;
    return true;
  }

  document.querySelectorAll('[data-variant-pills]').forEach(function (bar) {
    var card = bar.closest('.card');
    var saved = recall('irds.variant');
    if (saved) showVariant(card, bar, saved);
    bar.addEventListener('click', function (e) {
      var pill = e.target.closest('[data-variant]');
      // only an actual switch is a choice: re-clicking the shown pill (or a page's only
      // pill) must not overwrite what the user last picked elsewhere
      if (!pill || pill.classList.contains('active')) return;
      showVariant(card, bar, pill.getAttribute('data-variant'));
      remember('irds.variant', pill.getAttribute('data-variant'));
    });
  });

  /* A variant with several examples: its <select> shows the chosen one. */
  document.querySelectorAll('[data-example-select]').forEach(function (select) {
    var pane = select.closest('[data-variant-pane]');
    var key = 'irds.example.' + pane.getAttribute('data-variant-pane');
    var saved = recall(key);
    if (saved) showExample(pane, saved);
    select.addEventListener('change', function () {
      showExample(pane, select.value);
      remember(key, select.value);
    });
  });
})();
