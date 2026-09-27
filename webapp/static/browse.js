/* Live-search + infinite-scroll list, backed by /api/browse. Used by
 * /search (full catalog, q + type + provider filters) and
 * /provider/<prefix> (that provider's own nodes, q filter only) -- one
 * implementation, since both are just "search this slice of the catalog,
 * load more as you scroll", shown by default with no query needed. Needed
 * because the catalog is large (CLIRMatrix alone contributes ~620k names):
 * a plain GET-form-per-keystroke page reload, or an unpaginated table,
 * doesn't hold up at that size.
 *
 * Dispatches a `browse:filterchange` CustomEvent on `root` (bubbling)
 * whenever the query/type/provider selection changes (before the new page
 * is loaded), so static/nav.js can keep /search's URL in sync without
 * duplicating the debounce/reset logic here.
 */
(function () {
  function initBrowseList(root) {
    var tbody = root.querySelector('[data-browse-rows]');
    var status = root.querySelector('[data-browse-status]');
    var sentinel = root.querySelector('[data-browse-sentinel]');
    //: the search box is looked up inside `root` first -- /provider/<prefix>
    //: has its own local filter input there -- falling back to the
    //: document-wide navbar search box used by /search (which lives in the
    //: navbar, outside the results container entirely; see base.html).
    //: type/provider controls always live in the sidebar, outside `root`.
    var searchInput = root.querySelector('[data-browse-search]') || document.querySelector('[data-browse-search]');
    var typeSelect = document.querySelector('[data-browse-type]');
    var providerBoxes = document.querySelectorAll('[data-browse-provider]');
    var namePrefix = root.getAttribute('data-name-prefix') || '';
    var limit = parseInt(root.getAttribute('data-limit') || '200', 10);

    var offset = 0, total = 0, loading = false, done = false;
    //: bumped on every reset(); a response is only applied if it's still the
    //: current generation -- an AbortController alone isn't enough here,
    //: since a request that's already past `fetch()` (headers received,
    //: body streaming) can still resolve its `.then()` after abort() is
    //: called, and an out-of-order-resolving *older* request must not
    //: clobber a newer one's (possibly already-rendered) results.
    var generation = 0;
    var rowTemplate = root.querySelector('template[data-browse-row]');

    function reset() {
      generation++;
      offset = 0;
      total = 0;
      done = false;
      loading = false;
      tbody.innerHTML = '';
    }

    function renderRow(row) {
      var frag = rowTemplate.content.cloneNode(true);
      frag.querySelector('[data-f-link]').href = row.url;
      frag.querySelector('[data-f-name]').textContent = row.name;
      var typeEl = frag.querySelector('[data-f-type]');
      typeEl.textContent = row.type_label;
      typeEl.classList.add(row.badge_class);
      tbody.appendChild(frag);
    }

    function selectedProviders() {
      return Array.prototype.filter.call(providerBoxes, function (b) { return b.checked; })
        .map(function (b) { return b.value; });
    }

    function buildParams() {
      var params = new URLSearchParams();
      if (searchInput && searchInput.value.trim()) params.set('q', searchInput.value.trim());
      if (typeSelect && typeSelect.value) params.set('type', typeSelect.value);
      if (namePrefix) params.set('prefix', namePrefix);
      if (providerBoxes.length) params.set('providers', selectedProviders().join(','));
      return params;
    }

    function updateStatus() {
      if (total === 0 && !loading) {
        status.textContent = 'No nodes match.';
      } else {
        status.textContent = total + ' node' + (total === 1 ? '' : 's') +
          (loading ? ' – loading…' : '');
      }
    }

    function loadMore() {
      if (loading || done) return;
      loading = true;
      updateStatus();
      var myGeneration = generation;
      var params = buildParams();
      params.set('offset', offset);
      params.set('limit', limit);
      fetch('/api/browse?' + params.toString())
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (myGeneration !== generation) return;  // a newer search superseded this one
          data.rows.forEach(renderRow);
          offset += data.rows.length;
          total = data.total;
          done = offset >= total;
          loading = false;
          updateStatus();
        })
        .catch(function () {
          if (myGeneration !== generation) return;
          loading = false;
          status.textContent = 'Failed to load results.';
        });
    }

    var debounceTimer = null;
    function onFilterChange() {
      //: bubbles so static/nav.js (attached at the document level, since it
      //: has no reference to any particular widget's root) can hear it too,
      //: to keep /search's URL query params in sync as the user types.
      root.dispatchEvent(new CustomEvent('browse:filterchange', {detail: {params: buildParams()}, bubbles: true}));
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        reset();
        loadMore();
      }, 150);
    }

    if (searchInput) searchInput.addEventListener('input', onFilterChange);
    if (typeSelect) typeSelect.addEventListener('change', onFilterChange);
    providerBoxes.forEach(function (box) { box.addEventListener('change', onFilterChange); });

    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) loadMore();
    }, {rootMargin: '400px'}).observe(sentinel);

    loadMore();
  }

  function initAll() {
    document.querySelectorAll('[data-browse-list]').forEach(initBrowseList);
  }

  //: called by static/nav.js after it swaps a freshly-fetched /search page's
  //: markup into the DOM in place of a full reload -- that markup includes a
  //: `[data-browse-list]` this script never saw at initial page load.
  window.initBrowseWidgets = initAll;
  initAll();
})();
