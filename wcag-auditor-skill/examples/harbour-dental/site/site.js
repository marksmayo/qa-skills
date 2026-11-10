// Shared behaviour: menu toggle, cookie banner, carousel, tooltip.
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.querySelector('.menu-toggle');
  if (toggle) toggle.addEventListener('click', () => document.querySelector('nav').classList.toggle('open'));

  const cookie = document.querySelector('.cookie');
  if (cookie && localStorage.getItem('cookie-ok')) cookie.remove();
  const accept = document.querySelector('#cookie-accept');
  if (accept) accept.addEventListener('click', () => { localStorage.setItem('cookie-ok', '1'); cookie.remove(); });

  const slides = [...document.querySelectorAll('.slide')];
  if (slides.length) {
    let i = 0;
    const show = n => { slides[i].classList.remove('current'); i = (n + slides.length) % slides.length; slides[i].classList.add('current'); };
    setInterval(() => show(i + 1), 3000);
    document.querySelector('.carousel .prev').addEventListener('click', () => show(i - 1));
    document.querySelector('.carousel .next').addEventListener('click', () => show(i + 1));
  }

  for (const help of document.querySelectorAll('.help')) {
    const tip = help.nextElementSibling;
    help.addEventListener('mouseenter', () => { tip.style.display = 'block'; });
    help.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  }
});
