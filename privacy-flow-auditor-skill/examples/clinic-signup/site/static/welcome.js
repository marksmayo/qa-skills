// Greet the new patient in the support chat.
(function () {
  var email = new URLSearchParams(location.search).get('email');
  if (email && window.chatterbox) {
    chatterbox.identify({email: email});
  }
})();
