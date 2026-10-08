/* Sample Rows (node_table.html): a long cell is cut off at a fixed length with
 * a "[+N chars]" link; clicking it shows the rest in place.
 */
document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('[data-sample-expand]');
  if (!link) return;
  e.preventDefault();
  var cell = link.closest('.sample-cell');
  var rest = cell.querySelector('[data-sample-rest]');
  var ellipsis = cell.querySelector('[data-sample-ellipsis]');
  if (rest) rest.hidden = false;
  if (ellipsis) ellipsis.remove();
  link.remove();
});
