// parcel tracker (sample). Hash routes: #orders, #track/<id>, #address, #help.
const app = document.getElementById('app');

function spinner() { app.innerHTML = '<div class="spinner" role="progressbar" aria-busy="true" aria-label="Loading"></div>'; }

// Each route renders inside a boundary that shows a fallback when rendering throws.
function boundary(render) {
  return render().catch(function () {
    app.innerHTML = '<h2>Something went wrong</h2><p>Please reload the page.</p>';
  });
}

async function orders() {
  spinner();
  let list;
  try {
    list = await (await fetch('/api/orders')).json();
    if (!Array.isArray(list)) throw new Error('bad orders');
  } catch (e) {
    list = [];                                             // planted: a failure is shown as an empty list
  }
  const recs = await (await fetch('/api/recommendations')).json();
  const items = recs.items.map(i => '<li>' + i.title + '</li>').join('');   // planted: no check, so a failure throws
  app.innerHTML = (list.length ? '<h2>Your parcels</h2><ul>' + list.map(o =>
      '<li>' + o.id + ' from ' + o.from + ' <a class="track" href="#track/' + o.id + '">Track</a></li>').join('') + '</ul>'
    : '<h2>You have no parcels yet</h2>') + '<div class="recs"><h3>Good to know</h3><ul>' + items + '</ul></div>';
}

async function track(id) {
  spinner();
  const s = await (await fetch('/api/status/' + id)).json();     // planted: no timeout, so a hung request spins forever
  app.innerHTML = '<h2 class="status">' + s.id + ': ' + s.status + '</h2><p>Last scan: ' + (s.last_scan || 'none yet') + '</p>' +
    '<button id="received" type="button">Mark as received</button><p id="received-msg" role="status"></p>';
  document.getElementById('received').addEventListener('click', async function () {
    const msg = document.getElementById('received-msg');
    msg.textContent = 'Marked as received';                      // optimistic, and rolled back on failure
    msg.className = 'received-state';
    try {
      const r = await fetch('/api/received/' + id, {method: 'POST'});
      if (!r.ok) throw new Error('status ' + r.status);
    } catch (e) {
      msg.className = 'error';
      msg.setAttribute('role', 'alert');
      msg.innerHTML = 'Couldn\'t update the parcel. <button id="retry-received" type="button">Try again</button>';
      document.getElementById('retry-received').addEventListener('click', () => document.getElementById('received').click());
    }
  });
}

function address() {
  app.innerHTML = '<h2>Delivery address</h2><form id="addr"><label for="street">Street</label> <input id="street" name="street">' +
    ' <button id="save" type="submit">Save</button></form><p id="saved-msg" role="status"></p>';
  const form = document.getElementById('addr');
  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    try {
      const r = await fetch('/api/address', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({street: document.getElementById('street').value})});
      if (!r.ok) throw new Error('status ' + r.status);
      document.getElementById('saved-msg').textContent = 'Address saved';
    } catch (e) {
      form.reset();                                              // planted: the typed address is lost, with no message
    }
  });
}

function help() {
  return new Promise(function (resolve) {
    const s = document.createElement('script');
    s.src = 'chunk-help.js';                                     // lazy chunk
    s.onload = function () { window.renderHelp(app); resolve(); };
    s.onerror = function () { app.innerHTML = ''; resolve(); };  // planted: a failed chunk leaves the screen blank
    document.body.appendChild(s);
  });
}

function route() {
  const h = location.hash || '#orders';
  if (h.startsWith('#track/')) return boundary(() => track(h.slice(7)));
  if (h === '#address') return boundary(async () => address());
  if (h === '#help') return boundary(help);
  return boundary(orders);
}

// Service notices: polled every 30 s; on failure retried every 200 ms (planted: ignores Retry-After).
async function notices() {
  try {
    const r = await fetch('/api/notices');
    if (!r.ok) throw new Error('status ' + r.status);
    document.getElementById('notice').textContent = (await r.json()).notice;
    setTimeout(notices, 30000);
  } catch (e) {
    setTimeout(notices, 200);
  }
}

window.addEventListener('hashchange', route);
route();
notices();
