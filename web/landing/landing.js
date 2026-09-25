'use strict';
// Page de presentation : apparitions au defilement, compteur de match,
// jauge du mode Budget et calculateur mensuel. Aucune dependance.

document.documentElement.classList.add('js');
const $ = s => document.querySelector(s);
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
const number = (n, digits = 2) => n.toLocaleString('fr-FR', {minimumFractionDigits: digits, maximumFractionDigits: digits});

// Barre du haut : transparente sur l'ouverture, opaque des qu'on descend.
const nav = $('.nav');
const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 24);
window.addEventListener('scroll', onScroll, {passive: true});
onScroll();

// Apparitions : les elements d'un meme bloc arrivent les uns apres les autres.
const reveals = [...document.querySelectorAll('.reveal')];
reveals.forEach(node => {
  const siblings = [...node.parentElement.children].filter(n => n.classList.contains('reveal'));
  node.style.setProperty('--d', Math.min(siblings.indexOf(node), 5) * 0.08 + 's');
});
if (still || !('IntersectionObserver' in window)) {
  reveals.forEach(n => n.classList.add('in'));
} else {
  const seen = new IntersectionObserver(entries => entries.forEach(e => {
    if (!e.isIntersecting) return;
    e.target.classList.add('in');
    seen.unobserve(e.target);
  }), {rootMargin: '0px 0px -8% 0px', threshold: 0.12});
  reveals.forEach(n => seen.observe(n));
}

// Compteur de match : 90 minutes rejouees en 12 secondes. Le flux d'origine
// a 3,1 Go/h contre Streamly en Equilibre a 0,42 Go/h.
const RAW = 3.1, LITE = 0.42, MATCH = 90, LOOP = 12000, HOLD = 2600;
const full = RAW * MATCH / 60;
const minute = $('#match-minute'), quality = $('#quality-now');
const fillRaw = $('#fill-raw'), fillLite = $('#fill-lite'), valueRaw = $('#value-raw'), valueLite = $('#value-lite');
// La qualite suit la connexion : un passage a vide fait descendre, puis remonter.
const qualityAt = m => (m > 38 && m < 47 ? '360p' : m > 70 && m < 74 ? '240p' : '480p');

function showMinute(m) {
  const raw = RAW * m / 60, lite = LITE * m / 60;
  minute.textContent = String(Math.floor(m));
  fillRaw.style.width = (raw / full * 100) + '%';
  fillLite.style.width = (lite / full * 100) + '%';
  valueRaw.textContent = number(raw) + ' Go';
  valueLite.textContent = number(lite) + ' Go';
  quality.textContent = qualityAt(m);
}

if (still) {
  showMinute(MATCH);
} else {
  let start = null, visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe($('.screen'));
  const tick = now => {
    if (start === null) start = now;
    const elapsed = (now - start) % (LOOP + HOLD);
    if (visible) showMinute(Math.min(MATCH, elapsed / LOOP * MATCH));
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// Jauge du mode Budget : se remplit quand elle arrive a l'ecran.
const budget = $('.budget-demo');
if (budget) {
  if (still || !('IntersectionObserver' in window)) budget.classList.add('on');
  else new IntersectionObserver(([e], obs) => { if (e.isIntersecting) { budget.classList.add('on'); obs.disconnect(); } }, {threshold: 0.5}).observe(budget);
}

// Calculateur : heures par semaine x 4,3 semaines, source a 3 Go/h.
const hours = $('#hours');
function calc() {
  const h = Number(hours.value);
  const rate = Number(document.querySelector('input[name="mode"]:checked').value);
  const month = h * 4.3;
  const raw = month * 3, lite = month * rate;
  $('#hours-out').textContent = h + ' h';
  $('#calc-raw').textContent = Math.round(raw) + ' Go';
  $('#calc-lite').textContent = number(lite, lite < 10 ? 1 : 0) + ' Go';
  $('#calc-saved').textContent = Math.round(raw - lite) + ' Go';
  hours.style.setProperty('--fill', ((h - hours.min) / (hours.max - hours.min) * 100) + '%');
}
hours.addEventListener('input', calc);
document.querySelectorAll('input[name="mode"]').forEach(r => r.addEventListener('change', calc));
calc();
