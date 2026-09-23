'use strict';
// Guide des programmes en grille, dans l'onglet Direct : une ligne par chaine
// de la page en cours, le temps de gauche a droite. Les programmes viennent du
// guide XMLTV deja reduit par le serveur (/api/guide/grid).
// Charge avant app.js : ces fonctions n'utilisent ses outils ($, el, api,
// play...) qu'une fois appelees.

const GUIDE_SPAN = 8 * 3600;
const guideState = {from: 0, rows: [], serial: 0};

const guideCompact = () => matchMedia('(max-width: 999px)').matches;
const guidePpm = () => (guideCompact() ? 3.6 : 5);   // pixels par minute
const guideX = seconds => (seconds - guideState.from) / 60 * guidePpm();

// Une demi-heure avant maintenant, arrondie : on voit la fin du programme en cours.
function guideStart() {
  return Math.floor(Date.now() / 1000 / 1800) * 1800 - 1800;
}

function guideDay(epoch) {
  const d = new Date(epoch * 1000), today = new Date();
  if (d.toDateString() === today.toDateString()) return '';
  const tomorrow = new Date(today.getTime() + 86400000);
  if (d.toDateString() === tomorrow.toDateString()) return 'Demain · ';
  return d.toLocaleDateString('fr-FR', {weekday: 'long'}).replace(/^./, ch => ch.toUpperCase()) + ' · ';
}

function guideFrame() {
  const width = GUIDE_SPAN / 60 * guidePpm();
  const scroll = el('div', 'guide-scroll');
  scroll.style.setProperty('--gw', width + 'px');
  const head = el('div', 'guide-head');
  const corner = el('div', 'guide-corner', 'Aujourd’hui');
  const times = el('div', 'guide-times');
  for (let t = guideState.from; t < guideState.from + GUIDE_SPAN; t += 1800) {
    const tick = el('span', 'guide-tick', guideDay(t) && t === guideState.from ? guideDay(t) + clock(t) : clock(t));
    tick.style.left = guideX(t) + 'px';
    times.append(tick);
  }
  head.append(corner, times);
  const body = el('div', 'guide-body');
  body.append(el('span', 'guide-now'));
  scroll.append(head, body);
  // La suite des chaines se charge en approchant du bas, comme la grille.
  scroll.addEventListener('scroll', () => {
    const more = $('#load-more');
    if (!more.hidden && !more.disabled && scroll.scrollTop + scroll.clientHeight > scroll.scrollHeight - 400) more.click();
  }, {passive: true});
  return scroll;
}

function guideRow(c, number) {
  const node = el('div', 'guide-row');
  node.dataset.channelKey = channelKey(c);
  const ch = el('button', 'guide-ch');
  ch.type = 'button';
  ch.setAttribute('aria-label', 'Regarder ' + (c.label || ''));
  const logo = paint(el('span', 'guide-logo'), c.label);
  logo.append(el('span', 'brand', c.label || c.canonical || ''));
  const img = imageFor(c.icon, () => logo.classList.add('has-img'));
  if (img) logo.append(img);
  const text = el('span', 'guide-ch-text');
  text.append(el('span', 'guide-num', String(number)), el('span', 'guide-name', c.label || ''));
  ch.append(logo, text);
  ch.onclick = () => play(c).catch(failure);
  const line = el('div', 'guide-line');
  line.append(el('span', 'guide-empty', c.epg_id ? 'Chargement du programme…' : 'Aucun programme annoncé'));
  node.append(ch, line);
  return {node, line, channel: c};
}

