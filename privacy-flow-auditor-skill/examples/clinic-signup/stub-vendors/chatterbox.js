// Chatterbox support chat (stub). identify() sends the visitor's details to the chat service.
window.chatterbox = {
  identify: function (user) {
    new Image().src = 'http://api.chatterbox.test:8153/identify?' +
      Object.keys(user).map(function (k) { return k + '=' + encodeURIComponent(user[k]); }).join('&');
  }
};
new Image().src = 'http://api.chatterbox.test:8153/hello?page=' + encodeURIComponent(location.pathname);
