/* Makes the navbar search box (base.html, [data-nav-search]) act like it
 * lives on /search from any page -- typing the first character "fake"
 * navigates there (a pushState + a fetch-and-swap of the sidebar/results
 * markup, not a real page load), and every later keystroke or filter change
 * keeps the URL's query params in sync via replaceState, so the back button
 * only has to undo the one real transition, not one entry per keystroke.
 */
(function () {
  function onSearchPage() {
    return location.pathname === '/search';
  }

  //: the real navigation (pushState) only happens once the fetch below
  //: resolves, so onSearchPage() still reads false for every keystroke
  //: typed before that -- without debouncing and aborting here, each one
  //: fires its own overlapping /search fetch (a burst of duplicate
  //: requests for a single typed word).
  var debounceTimer = null;
  var pendingController = null;

  //: URLSearchParams.toString() percent-encodes commas (providers=a%2Cb%2Cc)
  //: even though a comma is a valid, unreserved character in a query string
  //: -- undo that just for the address bar, so ?providers=hf,irds stays readable.
  function toQueryString(params) {
    return params.toString().replace(/%2C/g, ',');
  }

  function currentNavParams() {
    var params = new URLSearchParams();
    var q = document.querySelector('[data-nav-search]');
    if (q && q.value.trim()) params.set('q', q.value.trim());
    var regexToggle = document.querySelector('[data-regex-toggle]');
    if (regexToggle && regexToggle.getAttribute('aria-pressed') === 'true') params.set('regex', '1');
    return params;
  }

  function swapToSearchPage(params) {
    // Cancel whatever's still in flight from the previous keystroke -- only
    // the latest typed value should ever hit the network or the DOM.
    if (pendingController) pendingController.abort();
    var controller = new AbortController();
    pendingController = controller;
    var url = '/search' + (params.toString() ? '?' + toQueryString(params) : '');
    fetch(url, {signal: controller.signal})
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var newRow = doc.querySelector('[data-page-row]');
        var curRow = document.querySelector('[data-page-row]');
        if (!newRow || !curRow) throw new Error('no [data-page-row] to swap');
        curRow.innerHTML = newRow.innerHTML;
        document.title = doc.title;
        history.pushState({fakeNav: true}, '', url);
        if (window.initBrowseWidgets) window.initBrowseWidgets();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;  // superseded, not a failure
        // Something about the fetch/parse failed -- fall back to a real
        // navigation rather than leaving the page stuck.
        window.location.href = url;
      });
  }

  document.addEventListener('input', function (e) {
    if (!e.target.matches('[data-nav-search]')) return;
    if (onSearchPage()) return;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      swapToSearchPage(currentNavParams());
    }, 150);
  });

  //: the regex toggle sitting next to the navbar search box (see
  //: [data-regex-toggle] in base.html) needs the same fake-nav treatment as
  //: typing above -- on /search itself, static/browse.js already owns the
  //: toggle (it re-runs its own search and fires browse:filterchange, caught
  //: below); everywhere else, nothing is listening for the click, so it must
  //: kick off the fake nav here, same as the first keystroke does.
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest && e.target.closest('[data-regex-toggle]');
    //: .navbar-search (base.html) scopes this to the navbar's own toggle --
    //: /provider/<prefix>'s local toggle lives in its own [data-browse-list]
    //: card instead, and static/browse.js already owns that one.
    if (!toggle || !toggle.closest('.navbar-search')) return;
    if (onSearchPage()) return;
    var q = document.querySelector('[data-nav-search]');
    if (!q || !q.value.trim()) return;  // matches browse.js's own no-op-on-empty rule
    toggle.setAttribute('aria-pressed', toggle.getAttribute('aria-pressed') !== 'true' ? 'true' : 'false');
    swapToSearchPage(currentNavParams());
  });

  // Dispatched (bubbling) by static/browse.js whenever the search/type/
  // provider selection changes -- keep /search's URL in sync without
  // growing history.
  document.addEventListener('browse:filterchange', function (e) {
    if (!onSearchPage()) return;
    var params = e.detail.params;
    var url = '/search' + (params.toString() ? '?' + toQueryString(params) : '');
    history.replaceState({fakeNav: true}, '', url);
  });

  // The one pushState above is the only history entry this script ever
  // creates, so going back just needs to land on a correct, real page --
  // simplest way to guarantee that is a real reload.
  window.addEventListener('popstate', function () {
    window.location.reload();
  });
})();