function guideProgram(c, [start, stop, title], now) {
  const left = Math.max(0, guideX(start));
  const right = Math.min(GUIDE_SPAN / 60 * guidePpm(), guideX(stop));
  if (right - left < 3) return null;
  const airing = start <= now && now < stop;
  const b = el('button', 'guide-prog' + (airing ? ' now' : stop <= now ? ' past' : ''));
  b.type = 'button';
  b.style.left = left + 'px';
  b.style.width = (right - left) + 'px';
  const hours = clock(start) + ' – ' + clock(stop);
  const text = el('span', 'guide-prog-text');
  text.append(el('strong', '', title || 'Programme'), el('span', '', hours));
  b.append(text);
  b.setAttribute('aria-label', (title || 'Programme') + ', ' + guideDay(start) + hours + ' sur ' + (c.label || ''));
  b.onclick = e => {
    // Le programme en cours se regarde tout de suite ; les autres montrent
    // d'abord leur horaire, avec l'acces a la chaine.
    if (airing) { play(c).catch(failure); return; }
    e.stopPropagation();
    openCardMenu(b, [{label: '▶ Regarder ' + (c.label || 'la chaîne') + ' maintenant', run: () => play(c)}],
      {title: title || 'Programme', sub: guideDay(start) + hours + ' · ' + (c.label || '')});
  };
  return b;
}

function placeNowLine() {
  const line = document.querySelector('#guide .guide-now');
  if (!line) return;
  const x = guideX(Date.now() / 1000);
  line.hidden = x < 0 || x > GUIDE_SPAN / 60 * guidePpm();
  line.style.left = 'calc(var(--gch) + ' + x + 'px)';
}

// Sur telephone, la page ne defile pas avec la grille : elle prend la hauteur
// qui reste jusqu'a la barre d'onglets, pour que l'heure reste en haut.
function fitGuide() {
  const box = $('#guide');
  if (!box || box.hidden) return;
  if (!guideCompact()) { box.style.height = ''; return; }
  const bar = $('.topbar .tabs');
  const bottom = bar ? bar.getBoundingClientRect().height : 72;
  box.style.height = Math.max(300, window.innerHeight - box.getBoundingClientRect().top - window.scrollY - bottom - 8) + 'px';
}
// Peut arriver avant le chargement d'app.js : pas de $ ici.
window.addEventListener('resize', () => {
  const box = document.getElementById('guide');
  if (box && !box.hidden) fitGuide();
});

async function fillGuide(rows, serial) {
  const ids = [...new Set(rows.map(r => r.channel.epg_id).filter(Boolean))];
  if (!ids.length) return;
  const from = guideState.from;
  const q = new URLSearchParams({ids: ids.join(','), from: String(from), to: String(from + GUIDE_SPAN)});
  const data = await api('/guide/grid?' + q, {skipAuthRedirect: true}).catch(() => null);
  if (serial !== guideState.serial) return;
  if (!data || !data.ready) {
    // Premier appel apres un demarrage : le serveur lit le guide en fond.
    setTimeout(() => fillGuide(rows, serial), 3000);
    return;
  }
  const now = Date.now() / 1000;
  rows.forEach(({line, channel}) => {
    const list = channel.epg_id && data.programs[channel.epg_id];
    const blocks = (list || []).map(p => guideProgram(channel, p, now)).filter(Boolean);
    if (blocks.length) line.replaceChildren(...blocks);
    else line.replaceChildren(el('span', 'guide-empty', 'Aucun programme annoncé'));
  });
}

// items : une page de chaines ; more : a la suite des precedentes.
function renderGuide(items, more, first) {
  const box = $('#guide');
  if (!more) {
    guideState.serial++;
    guideState.from = guideStart();
    guideState.rows = [];
    box.replaceChildren(guideFrame());
  }
  const body = box.querySelector('.guide-body');
  const rows = items.map((c, i) => guideRow(c, first + i + 1));
  rows.forEach(r => body.append(r.node));
  guideState.rows.push(...rows);
  placeNowLine();
  if (!more) {
    fitGuide();
    // Maintenant a gauche, avec un quart d'heure de marge.
    box.querySelector('.guide-scroll').scrollLeft = Math.max(0, guideX(Date.now() / 1000 - 900));
    if (!items.length) body.append(el('p', 'empty', 'Aucune chaîne pour ces critères.'));
  }
  fillGuide(rows, guideState.serial);
}

setInterval(placeNowLine, 60000);
