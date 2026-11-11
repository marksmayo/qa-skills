// Trackly analytics (stub). Sends a page view with the page URL and a client id cookie.
(function () {
  var m = document.cookie.match(/(?:^|; )_tk=([^;]+)/);
  var cid = m ? m[1] : 'tk.' + Math.random().toString(36).slice(2, 12);
  document.cookie = '_tk=' + cid + '; path=/; max-age=63072000';
  var img = new Image();
  img.src = 'http://px.trackly.test:8153/collect?ev=page_view&cid=' + encodeURIComponent(cid) +
    '&dl=' + encodeURIComponent(location.href) + '&dt=' + encodeURIComponent(document.title);
  window.trackly = function () {};
})();
