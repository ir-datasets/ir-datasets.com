/* The Verification card on a node page (macro verification_card): a pill per kind of attestation, showing that
 * kind's pane. Like the Use It pills (use_it.js), the choice is remembered across pages -- a kind this node does
 * not have just falls back to the card's own default. localStorage may be unavailable: then nothing is remembered. */
(function () {
  var KEY = 'irds.attest';
  function recall() { try { return window.localStorage.getItem(KEY); } catch (e) { return null; } }
  function remember(value) { try { window.localStorage.setItem(KEY, value); } catch (e) { /* not remembered */ } }

  function show(card, bar, kind) {
    var pills = bar.querySelectorAll('[data-verif]');
    if (!Array.prototype.some.call(pills, function (p) { return p.getAttribute('data-verif') === kind; })) return false;
    pills.forEach(function (p) {
      var on = p.getAttribute('data-verif') === kind;
      p.classList.toggle('active', on);
      p.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    card.querySelectorAll('[data-verif-pane]').forEach(function (pane) {
      pane.hidden = pane.getAttribute('data-verif-pane') !== kind;
    });
    return true;
  }

  document.querySelectorAll('[data-verif-pills]').forEach(function (bar) {
    var card = bar.closest('.card');
    var saved = recall();
    // a remembered kind only wins on a page where it has something to show or nothing else does
    if (saved) {
      var pill = bar.querySelector('[data-verif="' + saved + '"]');
      var count = pill && parseInt(pill.querySelector('.pill-count').textContent, 10);
      var hasAny = Array.prototype.some.call(bar.querySelectorAll('.pill-count'), function (c) { return parseInt(c.textContent, 10) > 0; });
      if (pill && (count > 0 || !hasAny)) show(card, bar, saved);
    }
    bar.addEventListener('click', function (e) {
      var pill = e.target.closest('[data-verif]');
      if (!pill || pill.classList.contains('active')) return;
      show(card, bar, pill.getAttribute('data-verif'));
      remember(pill.getAttribute('data-verif'));
    });
  });
})();
