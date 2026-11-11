const user = {name: 'Robin'};
const price = 12.5;
const meeting = new Date(2026, 11, 3);
i18n.ready.then(function () {
  // The greeting is built from fragments, so a translator never sees the whole sentence.
  document.getElementById('greeting') && (document.getElementById('greeting').textContent = i18n.t('greeting') + ', ' + user.name + '!');
  // The price is formatted by hand: always a dollar sign and a full stop, whatever the locale.
  const p = document.getElementById('price');
  if (p) p.textContent = i18n.t('membership.price', {price: '$' + price.toFixed(2)});
  // The date is formatted by hand as month/day/year.
  const d = document.getElementById('next');
  if (d) d.textContent = i18n.t('events.next', {date: (meeting.getMonth() + 1) + '/' + meeting.getDate() + '/' + meeting.getFullYear()});
});
