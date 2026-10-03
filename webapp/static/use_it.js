/* The "Use It" card on a benchmark page: when a facet has alternative tables,
 * the card carries a dropdown per such facet (macro use_it_section) and this
 * rebuilds the Python snippet from the selections -- the same snippet
 * app._python_snippet renders server-side for the all-defaults case. */
(function () {
  var holder = document.querySelector('[data-use-it-config]');
  if (!holder) return;
  var config = JSON.parse(holder.getAttribute('data-use-it-config'));
  var code = document.querySelector('#use-it pre code');
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
