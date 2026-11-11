// Replayr session replay (stub). Records input events once the site calls init(); masking is the site's choice.
window.replayr = {
  init: function (cfg) {
    var buffer = [];
    document.addEventListener('change', function (e) {
      var t = e.target;
      if (!t || !('value' in t)) { return; }
      buffer.push({type: 'input', id: t.id, value: cfg.maskAllInputs === false ? t.value : '***'});
    }, true);
    function flush() {
      if (!buffer.length) { return; }
      var body = JSON.stringify({events: buffer.splice(0)});
      fetch('http://rec.replayr.test:8153/rec', {method: 'POST', body: body, headers: {'content-type': 'text/plain'}, keepalive: true});
    }
    setInterval(flush, 500);
    window.addEventListener('pagehide', flush);
  }
};
