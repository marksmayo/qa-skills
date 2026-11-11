// Adnetic advertising (stub). Conversion events go to the pixel endpoint.
window.adnetic = {
  track: function (event, data) {
    var q = Object.keys(data).map(function (k) { return k + '=' + encodeURIComponent(data[k]); }).join('&');
    new Image().src = 'http://px.adnetic.test:8153/ev?e=' + encodeURIComponent(event) + '&' + q;
  }
};
