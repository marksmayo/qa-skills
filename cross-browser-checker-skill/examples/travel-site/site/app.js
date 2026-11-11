// Recommendations: the brand list comes from User-Agent Client Hints (Chromium only), with no feature check,
// so this throws in Firefox and WebKit before the button is wired up.
const brands = navigator.userAgentData.brands.map(b => b.brand).join(', ');
document.getElementById('more').addEventListener('click', function () {
  const ul = document.createElement('ul');
  ul.id = 'recs-list';
  ['Hobart, 3 nights', 'Broome, 5 nights', 'Queenstown, 4 nights'].forEach(function (t) {
    const li = document.createElement('li'); li.textContent = t; ul.appendChild(li);
  });
  document.getElementById('recs').appendChild(ul);
  document.getElementById('recs').dataset.brands = brands;
});
