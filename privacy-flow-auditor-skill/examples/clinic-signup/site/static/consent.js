// Consent banner. Advertising and session replay load only after "Accept all".
(function () {
  function loadScript(src, onload) {
    var s = document.createElement('script');
    s.src = src;
    if (onload) { s.onload = onload; }
    document.head.appendChild(s);
  }
  function applyConsent(state) {
    if (state === 'granted') {
      // cache key for the ad script build (not an identifier)
      loadScript('http://cdn.adnetic.test:8153/a.js?v=5d41402abc4b2a76b9719d911017c5925d41402abc4b2a76b9719d911017c592');
      loadScript('http://cdn.replayr.test:8153/r.js', function () {
        replayr.init({maskAllInputs: false});
      });
    }
  }
  window.siteScrub = function (event) {
    if (event.user) { delete event.user.email; }
    return event;
  };
  document.addEventListener('DOMContentLoaded', function () {
    var banner = document.getElementById('consent');
    var state = localStorage.getItem('consent');
    if (!state) {
      banner.hidden = false;
    } else {
      applyConsent(state);
    }
    document.getElementById('consent-accept').addEventListener('click', function () {
      localStorage.setItem('consent', 'granted');
      banner.hidden = true;
      applyConsent('granted');
    });
    document.getElementById('consent-reject').addEventListener('click', function () {
      localStorage.setItem('consent', 'denied');
      banner.hidden = true;
    });
  });
})();
