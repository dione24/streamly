'use strict';
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const store = {
  get(k, fallback = '') { try { return localStorage.getItem('streamly_' + k) || fallback; } catch (_) { return fallback; } },
  set(k, v) { try { localStorage.setItem('streamly_' + k, v); } catch (_) {} },
  remove(k) { try { localStorage.removeItem('streamly_' + k); } catch (_) {} },
  json(k, fallback) { try { return JSON.parse(this.get(k)) || fallback; } catch (_) { return fallback; } },
};
// Propre a l'onglet : survit a l'actualisation, pas a la fermeture. C'est ce
// qui distingue « je rechargeais la page » de « je reviens plus tard ».
const tabStore = {
  get(k) { try { return JSON.parse(sessionStorage.getItem('streamly_' + k)); } catch (_) { return null; } },
  set(k, v) { try { sessionStorage.setItem('streamly_' + k, JSON.stringify(v)); } catch (_) {} },
  remove(k) { try { sessionStorage.removeItem('streamly_' + k); } catch (_) {} },
};

const state = {
  mode: 'home', lang: store.get('lang'), provider: store.get('provider'), category: '', query: '', favorites: [],
  page: 0, pageSize: 48, request: 0, controller: null, hls: null, ticket: null, current: null,
  generation: null, retries: 0, statusMisses: 0, recoveryTimer: null, playback: 0, stalls: 0, started: 0,
  frames: false, movie: null, job: null, role: 'viewer', configTimer: null, jobTimer: null,
  configOpen: false, configReturnMode: 'live', configSnapshot: null,
  channelList: [], zapTimer: null,
  selectedChannelIndex: -1,
  // Historique du compte (serveur) ; watch : film ou episode en lecture.
  history: {items: [], next: {}}, watch: null, startAt: 0, savedAt: 0, liveTimer: null, homeRender: 0
};

function message(text) { $('#notice').textContent = text; $('#notice').hidden = !text; }
function tokenForApi() { return (store.get('token') || '').trim(); }
function requestHeaders(extra = {}, includeToken = true) {
  const headers = {'Content-Type': 'application/json', ...(extra || {})};
  if (includeToken) {
    const token = tokenForApi();
    if (token && !headers.Authorization && !headers.authorization) headers.Authorization = 'Bearer ' + token;
  }
  return headers;
}

function requireLogin(msg = '') {
  state.role = 'viewer';
  state.ticket = null; state.current = null; state.job = null; ++state.playback;
  state.favorites = [];
  state.history = {items: [], next: {}}; state.watch = null;
  if (state.controller) state.controller.abort(); state.controller = null;
  destroyPlayer(); $('#player-wrap').hidden = true;
  clearInterval(state.jobTimer); clearInterval(state.configTimer); state.jobTimer = null; state.configTimer = null;
  $('#login').hidden = false; $('#app').hidden = true;
  $('#tab-conf').hidden = true;
  $('#login-error').textContent = msg || '';
  $('#token-input').value = '';
  const u = $('#user-input'); if (u) u.value = '';
}

function failure(err) { if (err && err.name !== 'AbortError') message(err.message || 'Une erreur est survenue.'); }
window.addEventListener('unhandledrejection', e => failure(e.reason || {}));
window.addEventListener('error', e => { if (e.message) message('Erreur : ' + e.message); });

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  const forward = () => controller.abort();
  if (options.signal) { if (options.signal.aborted) controller.abort(); else options.signal.addEventListener('abort', forward, {once: true}); }
  try {
    const res = await fetch('/api' + path, {
      ...options,
      signal: controller.signal,
      credentials: 'same-origin',
      headers: requestHeaders(options.headers, options.sendToken !== false)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || 'Erreur serveur ' + res.status);
      if (res.status === 401 && path !== '/login' && !options.skipAuthRedirect) {
        store.remove('token');
        requireLogin(err.message);
      }
      throw err;
    }
    return data;
  } catch (err) {
    if (err.name === 'AbortError' && !(options.signal && options.signal.aborted)) {
      throw new Error('Le serveur met trop de temps à répondre. Réessayez.');
    }
    throw err;
  } finally {
    clearTimeout(timer);
    if (options.signal) options.signal.removeEventListener('abort', forward);
  }
}

const post = (path, data = {}, options = {}) => api(path, {method: 'POST', body: JSON.stringify(data), ...options});
function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}
function size(n) { return n >= 1e9 ? (n / 1e9).toFixed(2) + ' Go' : Math.round(n / 1e6) + ' Mo'; }
function favKey(c) { return (c.lang || '') + '|' + (c.canonical || c.label || c.title); }
function isFav(c) { return state.favorites.some(f => favKey(f) === favKey(c)); }
function sameChannel(a, b) { return channelKey(a) === channelKey(b); }
function channelKey(c) {
  if (!c) return '';
  return (c.lang || '') + '|' + (c.canonical || '') + '|' + (c.label || c.title || '');
}

function getChannelColor(str) {
  if (!str) return '#1e1e30';
  let hash = 0;
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
  const tints = ['#241b45', '#1c2140', '#2a1a3e', '#1a2438', '#301b38', '#20204a'];
  return tints[Math.abs(hash) % tints.length];
}

async function connected(role) {
  state.role = role;
  $('#login').hidden = true; $('#app').hidden = false; $('#tab-conf').hidden = role !== 'admin';
  api('/me', {skipAuthRedirect: true}).then(me => applyMaxMode(me.max_mode)).catch(() => {});
  const route = parseRoute();
  const [favorites] = await Promise.all([
    api('/favorites').catch(() => []),
    migrateRecents().then(() => loadHistory())
  ]);
  state.favorites = favorites;
  // Les filtres ne servent qu'aux catalogues : l'accueil s'affiche sans eux.
  const filters = loadFilters();
  if (route.mode !== 'home') await filters;
  await setMode(route.mode, {route: false});
  // Normalise l'adresse (#/accueil au premier chargement) sans empiler d'entree.
  window.history.replaceState(null, '', location.hash && route.id ? location.hash : routeHash(route.mode));
  openRouteDialog(route);
  offerResume();
}

// Support de connexion souple (Identifiant/Mot de passe ou Jeton direct)
$('#login-form').onsubmit = async e => {
  e.preventDefault();
  $('#token-go').disabled = true;
  $('#login-error').textContent = '';
  const username = ($('#user-input') ? $('#user-input').value.trim() : '');
  const password = $('#token-input').value.trim();
  try {
    // La case « Rester connecte » n'etait lue nulle part : le cookie etait
    // toujours emis pour sept jours, cochee ou non.
    const remember = $('#remember-token') ? $('#remember-token').checked : true;
    const payload = username ? {username, password, remember} : {token: password, password, remember};
    const r = await post('/login', payload);
    store.remove('token');
    $('#token-input').value = '';
    await connected(r.role);
  } catch (err) {
    $('#login-error').textContent = err.message || 'Identifiants ou jeton refusés.';
  } finally {
    $('#token-go').disabled = false;
  }
};

$('#logout').onclick = async () => {
  await stop().catch(failure);
  await post('/logout', {}, {skipAuthRedirect: true}).catch(() => {});
  store.remove('token');
  location.reload();
};

// Preferences
$('#play-mode').value = store.get('playmode', 'sport');
// L'instance peut etre bornee (max_mode) : on ne propose pas plus haut, sinon
// le serveur baisserait la qualite sans le dire.
const MODE_RANK = {eco: 0, balanced: 1, sport: 2};
function applyMaxMode(max) {
  const limit = MODE_RANK[max] === undefined ? 2 : MODE_RANK[max];
  [...$('#play-mode').options].forEach(o => { if (o.value in MODE_RANK) o.hidden = o.disabled = MODE_RANK[o.value] > limit; });
  const current = $('#play-mode').value;
  if (current in MODE_RANK && MODE_RANK[current] > limit) $('#play-mode').value = Object.keys(MODE_RANK)[limit];
  modeHint();
}
$('#connection').value = store.get('connection', 'stable');
function modeHint() {
  const mode = $('#play-mode').value;
  $('#budget-fields').hidden = mode !== 'budget';
  $('#mode-hint').textContent = {
    eco: 'Qualité plafonnée pour préserver votre forfait mobile.',
    balanced: 'Un équilibre optimal entre netteté et consommation.',
    sport: 'La meilleure image que votre connexion permet, jusqu’à 720p, cadence d’origine.',
    budget: 'Volume et durée dédiés à cette séance avec coupure automatique protectrice.'
  }[mode] || '';
}

async function changePreferences() {
  store.set('playmode', $('#play-mode').value);
  store.set('connection', $('#connection').value);
  modeHint();
  if (state.current && !state.job) {
    const c = state.current;
    await play(c);
  }
}
$('#play-mode').onchange = () => changePreferences().catch(failure);
$('#connection').onchange = () => changePreferences().catch(failure);
$('#budget-mb').onchange = $('#budget-minutes').onchange = () => {
  if (state.ticket) message('Le nouveau budget sera appliqué à la prochaine séance.');
};

// Presets Budget
$$('.budget-preset-btn').forEach(btn => {
  btn.onclick = () => {
    $('#budget-mb').value = btn.dataset.mb;
    $('#budget-minutes').value = btn.dataset.min;
    if (state.ticket) message('Le nouveau budget sera appliqué à la prochaine séance.');
  };
});
modeHint();

function destroyPlayer() {
  clearTimeout(state.recoveryTimer);
  state.recoveryTimer = null;
  if (state.hls) state.hls.destroy();
  state.hls = null;
  const v = $('#video');
  v.pause();
  v.removeAttribute('src');
  v.querySelectorAll('track').forEach(t => t.remove());
  v.load();
}

function clearZapOverlay() {
  const overlay = $('#channel-zap-overlay');
  if (!overlay) return;
  clearTimeout(state.zapTimer);
  state.zapTimer = null;
  overlay.classList.remove('visible');
  overlay.hidden = true;
}

function markNowPlaying(channel) {
  const key = channelKey(channel);
  const cards = Array.from($$('#channels li[data-channel-key], #live-rail li[data-channel-key]'));
  cards.forEach(card => card.classList.toggle('now-playing', card.dataset.channelKey === key));
  const inRail = cards.find(card => card.closest('#live-rail') && card.dataset.channelKey === key);
  // Defilement horizontal seul : scrollIntoView ferait aussi descendre la page
  // et cacherait le haut de la video.
  if (inRail) {
    const rail = $('#live-rail');
    rail.scrollTo({left: inRail.offsetLeft - (rail.clientWidth - inRail.offsetWidth) / 2, behavior: 'smooth'});
  }
  if (!channel) return;
  const idx = state.channelList.findIndex(item => sameChannel(item, channel));
  state.selectedChannelIndex = idx >= 0 ? idx : state.selectedChannelIndex;
}

function nextChannelFromCurrent(offset) {
  if (!state.current) return null;
  if (state.mode !== 'live' && state.mode !== 'favorites') return null;
  const list = state.channelList;
  if (!list.length) return null;
  const idx = list.findIndex(item => sameChannel(item, state.current));
  if (idx < 0) return null;
  return list[(idx + offset + list.length) % list.length];
}

function showZapOverlay(channel, direction) {
  const overlay = $('#channel-zap-overlay');
  if (!overlay) return;
  const directionLabel = $('#zap-direction');
  const title = $('#zap-channel');
  if (directionLabel) directionLabel.textContent = direction < 0 ? 'Chaîne précédente' : 'Chaîne suivante';
  if (title) title.textContent = channel.label || channel.title || '';
  overlay.hidden = false;
  overlay.classList.add('visible');
  clearTimeout(state.zapTimer);
  state.zapTimer = setTimeout(() => {
    state.zapTimer = null;
    overlay.classList.remove('visible');
    overlay.hidden = true;
  }, 900);
}

function zapChannel(offset) {
  if (!state.current || (state.mode !== 'live' && state.mode !== 'favorites') || $('#player-wrap').hidden) return;
  const target = nextChannelFromCurrent(offset);
  if (!target) return;
  showZapOverlay(target, offset);
  play(target).catch(failure);
}

async function stop() {
  // Avant destroyPlayer : il remet la video a zero.
  const saving = saveProgress(true);
  ++state.playback;
  const ticket = state.ticket;
  state.ticket = null;
  destroyPlayer();
  state.current = null;
  state.job = null;
  state.watch = null;
  state.startAt = 0;
  clearTimeout(state.liveTimer);
  tabStore.remove('playing');
  markNowPlaying(null);
  clearZapOverlay();
  $('#player-wrap').hidden = true;
  stageScreen(null);
  $('#program-info').hidden = true;
  $('#idle-hero').hidden = false;
  document.body.classList.remove('is-playing', 'reader-focus', 'catalogue-collapsed');
  $('#preferences').hidden = false;
  renderRecents();
  if (ticket) await post('/stop', {ticket}, {skipAuthRedirect: true}).catch(failure);
  await saving;
  // Retour a l'accueil : la position qu'on vient d'enregistrer doit s'y voir.
  if (state.mode === 'home' && $('#player-wrap').hidden && !state.configOpen) renderHome().catch(() => {});
}

// Gestion Collapse Catalogue & Zapping
function updateSidebarUI() {
  const isCollapsed = document.body.classList.contains('catalogue-collapsed');
  const textSpan = $('#toggle-sidebar-text');
  if (textSpan) textSpan.textContent = isCollapsed ? 'Afficher les chaînes' : 'Masquer les chaînes';
  const btn = $('#toggle-sidebar');
  if (btn) {
    btn.setAttribute('aria-expanded', String(!isCollapsed));
    btn.title = isCollapsed ? 'Afficher le catalogue pour zapper' : 'Agrandir le lecteur (mode cinéma)';
  }
}

function toggleCatalogueSidebar() {
  document.body.classList.toggle('catalogue-collapsed');
  const isCollapsed = document.body.classList.contains('catalogue-collapsed');
  store.set('sidebar_collapsed', isCollapsed ? '1' : '0');
  updateSidebarUI();
}

const toggleSidebarBtn = $('#toggle-sidebar');
if (toggleSidebarBtn) {
  toggleSidebarBtn.onclick = toggleCatalogueSidebar;
}

// Gestion Télémétrie Pliable
function updateTelemetryUI() {
  const telem = $('#telemetry');
  const btn = $('#toggle-telemetry');
  if (!telem || !btn) return;
  const isOpen = !telem.hidden;
  btn.setAttribute('aria-expanded', String(isOpen));
  btn.classList.toggle('active', isOpen);
}

function toggleTelemetry() {
  const telem = $('#telemetry');
  if (telem) {
    telem.hidden = !telem.hidden;
    store.set('telemetry_open', telem.hidden ? '0' : '1');
    updateTelemetryUI();
  }
}

const toggleTelemBtn = $('#toggle-telemetry');
if (toggleTelemBtn) {
  toggleTelemBtn.onclick = toggleTelemetry;
}

// Ecran Streamly par-dessus la video : on dit ce qui se passe, et a qui s'adresser.
// Le renvoi vers le fournisseur n'apparait que si le serveur a constate que
// toutes les sources de la chaine ont echoue.
const STAGE_SCREENS = {
  prepare: ['Préparation du direct…', 'COMPRESSION ADAPTÉE À VOTRE CONNEXION'],
  movie: ['Ouverture du film…', 'VERSION ADAPTÉE À VOTRE FORFAIT'],
  wait: ['Reprise du direct…', 'LA SOURCE EST INSTABLE · MERCI DE PATIENTER QUELQUES SECONDES'],
  offline: ['Votre connexion Internet est interrompue', 'LA LECTURE REPRENDRA DÈS SON RETOUR'],
  slow: ['Votre connexion est trop lente pour cette qualité', 'STREAMLY PASSE À UNE QUALITÉ PLUS LÉGÈRE · RAPPROCHEZ-VOUS DU WI-FI OU CHANGEZ DE RÉSEAU'],
  source: ['Cette chaîne ne répond pas chez votre fournisseur', 'ESSAYEZ UNE AUTRE CHAÎNE · SI LE PROBLÈME DURE, CONTACTEZ VOTRE FOURNISSEUR IPTV']
};

// Debit du spectateur, mesure pendant que l'encodeur demarre : sa connexion ne
// sert a rien d'autre a ce moment-la. 200 Ko, garde dix minutes pour ne pas
// depenser de donnees a chaque zapping.
const NET_LEVELS = [
  [0.6, 'faible', 'la qualité restera basse pour éviter les coupures'],
  [1.5, 'correcte', 'idéale pour le 480p'],
  [4, 'bonne', 'le 720p passera sans peine'],
  [Infinity, 'excellente', 'toutes les qualités passeront']
];

async function measureConnection() {
  const cached = store.json('net', null);
  if (cached && Date.now() - cached.at < 600000) return cached.mbps;
  const res = await fetch('/api/speedtest?t=' + Date.now(), {credentials: 'same-origin', cache: 'no-store'});
  if (!res.ok || !res.body) throw new Error('mesure impossible');
  // Sur une liaison lointaine, le debut d'un transfert est bride par la montee
  // en charge de TCP, pas par le debit : on ne chronometre que la fin, une
  // fois la connexion lancee. Mesurer le tout sous-estimait de moitie.
  const total = Number(res.headers.get('Content-Length')) || 200000;
  const reader = res.body.getReader();
  let received = 0, markBytes = 0, markAt = 0, firstAt = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    if (!firstAt) firstAt = performance.now();
    received += value.byteLength;
    if (!markAt && received >= total * 0.4) { markBytes = received; markAt = performance.now(); }
  }
  const end = performance.now();
  // Connexion tres rapide : tout arrive d'un bloc, il n'y a pas de « fin » a
  // chronometrer. On retombe alors sur le transfert entier.
  const tail = received - markBytes >= 20000;
  const mbps = tail ? (received - markBytes) * 8 / Math.max(end - markAt, 5) / 1000
                    : received * 8 / Math.max(end - firstAt, 5) / 1000;
  if (!received) throw new Error('mesure impossible');
  store.set('net', JSON.stringify({mbps, at: Date.now()}));
  return mbps;
}

function connectionPill(mbps, advice, verdict) {
  const slot = $('#stage-loader-net');
  let [, label, fallback] = NET_LEVELS.find(([limit]) => mbps < limit);
  // « Correcte » dans l'absolu peut etre insuffisante pour la qualite en cours.
  if (verdict) label = verdict;
  // Au-dela de 20 Mbit/s, la mesure n'est plus fine : on le dit tel quel.
  const shown = mbps >= 20 ? 'plus de 20' : '≈ ' + mbps.toLocaleString('fr-FR', {maximumFractionDigits: mbps < 10 ? 1 : 0});
  slot.replaceChildren(el('span', '', 'Votre connexion : '), el('strong', 'net-' + (verdict ? 'faible' : label), shown + ' Mbit/s · ' + label),
    el('span', 'net-advice', advice || fallback));
  slot.hidden = false;
}

function showConnection() {
  $('#stage-loader-net').hidden = true;
  const attempt = state.playback;
  measureConnection().then(mbps => {
    if (attempt === state.playback && !$('#stage-loader').hidden) connectionPill(mbps);
  }).catch(() => {});
}

// Pendant la lecture, hls.js mesure le debit reel sur chaque segment telecharge.
// Quand l'image cale, on sait donc si c'est la connexion du spectateur qui ne
// suit pas, ou la source : on ne met pas sur le dos du reseau ce qui vient d'ailleurs.
function stallCause() {
  if (!navigator.onLine) return {kind: 'offline'};
  const hls = state.hls;
  const level = hls && hls.levels && hls.levels[hls.currentLevel >= 0 ? hls.currentLevel : hls.loadLevel];
  const estimate = hls && hls.bandwidthEstimate;
  if (level && estimate && estimate < level.bitrate * 1.15) {
    return {kind: 'slow', mbps: estimate / 1e6,
            advice: 'il en faudrait ' + (level.bitrate * 1.2 / 1e6).toLocaleString('fr-FR', {maximumFractionDigits: 1}) + ' Mbit/s pour le ' + level.height + 'p'};
  }
  return {kind: 'wait'};
}

function stageScreen(kind) {
  const box = $('#stage-loader');
  clearTimeout(state.stallTimer);
  state.stallTimer = null;
  // Pendant l'attente, le bouton retour reste visible au-dessus de l'ecran.
  $('#video-stage').classList.toggle('loading', !!kind);
  if (!kind) { box.hidden = true; return; }
  const [title, sub] = STAGE_SCREENS[kind];
  $('#stage-loader-text').textContent = title;
  $('#stage-loader-sub').textContent = sub;
  // Une panne de source n'avance pas toute seule : pas de barre animee, un bouton.
  box.classList.toggle('settled', kind === 'source');
  $('#stage-retry').hidden = kind !== 'source';
  $('#stage-loader-net').hidden = true;
  box.hidden = false;
  if (kind === 'prepare') showConnection();
}

$('#stage-retry').onclick = () => { if (state.current) play(state.current).catch(failure); };

function playbackUI(title, live) {
  $('#player-wrap').hidden = false;
  $('#idle-hero').hidden = true;
  $('#program-info').hidden = true;
  $('#now-playing').textContent = title;
  $('#preferences').hidden = true;
  const recent = $('#recent-wrap');
  if (recent) recent.hidden = true;
  document.body.classList.add('is-playing', 'reader-focus');

  // Restaurer état plié/déplié de la barre latérale & télémétrie
  document.body.classList.remove('catalogue-collapsed');
  updateSidebarUI();

  const telem = $('#telemetry');
  if (telem) {
    telem.hidden = store.get('telemetry_open') !== '1';
    updateTelemetryUI();
  }

  $('#live-badge').innerHTML = live ? '<span class="pulse-dot" aria-hidden="true"></span> DIRECT' : 'FILM';
  $('#live-badge').classList.toggle('film', !live);
  $('#video-stage').classList.toggle('is-live', live);
  $('#pui-time').textContent = '';
  $('#pui-played').style.width = '0';
  $('#pui-buffered').style.width = '0';
  $('#quality').replaceChildren(new Option('Auto', '-1'));
  showControls();
  $('#toggle-sidebar').hidden = true;
  renderLiveRail(live);
  $('#back-live').hidden = !live;
  $('#player-status').textContent = 'Préparation de la lecture…';
  // L'encodeur met quelques secondes a produire : on le dit, plutot qu'un ecran noir.
  stageScreen(live ? 'prepare' : 'movie');
  $('#usage').textContent = live ? '0 Mo de vidéo' : 'Version préparée';
  $('#bitrate').textContent = 'Qualité automatique';
  $('#budget-meter').hidden = true;
  state.stalls = 0;
  state.frames = false;
  state.started = performance.now();
  state.retries = 0;
  $('#pui-subtitle').textContent = '';
  // Un film ferme la rangee des chaines ; en zappant, elle reste ouverte.
  if (!live) setRail(false);
}

async function play(channel) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.current = channel;
  state.generation = null;
  playbackUI(channel.label, true);
  $('#pui-subtitle').textContent = [channel.category, channel.lang].filter(Boolean).join(' · ');
  message('');
  updateProgramInfo().catch(() => {});
  
  markNowPlaying(channel);

  try {
    const info = await post('/play', {
      lang: channel.lang,
      canonical: channel.canonical,
      mode: $('#play-mode').value,
      budget_mb: Number($('#budget-mb').value),
      minutes: Number($('#budget-minutes').value)
    });
    if (attempt !== state.playback) {
      await post('/stop', {ticket: info.ticket});
      return;
    }
    state.ticket = info.ticket;
    state.url = info.play_url;
    $('#budget-meter').hidden = !info.budget;
    attach(info.play_url, true, attempt);
    markNowPlaying(channel);
    hideResume();
    tabStore.set('playing', {kind: 'live', channel});
    rememberWhenWatched(channel, attempt);
  } catch (err) {
    if (attempt === state.playback) {
      stageScreen(null);
      $('#player-status').textContent = err.message;
      message(err.message);
    }
  }
}

// Le moteur video (400 Ko) n'est charge qu'a la premiere lecture : l'accueil
// et les catalogues s'affichent sans l'attendre.
let hlsLoading = null;
function loadHls() {
  if (window.Hls) return Promise.resolve();
  if (!hlsLoading) {
    hlsLoading = new Promise(resolve => {
      const script = document.createElement('script');
      script.src = 'vendor/hls.min.js?v=1.5';
      script.onload = resolve;
      script.onerror = () => { hlsLoading = null; resolve(); };
      document.head.append(script);
    });
  }
  return hlsLoading;
}

// Premiere estimation du debit : la mesure faite au demarrage d'une chaine si
// elle est recente, sinon 1,5 Mbit/s. Partir trop bas imposait une image
// floue pendant les premieres secondes, le temps de remonter.
function startEstimate() {
  const net = store.json('net', null);
  if (net && Date.now() - net.at < 3600000 && net.mbps > 0) return Math.round(net.mbps * 850000);
  return 1500000;
}

function attach(url, live, attempt) {
  if (attempt !== state.playback) return;
  if (!window.Hls) { loadHls().then(() => attach(url, live, attempt)); return; }
  if (state.hls) state.hls.destroy();
  state.hls = null;
  const v = $('#video');
  // Lecture automatique refusee (son actif sans geste recent, iPhone) : on
  // retire l'ecran d'attente pour laisser voir le bouton ▶, sinon il le cachait.
  const startPlaying = () => v.play().catch(() => {
    if (attempt !== state.playback) return;
    stageScreen(null);
    syncPlayState();
    $('#player-status').textContent = 'Appuyez sur ▶ pour démarrer.';
  });

  if (window.Hls && Hls.isSupported()) {
    const unstable = $('#connection').value === 'unstable';
    const hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      maxBufferLength: unstable && live ? 45 : 18,
      maxMaxBufferLength: 90,
      backBufferLength: live ? 15 : 30,
      maxBufferSize: 40 * 1000 * 1000,
      liveSyncDuration: unstable ? 30 : 6,
      liveMaxLatencyDuration: unstable ? 65 : 25,
      maxLiveSyncPlaybackRate: 1,
      abrEwmaDefaultEstimate: startEstimate(),
      // La meilleure image que la connexion permet, quelle que soit la
      // taille du lecteur ; monter en qualite des que le debit le permet.
      capLevelToPlayerSize: false,
      abrBandWidthUpFactor: 0.8,
      startFragPrefetch: true,
      // Film en preparation : apres un saut, le serveur encode le passage
      // demande avant de repondre. On attend plutot que d'abandonner.
      ...(live ? {} : {startPosition: state.startAt || 0, fragLoadPolicy: {default: {
        maxTimeToFirstByteMs: 30000,
        maxLoadTimeMs: 120000,
        timeoutRetry: {maxNumRetry: 6, retryDelayMs: 1000, maxRetryDelayMs: 4000},
        errorRetry: {maxNumRetry: 20, retryDelayMs: 1500, maxRetryDelayMs: 4000}
      }}})
    });
    state.hls = hls;
    hls.attachMedia(v);
    hls.loadSource(url);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      const select = $('#quality');
      select.replaceChildren(new Option('Auto', '-1'));
      hls.levels.forEach((level, i) => {
        select.add(new Option((level.height ? level.height + 'p' : 'Qualité ' + (i + 1)) + ' · ≈ ' + size(level.bitrate * 3600 / 8) + '/h', String(i)));
      });
      startPlaying();
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, (_, d) => {
      const level = hls.levels[d.level];
      if (level) $('#bitrate').textContent = (level.height ? level.height + 'p · ' : '') + '≈ ' + size(level.bitrate * 3600 / 8) + '/h';
      // En automatique, le menu dit quelle qualite est jouee en ce moment.
      if (level && hls.autoLevelEnabled && level.height) $('#quality').options[0].text = 'Auto · ' + level.height + 'p';
    });
    hls.on(Hls.Events.ERROR, (_, d) => {
      if (attempt !== state.playback) return;
      if (d.response && d.response.code === 402) {
        destroyPlayer();
        $('#player-status').textContent = 'Budget vidéo atteint. Séance arrêtée.';
        return;
      }
      if (!d.fatal) return;
      if (++state.retries > 4) {
        hls.stopLoad();
        $('#player-status').textContent = 'Lecture interrompue. Relancez la chaîne pour réessayer.';
        return;
      }
      $('#player-status').textContent = navigator.onLine ? 'Reconnexion en cours…' : 'Connexion Internet interrompue…';
      clearTimeout(state.recoveryTimer);
      state.recoveryTimer = setTimeout(() => {
        if (attempt !== state.playback) return;
        if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else hls.startLoad();
      }, Math.min(12000, 1000 * 2 ** state.retries));
    });
  } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = url;
    $('#quality').replaceChildren(new Option('Auto', '-1'));
    startPlaying();
  } else {
    $('#player-status').textContent = 'Ce navigateur ne peut pas lire ce flux. Essayez un navigateur récent.';
  }
}

$('#stop').onclick = () => stop().catch(failure);
$('#quality').onchange = e => { if (state.hls) state.hls.currentLevel = Number(e.target.value); };
$('#zap-prev').onclick = () => zapChannel(-1);
$('#zap-next').onclick = () => zapChannel(1);
$('#back-live').onclick = () => {
  const v = $('#video');
  if (state.hls && state.hls.liveSyncPosition) v.currentTime = state.hls.liveSyncPosition;
  else if (v.seekable.length) v.currentTime = Math.max(0, v.seekable.end(v.seekable.length - 1) - 6);
};
// Plein ecran sur le cadre entier : nos commandes restent visibles. L'iPhone
// ne le permet que sur la video elle-meme, avec ses propres commandes.
async function toggleFullscreen() {
  const box = $('#video-stage'), v = $('#video');
  try {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      await (document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen());
    } else if (box.requestFullscreen) {
      await box.requestFullscreen();
    } else if (box.webkitRequestFullscreen) {
      box.webkitRequestFullscreen();
    } else if (v.webkitEnterFullscreen) {
      v.webkitEnterFullscreen();
    }
  } catch (err) { failure(err); }
}
$('#fullscreen').onclick = () => toggleFullscreen();
$('#pip').hidden = !document.pictureInPictureEnabled;
$('#pip').onclick = () => {
  if (document.pictureInPictureElement) document.exitPictureInPicture().catch(failure);
  else $('#video').requestPictureInPicture().catch(failure);
};

const video = $('#video');
video.addEventListener('waiting', () => {
  if (state.frames) state.stalls++;
  $('#player-status').textContent = state.job && state.job.state === 'preparing'
    ? 'Encodage de ce passage sur le serveur…' : 'Mise en réserve…';
  // Un court passage a vide est normal ; au-dela, on l'explique.
  if (state.frames && !state.job && !state.stallTimer && $('#stage-loader').hidden) {
    state.stallTimer = setTimeout(() => {
      state.stallTimer = null;
      if (!state.ticket || video.readyState >= 3) return;
      const cause = stallCause();
      stageScreen(cause.kind);
      if (cause.kind === 'slow') {
        connectionPill(cause.mbps, cause.advice, 'insuffisante');
        // La mesure prise au demarrage n'est plus vraie : on la remplace.
        store.set('net', JSON.stringify({mbps: cause.mbps, at: Date.now()}));
      }
    }, 3000);
  }
});
video.addEventListener('playing', () => {
  stageScreen(null);
  $('#player-status').textContent = state.frames ? 'Lecture en cours' : 'Image en ' + ((performance.now() - state.started) / 1000).toFixed(1) + ' s';
  state.frames = true;
});
video.addEventListener('error', () => { if (!state.hls && state.current) $('#player-status').textContent = 'Flux interrompu. Vérification de la source en cours…'; });
video.addEventListener('timeupdate', () => saveProgress(false));
video.addEventListener('pause', () => { if (!video.ended) saveProgress(true); });
video.addEventListener('ended', () => saveProgress(true, true));
video.addEventListener('loadedmetadata', () => {
  // hls.js part deja de startPosition ; le lecteur natif (Safari) doit y aller.
  const start = state.startAt;
  state.startAt = 0;
  if (state.job && start && Math.abs(video.currentTime - start) > 3) video.currentTime = start;
});

// ====================================================================
// Commandes du lecteur : les notres plutot que celles du navigateur, pour
// une interface identique partout, qui s'efface pendant la lecture.
// ====================================================================
const stageBox = $('#video-stage');
const PUI_ICONS = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l12.5-7.5z" fill="currentColor"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 4.5h3.8v15H6.5zM13.7 4.5h3.8v15h-3.8z" fill="currentColor"/></svg>',
  sound: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5h3.5L12 5v14l-4.5-4.5H4z" fill="currentColor"/><path d="M16 9a4.2 4.2 0 0 1 0 6M18.6 6.4a8 8 0 0 1 0 11.2"/></svg>',
  muted: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5h3.5L12 5v14l-4.5-4.5H4z" fill="currentColor"/><path d="M16.5 9.5l5 5M21.5 9.5l-5 5"/></svg>',
  expand: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
  shrink: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/></svg>'
};

function clockOf(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return (h ? h + ':' + pad2(m) : m) + ':' + pad2(s % 60);
}

function togglePlay() {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

function seekBy(delta) {
  if (!Number.isFinite(video.duration)) return;
  video.currentTime = Math.max(0, Math.min(video.duration - 1, video.currentTime + delta));
  showControls();
}

let controlsTimer = null;
function showControls() {
  stageBox.classList.add('ui-on');
  clearTimeout(controlsTimer);
  // Rangee des chaines ouverte : les commandes restent, on est en train de choisir.
  if (!video.paused && !stageBox.classList.contains('rail-open')) {
    controlsTimer = setTimeout(() => stageBox.classList.remove('ui-on'), 3000);
  }
}

function setRail(open) {
  stageBox.classList.toggle('rail-open', open);
  $('#pui-channels').setAttribute('aria-expanded', String(open));
  if (open) markNowPlaying(state.current);
  showControls();
}

// « The Winter King S01E05 — Le retour » : titre, puis l'episode a part.
function splitTitle(title) {
  const m = String(title || '').match(/^(.*?)\s+S(\d+)\s*E(\d+)(?:\s+[—–-]\s+(.*))?$/i);
  if (!m) return {title: title || '', sub: ''};
  return {title: m[1], sub: ['S' + Number(m[2]), 'É' + Number(m[3]), m[4] || ''].filter(Boolean).join(' · ')};
}

function showJobTitle(job) {
  const parts = splitTitle(cleanTitle(job.title));
  $('#now-playing').textContent = parts.title;
  $('#pui-subtitle').textContent = parts.sub;
  $('#live-badge').textContent = job.kind === 'episode' || parts.sub ? 'ÉPISODE' : 'FILM';
}

function syncPlayState() {
  const paused = video.paused;
  $('#pui-play').innerHTML = paused ? PUI_ICONS.play : PUI_ICONS.pause;
  $('#pui-play').setAttribute('aria-label', paused ? 'Lecture' : 'Pause');
  stageBox.classList.toggle('paused', paused);
  showControls();
}

function syncVolume() {
  const silent = video.muted || video.volume === 0;
  $('#pui-mute').innerHTML = silent ? PUI_ICONS.muted : PUI_ICONS.sound;
  $('#pui-mute').setAttribute('aria-label', silent ? 'Rétablir le son' : 'Couper le son');
  $('#pui-vol').value = video.muted ? 0 : video.volume;
}

function syncTimeline() {
  const d = video.duration, t = video.currentTime;
  if (Number.isFinite(d) && d > 0) {
    $('#pui-played').style.width = (t / d * 100) + '%';
    let ahead = t;
    for (let i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= t + 1 && video.buffered.end(i) > ahead) ahead = video.buffered.end(i);
    }
    $('#pui-buffered').style.width = (ahead / d * 100) + '%';
    $('#pui-time').textContent = clockOf(t) + ' / ' + clockOf(d);
  }
  // Direct : le bouton signale qu'on regarde en differe.
  if (state.hls && state.hls.liveSyncPosition) {
    $('#back-live').classList.toggle('behind', state.hls.liveSyncPosition - t > 12);
  }
}

function syncFullscreen() {
  const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
  $('#fullscreen').innerHTML = on ? PUI_ICONS.shrink : PUI_ICONS.expand;
  $('#fullscreen').setAttribute('aria-label', on ? 'Quitter le plein écran' : 'Plein écran');
  stageBox.classList.toggle('is-fullscreen', on);
}

function syncSubtitles() {
  const track = video.textTracks[0];
  $('#pui-cc').hidden = !track;
  if (track) $('#pui-cc').setAttribute('aria-pressed', String(track.mode === 'showing'));
}

['play', 'pause'].forEach(ev => video.addEventListener(ev, syncPlayState));
video.addEventListener('volumechange', () => { syncVolume(); store.set('volume', String(video.volume)); });
['timeupdate', 'durationchange', 'progress', 'seeked'].forEach(ev => video.addEventListener(ev, syncTimeline));
video.textTracks.addEventListener('addtrack', syncSubtitles);
document.addEventListener('fullscreenchange', syncFullscreen);
document.addEventListener('webkitfullscreenchange', syncFullscreen);

video.volume = Math.min(1, Math.max(0, Number(store.get('volume', '1')) || 1));
syncPlayState();
syncVolume();
syncFullscreen();

$('#pui-play').onclick = togglePlay;
$('#pui-channels').onclick = () => setRail(!stageBox.classList.contains('rail-open'));
$('#pui-big').onclick = togglePlay;
$('#pui-back').onclick = () => seekBy(-10);
$('#pui-fwd').onclick = () => seekBy(10);
$('#pui-mute').onclick = () => {
  if (video.muted || video.volume === 0) { video.muted = false; if (!video.volume) video.volume = 0.6; }
  else video.muted = true;
};
$('#pui-vol').oninput = e => { video.volume = Number(e.target.value); video.muted = video.volume === 0; };
$('#pui-cc').onclick = () => {
  const track = video.textTracks[0];
  if (track) track.mode = track.mode === 'showing' ? 'hidden' : 'showing';
  syncSubtitles();
};

// Souris : les commandes apparaissent au moindre mouvement ; un clic sur
// l'image met en pause, un double clic passe en plein ecran. Tactile : un
// premier toucher montre les commandes, le suivant les masque.
stageBox.addEventListener('pointermove', e => { if (e.pointerType === 'mouse') showControls(); });
stageBox.addEventListener('mouseleave', () => { if (!video.paused) stageBox.classList.remove('ui-on'); });
video.addEventListener('click', () => {
  if (matchMedia('(hover: none)').matches) {
    if (stageBox.classList.contains('ui-on')) stageBox.classList.remove('ui-on');
    else showControls();
  } else {
    togglePlay();
  }
});
video.addEventListener('dblclick', () => toggleFullscreen());

// Barre de progression : survol pour voir le temps, glisser pour se deplacer.
const progressBox = $('#pui-progress');
function fractionAt(e) {
  const r = progressBox.getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
}
function previewAt(e) {
  const f = fractionAt(e), hover = $('#pui-hover');
  if (!Number.isFinite(video.duration)) return f;
  hover.textContent = clockOf(f * video.duration);
  hover.style.left = (f * 100) + '%';
  hover.hidden = false;
  return f;
}
let scrubbing = false;
progressBox.addEventListener('pointerdown', e => {
  if (!Number.isFinite(video.duration)) return;
  scrubbing = true;
  progressBox.setPointerCapture(e.pointerId);
  $('#pui-played').style.width = previewAt(e) * 100 + '%';
});
progressBox.addEventListener('pointermove', e => {
  if (!Number.isFinite(video.duration)) return;
  const f = previewAt(e);
  if (scrubbing) $('#pui-played').style.width = f * 100 + '%';
  showControls();
});
progressBox.addEventListener('pointerup', e => {
  if (!scrubbing) return;
  scrubbing = false;
  video.currentTime = fractionAt(e) * video.duration;
  $('#pui-hover').hidden = true;
});
progressBox.addEventListener('pointerleave', () => { if (!scrubbing) $('#pui-hover').hidden = true; });

let statusBusy = false;
setInterval(async () => {
  if (!state.ticket || statusBusy) return;
  statusBusy = true;
  const ticket = state.ticket;
  try {
    const st = await api('/playback?ticket=' + encodeURIComponent(ticket));
    if (ticket !== state.ticket) return;
    $('#usage').textContent = size(st.bytes) + (st.budget ? ' / ' + size(st.budget) : ' de vidéo');
    if (st.budget) {
      const pct = (st.bytes / st.budget) * 100;
      const prog = $('#budget-progress');
      prog.value = pct;
      prog.classList.toggle('danger', pct > 85);
    }
    if (st.state === 'failed') {
      destroyPlayer();
      stageScreen('source');
      $('#player-status').textContent = st.error ? ('Aucune source disponible : ' + st.error) : 'Aucune source disponible. Réessayez plus tard.';
      return;
    }
    if ((state.generation === null && st.generation > 0) || (state.generation !== null && state.generation !== st.generation)) {
      $('#player-status').textContent = 'Passage à une source de secours…';
      if (state.frames) stageScreen('wait');
      state.retries = 0;
      attach(state.url + '?generation=' + st.generation, true, state.playback);
    }
    state.generation = st.generation;
    state.statusMisses = 0;
    if (!video.paused && video.readyState >= 3) {
      $('#player-status').textContent = state.job ? 'Lecture en cours' : 'Direct en cours';
    }
  } catch (err) {
    if (ticket !== state.ticket) return;
    state.statusMisses = (state.statusMisses || 0) + 1;
    if (state.statusMisses >= 3 && (video.paused || video.readyState < 3)) {
      $('#player-status').textContent = err.message;
    }
  } finally {
    statusBusy = false;
  }
}, 4000);

setInterval(() => {
  if ($('#player-wrap').hidden) return;
  let ahead = 0;
  for (let i = 0; i < video.buffered.length; i++) {
    if (video.buffered.start(i) <= video.currentTime && video.buffered.end(i) >= video.currentTime) {
      ahead = video.buffered.end(i) - video.currentTime;
    }
  }
  $('#buffer').textContent = 'Réserve : ' + Math.round(ahead) + ' s';
  $('#stalls').textContent = state.stalls + ' interruption' + (state.stalls > 1 ? 's' : '');
}, 1000);

window.addEventListener('pagehide', () => {
  if (state.ticket) navigator.sendBeacon('/api/stop', new Blob([JSON.stringify({ticket: state.ticket})], {type: 'application/json'}));
  // Actualisation ou fermeture en plein film : la position part quand meme.
  const body = progressBody();
  if (body && body.position >= 5) {
    notePlayingPosition(body.position);
    navigator.sendBeacon('/api/history', new Blob([JSON.stringify(body)], {type: 'application/json'}));
  }
});
window.addEventListener('offline', () => message('Vous êtes hors connexion. La lecture reprend tant que la réserve le permet.'));
window.addEventListener('online', () => message('Connexion rétablie.'));

// Skeletons
function renderSkeletons(count = 6) {
  $('#channels').replaceChildren();
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const card = el('li', 'skeleton-card shimmer');
    const logo = el('div', 'skeleton-logo');
    const lines = el('div', 'skeleton-lines');
    lines.append(el('div', 'skeleton-line title'), el('div', 'skeleton-line subline'));
    card.append(logo, lines);
    fragment.append(card);
  }
  $('#channels').append(fragment);
}

// Catalogue row
function row(c, kind = 'channel', number = 0) {
  const movie = kind === 'movie' || kind === 'series';
  return movie ? posterRow(c, kind) : channelRow(c, number);
}

// Film ou serie : affiche, titre et categorie dessous.
function posterRow(c, kind) {
  const li = el('li', 'channel-card movie-card');
  li.dataset.channelKey = channelKey(c);
  const button = el('button', 'channel-main');
  button.type = 'button';
  // Affiche : l'image du panel, sinon une affiche generee avec le titre.
  button.append(artwork(c.icon, c.title, 'logo-slot'));
  const meta = el('span', 'meta');
  meta.append(el('div', 'name', c.title), el('div', 'sub', c.category_name || (kind === 'series' ? 'Série' : 'Film')));
  button.append(meta);
  button.onclick = () => (kind === 'series' ? seriesDialog(c) : movieDialog(c)).catch(failure);
  li.append(button);
  return li;
}

// Chaine : vignette 16:9 (logo, numero, avancement du programme), puis le nom
// et le programme en cours, rempli apres coup par loadNowNext.
function channelRow(c, number = 0) {
  const li = el('li', 'channel-card live-card');
  li.dataset.channelKey = channelKey(c);
  if (c.epg_id) li.dataset.epgId = c.epg_id;
  const button = el('button', 'channel-main');
  button.type = 'button';
  const tile = paint(el('span', 'tile'), c.label);
  if (number) tile.append(el('span', 'ch-num', String(number)));
  tile.append(el('span', 'brand', c.label || c.canonical));
  // Les logos viennent de serveurs tiers, lents, et souvent en http (bloques
  // sur une page https). Le serveur les relaie et les garde : la vignette
  // s'affiche tout de suite, le logo la remplace quand il arrive.
  const img = imageFor(c.icon, () => tile.classList.add('has-img'));
  if (img) tile.append(img);
  const progress = bar(0);
  progress.hidden = true;
  tile.append(progress);
  const meta = el('span', 'meta');
  meta.append(el('div', 'name', c.label),
    el('div', 'sub', [c.category || 'Direct', c.lang].filter(Boolean).join(' · ')));
  button.append(tile, meta);
  button.onclick = () => play(c).catch(failure);
  button.ondblclick = () => fullscreenWhenReady();
  li.append(button);

  const star = el('button', 'star' + (isFav(c) ? ' on' : ''), '★');
  star.type = 'button';
  star.setAttribute('aria-label', 'Ma liste : ' + c.label);
  star.setAttribute('aria-pressed', String(isFav(c)));
  star.onclick = async e => {
    e.stopPropagation();
    star.disabled = true;
    try {
      await post(isFav(c) ? '/favorites/delete' : '/favorites', {lang: c.lang, canonical: c.canonical, label: c.label});
      state.favorites = await api('/favorites').catch(() => []);
      star.classList.toggle('on', isFav(c));
      star.setAttribute('aria-pressed', String(isFav(c)));
      if (state.mode === 'favorites') await renderChannels();
    } catch (err) {
      failure(err);
    } finally {
      star.disabled = false;
    }
  };
  li.append(star);
  return li;
}

// Sous le lecteur, les chaines de la liste en cours : on zappe sans quitter
// l'image. Reconstruite seulement quand la liste change.
function renderLiveRail(live) {
  const list = live ? state.channelList.slice(0, 150) : [];
  $('#live-rail-wrap').hidden = !list.length;
  $('#pui-channels').hidden = !list.length;
  if (!list.length) setRail(false);
  const key = list.length + '|' + list.map(channelKey).slice(0, 3).join('|');
  if (!list.length || key === state.railKey) return;
  state.railKey = key;
  $('#live-rail').replaceChildren(...list.map((c, i) => channelRow(c, i + 1)));
  loadNowNext(list).catch(() => {});
}

// Programme en cours : une requete par page de chaines, sur le guide en cache du serveur.
const clock = epoch => new Date(epoch * 1000).toLocaleTimeString('fr-FR', {hour: '2-digit', minute: '2-digit'});
const progressOf = p => Math.max(0, Math.min(100, (Date.now() / 1000 - p.start) / Math.max(1, p.stop - p.start) * 100));

async function loadNowNext(items) {
  const ids = [...new Set(items.map(c => c.epg_id).filter(Boolean))];
  if (!ids.length) return;
  const guide = await api('/guide/now?ids=' + encodeURIComponent(ids.join(',')), {skipAuthRedirect: true});
  $$('#channels li[data-epg-id], #live-rail li[data-epg-id]').forEach(card => {
    const entry = guide[card.dataset.epgId];
    const program = entry && (entry.now || entry.next);
    if (!program) return;
    card.querySelector('.meta .sub').textContent = (entry.now ? '' : 'À ' + clock(program.start) + ' · ') + program.title;
    card.classList.add('has-program');
    if (entry.now) {
      const progress = card.querySelector('.tile .bar');
      progress.firstChild.style.width = progressOf(entry.now) + '%';
      progress.hidden = false;
    }
  });
}

async function updateProgramInfo() {
  const box = $('#program-info');
  const channel = state.current;
  if (!channel || !channel.epg_id || state.job) { box.hidden = true; return; }
  const guide = await api('/guide/now?ids=' + encodeURIComponent(channel.epg_id), {skipAuthRedirect: true}).catch(() => ({}));
  const entry = guide[channel.epg_id];
  if (state.current !== channel || !entry || !(entry.now || entry.next)) { box.hidden = true; return; }
  const now = entry.now, next = entry.next;
  $('.program-now', box).hidden = !now;
  $('.program-bar', box).hidden = !now;
  if (now) {
    $('#program-now-title').textContent = now.title;
    $('#program-now-time').textContent = clock(now.start) + ' – ' + clock(now.stop);
    $('#program-progress').style.width = progressOf(now) + '%';
  }
  $('.program-next', box).hidden = !next;
  if (next) {
    $('#program-next-title').textContent = next.title;
    $('#program-next-time').textContent = clock(next.start);
  }
  box.hidden = false;
}
setInterval(() => { if (state.current) updateProgramInfo().catch(() => {}); }, 30000);

function fullscreenWhenReady(tries = 20) {
  // Le double-clic suit un clic qui vient de lancer la lecture : on attend
  // que le lecteur soit affiche, dans la fenetre d'activation du geste.
  if (!$('#player-wrap').hidden) { if (!document.fullscreenElement) $('#fullscreen').click(); return; }
  if (tries > 0) setTimeout(() => fullscreenWhenReady(tries - 1), 100);
}

async function renderChannels(more = false) {
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  const serial = ++state.request;
  if (!more) {
    state.page = 0;
    renderSkeletons(state.pageSize > 12 ? 6 : state.pageSize);
  }
  $('#load-more').disabled = true;
  try {
    let items;
    if (state.mode === 'favorites') {
      items = state.favorites.filter(c => !state.query || (c.label || '').toLowerCase().includes(state.query.toLowerCase()));
    } else {
      const q = new URLSearchParams({limit: String(state.pageSize + 1), offset: String(state.page * state.pageSize)});
      if (state.lang) q.set('lang', state.lang);
      if (state.category) q.set('category', state.category);
      if (state.query) q.set('q', state.query);
      if (state.provider) q.set('provider', state.provider);
      const endpoint = {vod: '/vod?', series: '/series?'}[state.mode] || '/channels?';
      items = await api(endpoint + q, {signal: controller.signal});
    }
    if (serial !== state.request) return;
    const hasMore = state.mode !== 'favorites' && items.length > state.pageSize;
    if (hasMore) items.pop();
    if (state.mode === 'live' || state.mode === 'favorites') {
      state.channelList = more ? state.channelList.concat(items) : [...items];
    } else {
      state.channelList = [];
    }
    if (!more) $('#channels').replaceChildren();
    const fragment = document.createDocumentFragment();
    const kind = {vod: 'movie', series: 'series'}[state.mode] || 'channel';
    const first = $('#channels').children.length;
    items.forEach((c, i) => fragment.append(row(c, kind, first + i + 1)));
    $('#channels').append(fragment);
    if (kind === 'channel') loadNowNext(items).catch(() => {});
    $('#load-more').hidden = !hasMore;
    $('#empty').hidden = $('#channels').children.length > 0;
    const totalRendered = $('#channels').children.length;
    const noun = {vod: ' films affichés', series: ' séries affichées'}[state.mode] || ' chaînes affichées';
    $('#count').textContent = totalRendered + noun + (hasMore ? ' · suite disponible' : '');
    markNowPlaying(state.current);
    if (!state.current) state.selectedChannelIndex = -1;
  } catch (err) {
    if (serial === state.request) {
      if (more) state.page = Math.max(0, state.page - 1);
      const retry = el('button', 'quiet', 'Réessayer');
      retry.onclick = () => renderChannels(more);
      const line = el('li', 'channel-card');
      const messageSpan = el('span', 'muted small', 'Impossible de charger le catalogue.');
      const actions = el('span', 'job-actions');
      actions.style.display = 'flex';
      actions.style.gap = '10px';
      actions.append(retry);
      const meta = el('span', 'meta');
      meta.append(messageSpan, actions);
      line.append(meta);
      $('#channels').replaceChildren(line);
      failure(err);
    }
  } finally {
    if (serial === state.request) {
      $('#channels').classList.remove('loading');
      $('#load-more').disabled = false;
    }
  }
}

$('#load-more').onclick = () => { state.page++; renderChannels(true); };
// En colonne defilante, la suite se charge en approchant du bas : pas de bouton a viser.
$('#channels').addEventListener('scroll', e => {
  const list = e.currentTarget, more = $('#load-more');
  if (more.hidden || more.disabled) return;
  if (list.scrollTop + list.clientHeight > list.scrollHeight - 600) more.click();
}, {passive: true});

let filterGeneration = 0;
async function loadFilters() {
  await loadProviders();
  const langs = await api('/languages' + (state.provider ? '?provider=' + encodeURIComponent(state.provider) : '')).catch(() => []);
  const sel = $('#lang');
  sel.replaceChildren(new Option('Toutes les langues', ''));
  langs.sort((x, y) => alphaSort(x.lang, y.lang)).forEach(l => sel.add(new Option(l.lang, l.lang)));
  sel.value = state.lang;
  if (sel.selectedIndex < 0) { state.lang = ''; sel.value = ''; }
  await loadCategories();
}

const alphaSort = (a, b) => String(a || '').trim().localeCompare(String(b || '').trim(), 'fr', {sensitivity: 'base', numeric: true});

function normalizeCategoryValue(value) {
  if (!value) return '';
  return typeof value === 'string' ? value : (value.name || value.slug || '');
}

let categoryValues = [];
let categoryFilter = '';

function renderCategoryChips(items = []) {
  const chips = $('#category-chips');
  if (!chips) return;

  categoryValues = [...new Set(items.map(normalizeCategoryValue).filter(Boolean))];
  const term = categoryFilter.trim().toLowerCase();
  const visible = term ? categoryValues.filter(v => v.toLowerCase().includes(term)) : categoryValues;
  chips.replaceChildren();

  const makeChip = (value, label) => {
    const chip = el('button', 'category-chip', label);
    chip.type = 'button';
    chip.setAttribute('role', 'tab');
    chip.setAttribute('aria-selected', String(state.category === value));
    chip.classList.toggle('active', state.category === value);
    chip.onclick = () => {
      state.category = value;
      const sel = $('#category');
      if (sel) sel.value = value;
      renderCategoryChips(categoryValues);
      renderChannels();
    };
    return chip;
  };

  chips.append(makeChip('', 'Toutes les catégories'));
  // La categorie active reste visible meme si le filtre texte l'exclut,
  // sinon on ne voit plus ce qui est applique.
  if (state.category && !visible.includes(state.category) && categoryValues.includes(state.category)) {
    chips.append(makeChip(state.category, state.category));
  }
  visible.forEach(value => chips.append(makeChip(value, value)));

  const emptyMsg = $('#category-empty');
  if (emptyMsg) emptyMsg.hidden = visible.length > 0 || !term;
  const searchInput = $('#category-search');
  if (searchInput) {
    searchInput.placeholder = categoryValues.length
      ? 'Filtrer ' + categoryValues.length + ' catégories…'
      : 'Filtrer les catégories…';
  }
}

const categorySearch = $('#category-search');
const categorySearchClear = $('#category-search-clear');
if (categorySearch) {
  let categorySearchTimer;
  categorySearch.oninput = e => {
    if (categorySearchClear) categorySearchClear.hidden = !e.target.value;
    clearTimeout(categorySearchTimer);
    categorySearchTimer = setTimeout(() => {
      categoryFilter = e.target.value;
      renderCategoryChips(categoryValues);
    }, 120);
  };
}
if (categorySearchClear) {
  categorySearchClear.onclick = () => {
    categorySearch.value = '';
    categoryFilter = '';
    categorySearchClear.hidden = true;
    renderCategoryChips(categoryValues);
    categorySearch.focus();
  };
}

async function loadCategories() {
  const serial = ++filterGeneration;
  const params = new URLSearchParams();
  if (state.lang) params.set('lang', state.lang);
  if (state.provider) params.set('provider', state.provider);
  const suffix = params.toString() ? '?' + params : '';
  const catsPath = {vod: '/vod/categories', series: '/series/categories'}[state.mode] || '/categories';
  const cats = await api(catsPath + suffix).catch(() => []);
  if (serial !== filterGeneration) return;
  const sel = $('#category');
  const rawValues = cats.map(normalizeCategoryValue).filter(Boolean);
  const values = [...new Set(rawValues)].sort(alphaSort);
  const selected = normalizeCategoryValue(state.category);

  sel.replaceChildren(new Option('Toutes les catégories', ''));
  values.forEach(v => sel.add(new Option(v, v)));
  if (values.includes(selected)) {
    state.category = selected;
    sel.value = selected;
  } else {
    state.category = '';
    sel.value = '';
  }
  renderCategoryChips(values);
}

async function loadProviders() {
  const st = await api('/status').catch(() => ({providers: []}));
  const sel = $('#provider');
  if (!sel) return;
  sel.replaceChildren(new Option('Tous les abonnements', ''));
  (st.providers || []).forEach(p => sel.add(new Option(p.name || p.id, p.id)));
  sel.value = state.provider;
  if (sel.selectedIndex < 0) { state.provider = ''; sel.value = ''; store.set('provider', ''); }
}

$('#provider').onchange = async e => {
  state.provider = e.target.value;
  state.lang = '';
  state.category = '';
  store.set('provider', state.provider);
  await loadFilters();
  await renderChannels();
};

$('#lang').onchange = async e => {
  state.lang = e.target.value;
  state.category = '';
  store.set('lang', state.lang);
  await loadCategories();
  await renderChannels();
};

$('#category').onchange = e => {
  state.category = e.target.value;
  renderCategoryChips($('#category option').length ? Array.from($('#category').options).slice(1).map(opt => opt.value) : []);
  renderChannels();
};

// Search & Clear button
const searchInput = $('#search');
const clearSearchBtn = $('#search-clear');
function updateClearSearch() {
  if (clearSearchBtn) clearSearchBtn.hidden = !searchInput.value;
}
if (clearSearchBtn) {
  clearSearchBtn.onclick = () => {
    searchInput.value = '';
    state.query = '';
    updateClearSearch();
    renderChannels();
    searchInput.focus();
  };
}

let searchTimer;
searchInput.oninput = e => {
  updateClearSearch();
  clearTimeout(searchTimer);
  const value = e.target.value;
  searchTimer = setTimeout(() => {
    state.query = value.trim();
    renderChannels();
  }, 250);
};

// route : false quand le changement vient deja de l'adresse (retour arriere,
// premier chargement) ; sinon l'onglet choisi s'inscrit dans l'historique.
async function setMode(mode, {route = true} = {}) {
  if (!ROUTE_OF[mode]) mode = 'home';
  if (route) setRoute(routeHash(mode));
  if (state.configOpen) {
    // Quitter les reglages par un onglet, sans « Fermer » : meme remise en
    // etat, sinon la page restait en mode reglages et le lecteur masque.
    state.configOpen = false;
    state.configReturnMode = null;
    document.body.classList.remove('view-config');
    clearInterval(state.configTimer);
    state.configTimer = null;
    restoreSettingsSnapshot();
  }
  $('#tab-conf').classList.remove('active');
  state.mode = mode;
  state.category = '';
  state.query = '';
  $('#search').value = '';
  updateClearSearch();
  categoryFilter = '';
  if (categorySearch) categorySearch.value = '';
  if (categorySearchClear) categorySearchClear.hidden = true;
  $('#config').hidden = true;
  $('#home').hidden = mode !== 'home';
  $('#preparations').hidden = mode !== 'prepared';
  $('#catalogue').hidden = mode === 'prepared' || mode === 'home';
  $('#filters').hidden = mode === 'prepared' || mode === 'home';
  $('#recent-wrap').hidden = mode !== 'live' || !recentChannels().length;
  if (mode === 'live') renderRecents();
  
  const modeClass = 'mode-' + (mode === 'favorites' ? 'favorites' : mode);
  document.body.classList.remove('mode-home', 'mode-live', 'mode-vod', 'mode-series', 'mode-favorites', 'mode-prepared');
  document.body.classList.add(modeClass);

  ['home', 'live', 'vod', 'series', 'fav', 'prepared'].forEach(k => {
    $('#tab-' + k).classList.toggle('active', (k === 'fav' ? 'favorites' : k) === mode);
  });
  $('#catalogue-title').textContent = {live: 'Direct', vod: 'Films', series: 'Séries', favorites: 'Ma liste'}[mode] || '';
  document.body.classList.toggle('scrolled', mode === 'home' && $('#home').scrollTop > 40);
  $('#search').placeholder = {vod: 'Rechercher un film…', series: 'Rechercher une série…'}[mode]
    || 'Rechercher une chaîne…';
  
  clearInterval(state.jobTimer);
  if (mode === 'home') {
    await renderHome();
  } else if (mode === 'prepared') {
    await refreshJobs();
    state.jobTimer = setInterval(() => refreshJobs().catch(failure), 5000);
  } else {
    await loadCategories();
    await renderChannels();
  }
}

function restoreSettingsSnapshot() {
  if (!state.configSnapshot) return;
  state.configSnapshot.forEach(({element, hidden}) => { if (element) element.hidden = hidden; });
  state.configSnapshot = null;
}

async function openSettings() {
  if (state.configOpen) return;
  state.configOpen = true;
  state.configReturnMode = state.mode;
  // Rendre un instantane laisse par une ouverture precedente AVANT d'en
  // prendre un nouveau : dans l'autre sens on memorise des elements deja
  // masques, et on efface l'instantane qu'on vient tout juste de constituer.
  restoreSettingsSnapshot();
  document.body.classList.add('view-config');
  $$('.topbar .tab, #account-menu button').forEach(t => t.classList.toggle('active', t.id === 'tab-conf'));
  state.configSnapshot = ['#player-wrap', '#preferences', '#recent-wrap', '#catalogue', '#preparations', '#filters', '#idle-hero', '#home']
    .map(id => {
      const element = $(id);
      if (!element) return null;
      return {element, hidden: element.hidden};
    }).filter(Boolean);
  state.configSnapshot.forEach(item => { item.element.hidden = true; });
  $('#config').hidden = false;
  clearInterval(state.configTimer);
  await refreshConfig();
  // Hors du rafraichissement de 8 s : il remettrait a zero le choix en cours.
  loadPlayerAccess().catch(failure);
  loadDevices().catch(failure);
  $('#config').scrollIntoView({behavior: 'smooth', block: 'start'});
  state.configTimer = setInterval(() => refreshConfig().catch(failure), 8000);
}

async function closeSettings() {
  if (!state.configOpen) return;
  state.configOpen = false;
  document.body.classList.remove('view-config');
  $('#config').hidden = true;
  clearInterval(state.configTimer);
  state.configTimer = null;
  const returnMode = state.configReturnMode || state.mode;
  state.configReturnMode = null;
  restoreSettingsSnapshot();
  await setMode(returnMode);
}

$('#tab-home').onclick = () => setMode('home').catch(failure);
$('#tab-live').onclick = () => setMode('live').catch(failure);
$('#tab-vod').onclick = () => setMode('vod').catch(failure);
$('#tab-series').onclick = () => setMode('series').catch(failure);
$('#tab-fav').onclick = () => setMode('favorites').catch(failure);
$('#tab-prepared').onclick = () => setMode('prepared').catch(failure);

function recentChannels() {
  return state.history.items.filter(i => i.kind === 'live').map(channelOf);
}

function renderRecents() {
  const list = recentChannels().slice(0, 6);
  $('#recent-wrap').hidden = !list.length || state.mode !== 'live';
  $('#recents').replaceChildren();
  list.forEach(c => {
    const b = el('button', '', c.label);
    b.onclick = () => play(c).catch(failure);
    $('#recents').append(b);
  });
}

// ====================================================================
// Adresse de la page : onglet, et fiche ouverte. Actualiser ramene au meme
// endroit ; le bouton retour du navigateur change d'onglet.
// ====================================================================
const ROUTES = {accueil: 'home', direct: 'live', films: 'vod', series: 'series', favoris: 'favorites', prets: 'prepared'};
const ROUTE_OF = Object.fromEntries(Object.entries(ROUTES).map(([name, mode]) => [mode, name]));

function routeHash(mode, provider, id) {
  const base = '#/' + (ROUTE_OF[mode] || 'accueil');
  return provider && id !== undefined && id !== '' ? base + '/' + encodeURIComponent(provider) + '/' + encodeURIComponent(id) : base;
}

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').map(part => {
    try { return decodeURIComponent(part); } catch (_) { return ''; }
  });
  return {mode: ROUTES[parts[0]] || 'home', provider: parts[1] || '', id: parts[2] || ''};
}

function setRoute(hash, replace = false) {
  if (location.hash === hash) return;
  window.history[replace ? 'replaceState' : 'pushState'](null, '', hash);
}

function openRouteDialog(route) {
  if (!route.id) return;
  if (route.mode === 'vod' && !$('#movie-dialog').open) {
    movieDialog({provider_id: route.provider, stream_id: route.id, title: ''}).catch(failure);
  } else if (route.mode === 'series' && !$('#series-dialog').open) {
    seriesDialog({provider_id: route.provider, series_id: route.id, title: ''}).catch(failure);
  }
}

window.addEventListener('popstate', () => {
  if ($('#app').hidden) return;
  const route = parseRoute();
  const change = route.mode !== state.mode || state.configOpen ? setMode(route.mode, {route: false}) : Promise.resolve();
  change.then(() => {
    if (!route.id) {
      if ($('#movie-dialog').open) $('#movie-dialog').close();
      if ($('#series-dialog').open) $('#series-dialog').close();
    }
    openRouteDialog(route);
  }).catch(failure);
});

// Fermer une fiche rend l'adresse de l'onglet, sans nouvelle entree.
['#movie-dialog', '#series-dialog'].forEach(id => $(id).addEventListener('close', () => {
  if (parseRoute().id) setRoute(routeHash(state.mode), true);
}));

// ====================================================================
// Historique : ce que le compte a regarde, garde par le serveur pour que
// tous les appareils le partagent.
// ====================================================================
// maxAge : reutiliser un historique charge il y a moins de maxAge ms.
async function loadHistory(maxAge = 0) {
  if (maxAge && state.historyAt && Date.now() - state.historyAt < maxAge) return state.history;
  const data = await api('/history', {skipAuthRedirect: true}).catch(() => null);
  if (data && Array.isArray(data.items)) {
    state.history = {items: data.items, next: data.next || {}};
    state.historyAt = Date.now();
  }
  return state.history;
}

// « The Winter King S01E04 — S01E04 » : certains panels donnent comme titre
// d'episode son numero ; les preparations d'avant la correction le gardent.
function cleanTitle(title) {
  return String(title || '').replace(/\b(S\d+\s*E\d+)\s*[—–-]\s*\1\s*$/i, '$1');
}

// Confirmation dans le style de l'app, plutot que la boite du navigateur.
function askConfirm({title, text = '', ok = 'Confirmer', danger = false}) {
  const box = $('#confirm-dialog'), okButton = $('#confirm-ok'), cancel = $('#confirm-cancel');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  okButton.textContent = ok;
  okButton.className = danger ? 'btn-danger' : 'btn-play';
  return new Promise(resolve => {
    const done = value => { box.close(); resolve(value); };
    okButton.onclick = () => done(true);
    cancel.onclick = () => done(false);
    box.oncancel = e => { e.preventDefault(); done(false); };
    box.onclick = e => { if (e.target === box) done(false); };
    box.showModal();
    // Action destructive : le focus va sur « Annuler », pas sur le danger.
    (danger ? cancel : okButton).focus();
  });
}

function watchOfJob(job) {
  return {kind: job.kind || 'movie', provider: job.provider, id: job.movie, height: job.height,
          audio: job.audio === undefined ? null : job.audio, subtitle: job.subtitle === undefined ? null : job.subtitle};
}

function watchOf(item) {
  const d = item.data || {};
  return {kind: item.kind, provider: d.provider_id, id: d.stream_id, height: d.height || 480,
          audio: d.audio === undefined ? null : d.audio, subtitle: d.subtitle === undefined ? null : d.subtitle};
}

function channelOf(item) {
  return {...(item.data || {}), icon: item.icon || undefined};
}

function historyItem(watch) {
  const ref = watch.provider + ':' + watch.id;
  return state.history.items.find(i => i.kind === watch.kind && i.ref === ref);
}

function jobDuration(job) {
  return job && job.duration > 0 && !job.open ? job.duration : 0;
}

function jobPosition(job) {
  const item = historyItem(watchOfJob(job));
  if (item) return item.finished ? 0 : item.position;
  // Positions gardees par ce navigateur avant l'historique du compte.
  return Number(store.get('position_' + job.id)) || 0;
}

function progressBody(finished = false) {
  const watch = state.watch, job = state.job;
  if (!watch || !job) return null;
  const position = video.currentTime || 0;
  // Pendant la preparation, la duree de la video peut n'etre que la partie
  // deja encodee : seule celle du serveur fait foi.
  const duration = jobDuration(job) || (job.state === 'ready' && Number.isFinite(video.duration) ? video.duration : 0);
  return {...watch, position, duration, finished};
}

function notePlayingPosition(position) {
  const playing = tabStore.get('playing');
  if (playing && playing.kind === 'watch') tabStore.set('playing', {...playing, position});
}

// Toutes les 15 s pendant la lecture, et a chaque pause, arret ou fin.
function saveProgress(force = false, finished = false) {
  const body = progressBody(finished);
  if (!body || (body.position < 5 && !finished)) return Promise.resolve();
  const now = Date.now();
  if (!force && now - state.savedAt < 15000) return Promise.resolve();
  state.savedAt = now;
  notePlayingPosition(body.position);
  return post('/history', body, {skipAuthRedirect: true}).then(saved => {
    const item = historyItem(body);
    if (item) Object.assign(item, {position: body.position, finished: saved.finished, updated: Math.floor(now / 1000)});
    else loadHistory().catch(() => {});
  }).catch(() => {});
}

// Une chaine compte comme regardee apres dix secondes d'image : zapper ne
// remplit pas la liste.
function rememberWhenWatched(channel, attempt) {
  clearTimeout(state.liveTimer);
  let waited = 0;
  const check = () => {
    if (attempt !== state.playback) return;
    if (!state.frames && (waited += 5000) < 60000) { state.liveTimer = setTimeout(check, 5000); return; }
    if (!state.frames) return;
    recordChannel(channel).catch(() => {});
  };
  state.liveTimer = setTimeout(check, 10000);
}

async function recordChannel(channel) {
  await post('/history', {kind: 'live', lang: channel.lang, canonical: channel.canonical, label: channel.label,
    icon: channel.icon, epg_id: channel.epg_id, category: channel.category}, {skipAuthRedirect: true});
  await loadHistory();
  renderRecents();
}

// Les chaines recentes etaient gardees par le navigateur : on les confie au
// compte une fois, puis on oublie la copie locale.
async function migrateRecents() {
  const old = store.json('recents', []);
  if (!old.length) return;
  for (const channel of old.slice().reverse()) {
    if (channel && channel.canonical) {
      await post('/history', {kind: 'live', lang: channel.lang, canonical: channel.canonical, label: channel.label,
        icon: channel.icon, epg_id: channel.epg_id, category: channel.category}, {skipAuthRedirect: true}).catch(() => {});
    }
  }
  store.remove('recents');
}

async function forget(item) {
  await post('/history/delete', {grp: item.grp});
  await loadHistory();
  if (state.mode === 'home') await renderHome();
}

// Relance d'un film ou d'un episode : la meme preparation si elle existe
// encore, sinon une nouvelle, qui repart directement de la position.
async function watchAgain(watch, start) {
  hideResume();
  const job = await post('/prepare', {kind: watch.kind, provider: watch.provider, id: watch.id,
    height: watch.height || 480, audio: watch.audio, subtitle: watch.subtitle});
  await watchWhenPlayable(job, start);
}

function watchFailed(err) {
  stageScreen(null);
  $('#player-status').textContent = err.message;
  failure(err);
}

function resumeWatch(item, fromStart = false) {
  const start = fromStart || item.finished ? 0 : item.position;
  return watchAgain(watchOf(item), start).catch(watchFailed);
}

function playNext(next) {
  return watchAgain({kind: 'episode', provider: next.provider_id, id: next.episode_id, height: next.height || 480,
    audio: null, subtitle: null}, 0).catch(watchFailed);
}

// ====================================================================
// Accueil
// ====================================================================
const pad2 = n => String(n).padStart(2, '0');
function episodeCode(season, episode) {
  return (season ? 'S' + pad2(season) : '') + (episode ? 'E' + pad2(episode) : '');
}

function humanDuration(seconds) {
  const total = Math.max(60, Math.round(seconds / 60) * 60);
  const h = Math.floor(total / 3600), m = Math.round((total % 3600) / 60);
  return h ? h + ' h ' + pad2(m) : m + ' min';
}

function progressLine(item) {
  const left = item.duration - item.position;
  if (item.duration > 0 && left > 0) return 'Reste ' + humanDuration(left);
  return humanDuration(item.position) + ' regardées';
}

function itemLabel(item) {
  if (item.kind !== 'episode') return item.title;
  const d = item.data || {};
  return item.title + ' · ' + episodeCode(d.season, d.episode);
}

// Titre d'episode seulement s'il dit autre chose que son numero.
function episodeTitle(title, code) {
  const raw = String(title || '').trim();
  if (!raw || raw.toUpperCase() === code || /^S\s*\d+\s*E\s*\d+$/i.test(raw) || /^(episode|épisode)\s*\d+$/i.test(raw)) return '';
  return raw;
}

// Illustration : l'image du panel quand elle existe, sinon un degrade tire
// du titre et le titre en lettres d'affiche. Jamais de case vide.
const ART_PALETTES = [
  ['#2a1850', '#8b5cf6', '#120a24'], ['#12203a', '#3e7bd6', '#070d18'], ['#3a2410', '#e39b3a', '#170d05'],
  ['#3b1630', '#e0567d', '#1a0a16'], ['#321212', '#d74a3c', '#140606'], ['#1b2440', '#6f8fd8', '#0e1326'],
  ['#2a1433', '#b45fd6', '#10071a'], ['#3a2a10', '#f0b04a', '#170f05'], ['#112a3a', '#3fa7d6', '#06121a'],
  ['#2e1a2a', '#c86b9a', '#120a10'], ['#26262e', '#b8b8c8', '#101014'], ['#301c10', '#d9763a', '#140a05']
];

function hashOf(text) {
  let h = 0;
  for (const ch of String(text || '')) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(h);
}

function paint(node, title) {
  const [c1, c2, c3] = ART_PALETTES[hashOf(title) % ART_PALETTES.length];
  node.style.setProperty('--c1', c1);
  node.style.setProperty('--c2', c2);
  node.style.setProperty('--c3', c3);
  return node;
}

function imageFor(url, onReady) {
  if (!url || !/^https?:\/\//.test(url)) return null;
  const img = el('img');
  img.src = '/api/logo?u=' + encodeURIComponent(url);
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';
  img.onload = () => { img.classList.add('ready'); if (onReady) onReady(); };
  img.onerror = () => img.remove();
  return img;
}

function artwork(url, title, className = '') {
  const art = paint(el('span', 'art' + (className ? ' ' + className : '')), title);
  art.append(el('span', 'art-title', title || ''));
  const img = imageFor(url, () => art.classList.add('has-img'));
  if (img) art.append(img);
  return art;
}

function bar(fraction) {
  const track = el('span', 'bar');
  const fill = el('span');
  fill.style.width = Math.max(0, Math.min(1, fraction || 0)) * 100 + '%';
  track.append(fill);
  return track;
}

// « S3 · É3 » : lisible d'un coup d'oeil, comme sur les applis de streaming.
function seasonEpisode(season, episode) {
  return [season ? 'S' + season : '', episode ? 'É' + episode : ''].filter(Boolean).join(' · ');
}

// Menu « ⋯ » d'une carte : un seul a la fois, place sous le bouton.
let cardMenu = null;
function closeCardMenu() {
  if (cardMenu) { cardMenu.remove(); cardMenu = null; }
}
function openCardMenu(anchor, actions) {
  closeCardMenu();
  const menu = el('div', 'card-menu');
  menu.setAttribute('role', 'menu');
  actions.forEach(({label, run, danger}) => {
    const b = el('button', danger ? 'danger' : '', label);
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.onclick = () => { closeCardMenu(); Promise.resolve().then(run).catch(failure); };
    menu.append(b);
  });
  document.body.append(menu);
  const r = anchor.getBoundingClientRect(), w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + 'px';
  menu.style.top = (r.bottom + h + 8 > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + 'px';
  cardMenu = menu;
  menu.querySelector('button').focus({preventScroll: true});
}
document.addEventListener('click', e => { if (cardMenu && !e.target.closest('.card-menu')) closeCardMenu(); });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !cardMenu) return;
  e.stopPropagation();
  closeCardMenu();
});
document.addEventListener('scroll', closeCardMenu, {capture: true, passive: true});

function moreButton(label, actions) {
  const b = el('button', 'more', '⋯');
  b.type = 'button';
  b.setAttribute('aria-label', 'Options : ' + label);
  b.setAttribute('aria-haspopup', 'menu');
  b.onclick = e => { e.stopPropagation(); openCardMenu(b, actions); };
  return b;
}

function cardLine(title, sub, more) {
  const line = el('div', 'card-line');
  const text = el('div', 'card-text');
  text.append(el('div', 'card-title', title || ''), el('div', 'card-sub', sub || ''));
  line.append(text);
  if (more) line.append(more);
  return line;
}

function cardHit(label, visual, onPlay) {
  const b = el('button', 'card-hit');
  b.type = 'button';
  b.dataset.navItem = '';
  b.setAttribute('aria-label', label);
  b.append(visual);
  b.onclick = () => Promise.resolve().then(onPlay).catch(failure);
  return b;
}

// Continuer a regarder : format paysage, progression au pied de l'image.
function wideCard(item) {
  const d = item.data || {};
  const art = artwork(item.icon, item.title);
  art.append(bar(item.duration > 0 ? item.position / item.duration : 0.05));
  const sub = [item.kind === 'episode' ? seasonEpisode(d.season, d.episode) : '', progressLine(item)].filter(Boolean).join(' · ');
  const card = el('article', 'card wide');
  card.append(
    cardHit('Reprendre ' + itemLabel(item), art, () => resumeWatch(item)),
    cardLine(item.title, sub, moreButton(item.title, [
      {label: 'Reprendre depuis le début', run: () => resumeWatch(item, true)},
      {label: 'Retirer de la liste', run: () => forget(item), danger: true}
    ])));
  return card;
}

function posterCard({title, sub, icon, badge, label, onPlay, item, actions}) {
  const art = artwork(icon, title);
  if (badge) art.append(el('span', 'badge', badge));
  if (item && item.duration > 0) art.append(bar(item.position / item.duration));
  const card = el('article', 'card poster');
  card.append(cardHit(label || title, art, onPlay), cardLine(title, sub, actions ? moreButton(title, actions) : null));
  return card;
}

// Chaine : vignette 16:9, logo au centre, programme en cours dessous.
function channelTile(c, actions) {
  const tile = paint(el('div', 'tile'), c.label);
  tile.append(el('span', 'live-dot', 'DIRECT'), el('span', 'brand', c.label || c.canonical));
  const img = imageFor(c.icon, () => tile.classList.add('has-img'));
  if (img) tile.append(img);
  const progress = bar(0);
  progress.hidden = true;
  tile.append(progress);
  const card = el('article', 'card channel');
  if (c.epg_id) card.dataset.epgId = c.epg_id;
  card.dataset.label = c.label || '';
  const hit = cardHit('Regarder ' + (c.label || ''), tile, () => play(c));
  hit.ondblclick = () => fullscreenWhenReady();
  card.append(hit, cardLine(c.label, [c.category, c.lang].filter(Boolean).join(' · ') || 'Direct',
    actions ? moreButton(c.label, actions) : null));
  return card;
}

function homeRow(title, cards, more) {
  const section = el('section', 'home-row');
  const head = el('div', 'row-head');
  head.append(el('h2', '', title));
  if (more) {
    const link = el('button', 'row-more', more.label);
    link.type = 'button';
    link.onclick = () => setMode(more.mode).catch(failure);
    head.append(link);
  }
  const list = el('div', 'rail');
  list.dataset.navRow = '';
  cards.forEach(card => list.append(card));
  section.append(head, list);
  return section;
}

// Ce que le compte a en cours, dans l'ordre de l'historique (le plus recent
// d'abord) : un seul episode par serie, le dernier regarde.
function homeSections(data) {
  const watching = [], upNext = [], seen = new Set();
  let hero = null;
  for (const item of data.items) {
    let entry = null;
    if (item.kind === 'live') {
      entry = {type: 'live', item};
    } else if (item.kind === 'movie') {
      if (!item.finished && item.position >= 120) { entry = {type: 'watch', item}; watching.push(entry); }
    } else if (item.kind === 'episode' && !seen.has(item.grp)) {
      seen.add(item.grp);
      const next = data.next[item.grp];
      if (!item.finished && item.position >= 120) { entry = {type: 'watch', item}; watching.push(entry); }
      else if (item.finished && next) { entry = {type: 'next', item, next}; upNext.push(entry); }
    }
    if (entry && !hero) hero = entry;
  }
  const channels = data.items.filter(i => i.kind === 'live');
  return {hero, watching, upNext, channels};
}

function metaLine(parts) {
  const line = el('div', 'billboard-meta');
  parts.filter(Boolean).forEach((part, i) => {
    if (i) line.append(el('span', 'dot'));
    line.append(typeof part === 'string' ? el('span', '', part) : part);
  });
  return line;
}

async function heroProgram(channel, slot) {
  if (!channel.epg_id) return;
  const guide = await api('/guide/now?ids=' + encodeURIComponent(channel.epg_id), {skipAuthRedirect: true}).catch(() => ({}));
  const entry = guide[channel.epg_id];
  const program = entry && (entry.now || entry.next);
  if (!program) return;
  slot.textContent = (entry.now ? 'En ce moment : ' : 'À ' + clock(program.start) + ' : ') + program.title;
}

function openSeriesOf(next, item) {
  return seriesDialog({provider_id: next.provider_id, series_id: next.series_id, title: item.title, icon: item.icon});
}

// Grande affiche : la derniere chose regardee, a reprendre en un geste.
function renderHero(hero) {
  const box = $('#home-hero');
  box.replaceChildren();
  box.hidden = !hero;
  if (!hero) return;
  const {type, item, next} = hero;
  const d = item.data || {};
  const scene = paint(el('div', 'billboard-scene'), item.title);
  scene.setAttribute('aria-hidden', 'true');
  const backdrop = imageFor(item.icon);
  if (backdrop) scene.append(backdrop);
  const body = el('div', 'billboard-body');
  const kicker = el('p', 'kicker');
  const actions = el('div', 'billboard-actions');
  actions.dataset.navRow = '';
  const action = (label, cls, fn, aria) => {
    const b = el('button', cls, label);
    b.type = 'button';
    b.dataset.navItem = '';
    if (aria) b.setAttribute('aria-label', aria);
    b.onclick = () => Promise.resolve().then(fn).catch(failure);
    actions.append(b);
    return b;
  };
  let title = item.title, meta, poster, progress = null, menu;
  if (type === 'live') {
    const channel = channelOf(item);
    kicker.append(el('b', '', 'Reprendre'), document.createTextNode(' · Direct'));
    title = channel.label;
    const now = el('span', '', '');
    meta = metaLine([channel.category, channel.lang, now]);
    heroProgram(channel, now).catch(() => {});
    poster = paint(el('div', 'billboard-poster art logo'), channel.label);
    poster.append(el('span', 'art-title', channel.label));
    const logo = imageFor(item.icon, () => poster.classList.add('has-img'));
    if (logo) poster.append(logo);
    action('▶ Regarder', 'btn-play', () => play(channel));
    menu = [{label: 'Retirer des récents', run: () => forget(item), danger: true}];
  } else if (type === 'next') {
    const code = seasonEpisode(next.season, next.episode);
    kicker.append(el('b', '', 'À suivre'), document.createTextNode(' · Série'));
    meta = metaLine([code, episodeTitle(next.title, episodeCode(next.season, next.episode)), next.duration ? readableDuration(next.duration) : '']);
    poster = artwork(item.icon, item.title, 'billboard-poster');
    action('▶ Lancer ' + (code.replace(' · ', ' ') || 'l’épisode'), 'btn-play', () => playNext(next));
    action('Épisodes', 'btn-ghost', () => openSeriesOf(next, item));
    menu = [{label: 'Retirer de l’accueil', run: () => forget(item), danger: true}];
  } else {
    kicker.append(el('b', '', 'Reprendre'), document.createTextNode(item.kind === 'episode' ? ' · Série' : ' · Film'));
    meta = metaLine([item.kind === 'episode' ? seasonEpisode(d.season, d.episode) : '',
      item.kind === 'episode' ? d.episode_title && episodeTitle(d.episode_title, episodeCode(d.season, d.episode)) : '',
      item.duration ? humanDuration(item.duration) : '', d.height ? el('span', 'pill', d.height + 'p') : '']);
    progress = el('div', 'billboard-progress');
    progress.append(bar(item.duration > 0 ? item.position / item.duration : 0.05), document.createTextNode(progressLine(item)));
    poster = artwork(item.icon, item.title, 'billboard-poster');
    action('▶ Reprendre', 'btn-play', () => resumeWatch(item));
    action('Depuis le début', 'btn-ghost', () => resumeWatch(item, true));
    menu = [{label: 'Retirer de l’accueil', run: () => forget(item), danger: true}];
  }
  const more = action('⋯', 'btn-ghost btn-round', () => {}, 'Plus d’options');
  more.onclick = e => { e.stopPropagation(); openCardMenu(more, menu); };
  body.append(kicker, el('h1', '', title), meta);
  if (progress) body.append(progress);
  body.append(actions);
  box.append(scene, poster, body);
}

async function renderHome() {
  const serial = ++state.homeRender;
  const hour = new Date().getHours();
  $('#home-greeting').textContent = hour >= 5 && hour < 18 ? 'Bonjour' : 'Bonsoir';
  const [data, jobs] = await Promise.all([loadHistory(4000), api('/preparations', {skipAuthRedirect: true}).catch(() => [])]);
  if (serial !== state.homeRender || state.mode !== 'home') return;
  const {hero, watching, upNext, channels} = homeSections(data);
  renderHero(hero);
  paint($('#home-empty .billboard-scene'), 'Streamly');
  $('#home-empty').hidden = !!hero;
  const rows = [];

  const others = watching.filter(e => e !== hero);
  if (others.length) rows.push(homeRow('Continuer à regarder', others.map(({item}) => wideCard(item))));

  const series = upNext.filter(e => e !== hero);
  if (series.length) {
    rows.push(homeRow('Séries en cours', series.map(({item, next}) => posterCard({
      title: item.title, icon: item.icon,
      sub: [seasonEpisode(next.season, next.episode), episodeTitle(next.title, episodeCode(next.season, next.episode))].filter(Boolean).join(' · '),
      badge: (next.episode ? 'É' + next.episode : 'Épisode') + ' à suivre',
      label: 'Lancer ' + item.title + ' ' + episodeCode(next.season, next.episode),
      onPlay: () => playNext(next),
      actions: [
        {label: 'Tous les épisodes', run: () => openSeriesOf(next, item)},
        {label: 'Retirer de la liste', run: () => forget(item), danger: true}
      ]
    }))));
  }

  const recent = channels.filter(i => !hero || hero.item !== i).slice(0, 14);
  if (recent.length) {
    rows.push(homeRow('En direct', recent.map(i => channelTile(channelOf(i),
      [{label: 'Retirer des récents', run: () => forget(i), danger: true}])), {label: 'Tout le direct', mode: 'live'}));
  }

  if (state.favorites.length) {
    rows.push(homeRow('Ma liste', state.favorites.slice(0, 14).map(c => channelTile(c, [{
      label: 'Retirer de ma liste', danger: true,
      run: async () => {
        await post('/favorites/delete', {lang: c.lang, canonical: c.canonical});
        state.favorites = await api('/favorites').catch(() => []);
        await renderHome();
      }
    }])), {label: 'Tout voir', mode: 'favorites'}));
  }

  const ready = jobs.filter(j => j.state === 'ready' || (j.state === 'preparing' && j.playable)).slice(0, 14);
  if (ready.length) {
    rows.push(homeRow('Prêts hors connexion', ready.map(job => {
      const item = historyItem(watchOfJob(job));
      return posterCard({
        title: cleanTitle(job.title), icon: item && item.icon,
        badge: job.state === 'ready' ? job.height + 'p · ' + size(job.size_bytes) : 'Préparation ' + job.progress + ' %',
        sub: item && !item.finished && item.position >= 120 ? progressLine(item) : (job.state === 'ready' ? 'Prêt' : 'Lisible pendant la préparation'),
        item: item && !item.finished && item.position >= 120 ? item : null,
        onPlay: () => playJob(job)
      });
    }), {label: 'Tout voir', mode: 'prepared'}));
  }
  $('#home-rows').replaceChildren(...rows);

  // Programme en cours sous chaque chaine, depuis le guide en cache.
  const tiles = [...$$('#home .card.channel[data-epg-id]')];
  const ids = [...new Set(tiles.map(t => t.dataset.epgId))];
  if (ids.length) {
    api('/guide/now?ids=' + encodeURIComponent(ids.join(',')), {skipAuthRedirect: true}).then(guide => {
      tiles.forEach(card => {
        const entry = guide[card.dataset.epgId];
        if (!entry || !entry.now) return;
        card.querySelector('.card-title').textContent = entry.now.title;
        card.querySelector('.card-sub').textContent = card.dataset.label + ' · ' + clock(entry.now.start);
        const progress = card.querySelector('.tile .bar');
        progress.firstChild.style.width = progressOf(entry.now) + '%';
        progress.hidden = false;
      });
    }).catch(() => {});
  }
}

// Menu du compte : Films prets, Reglages, Deconnexion.
function closeAccountMenu() {
  $('#account-menu').hidden = true;
  $('#account-btn').setAttribute('aria-expanded', 'false');
}
$('#account-btn').onclick = e => {
  e.stopPropagation();
  const open = $('#account-menu').hidden;
  $('#account-menu').hidden = !open;
  $('#account-btn').setAttribute('aria-expanded', String(open));
  if (open) $('#account-menu button:not([hidden])').focus();
};
// Les entrees du menu gardent leurs propres actions ; le menu se referme.
$('#account-menu').addEventListener('click', e => { if (e.target.closest('button')) closeAccountMenu(); });
document.addEventListener('click', e => { if (!$('#account-menu').hidden && !e.target.closest('.top-tools')) closeAccountMenu(); });

// La barre du haut est transparente sur l'affiche de l'accueil, opaque des
// qu'on fait defiler. Le defilement ne remonte pas : on l'ecoute en capture.
document.addEventListener('scroll', e => {
  const box = e.target === document ? document.scrollingElement : e.target;
  if (!box || (box !== document.scrollingElement && box.id !== 'home')) return;
  document.body.classList.toggle('scrolled', box.scrollTop > 40);
}, {capture: true, passive: true});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || $('#account-menu').hidden) return;
  // Sinon Echap fermerait aussi le lecteur.
  e.stopPropagation();
  closeAccountMenu();
  $('#account-btn').focus();
});

$$('#home-empty [data-goto]').forEach(b => { b.onclick = () => setMode(b.dataset.goto).catch(failure); });

// Fleches dans l'accueil : gauche/droite dans une rangee, haut/bas entre rangees.
function homeArrow(e) {
  const rows = [...$$('#home [data-nav-row]')].map(r => [...r.querySelectorAll('[data-nav-item]')]).filter(r => r.length);
  if (!rows.length) return false;
  let r = rows.findIndex(row => row.includes(document.activeElement));
  if (r < 0) { rows[0][0].focus(); return true; }
  let i = rows[r].indexOf(document.activeElement);
  if (e.key === 'ArrowRight') i = Math.min(rows[r].length - 1, i + 1);
  else if (e.key === 'ArrowLeft') i = Math.max(0, i - 1);
  else {
    r = e.key === 'ArrowDown' ? Math.min(rows.length - 1, r + 1) : Math.max(0, r - 1);
    i = Math.min(i, rows[r].length - 1);
  }
  rows[r][i].focus();
  rows[r][i].scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'nearest'});
  return true;
}

// ====================================================================
// Apres une actualisation en pleine lecture : proposer de reprendre, sans
// relancer d'office (son bloque par le navigateur, encodage inutile).
// ====================================================================
function hideResume() { $('#resume-bar').hidden = true; }

function offerResume() {
  const playing = tabStore.get('playing');
  if (!playing) return;
  if (playing.kind === 'live' && playing.channel) {
    $('#resume-title').textContent = playing.channel.label || '';
    $('#resume-detail').textContent = 'Direct';
    $('#resume-go').onclick = () => play(playing.channel).catch(failure);
  } else if (playing.kind === 'watch' && playing.watch) {
    const item = historyItem(playing.watch);
    $('#resume-title').textContent = item ? itemLabel(item) : 'Votre film';
    $('#resume-detail').textContent = playing.position > 5 ? 'à ' + clockPosition(playing.position) : '';
    $('#resume-go').onclick = () => { hideResume(); watchAgain(playing.watch, playing.position).catch(watchFailed); };
  } else {
    return;
  }
  $('#resume-bar').hidden = false;
  $('#resume-go').focus({preventScroll: true});
}

function clockPosition(seconds) {
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return (h ? h + ':' + pad2(m) : m) + ':' + pad2(s % 60);
}

$('#resume-close').onclick = () => { hideResume(); tabStore.remove('playing'); };

// Dialog films
// Bandeau d'une fiche : l'affiche floutee en fond, l'affiche nette devant.
function sheetArt(selector, icon, title) {
  $(selector).replaceChildren(artwork(icon, title), artwork(icon, title, 'sheet-poster'));
}

async function seriesDialog(show) {
  if (state.mode === 'series') setRoute(routeHash('series', show.provider_id, show.series_id), true);
  $('#series-title').textContent = show.title || show.name || '';
  sheetArt('#series-art', show.icon, show.title || show.name);
  $('#series-details').textContent = '';
  $('#series-plot').textContent = '';
  $('#series-message').textContent = 'Chargement des saisons…';
  $('#series-episodes').replaceChildren();
  $('#series-season-field').hidden = true;
  $('#series-dialog').showModal();
  let info;
  try {
    info = await api('/series/info?provider=' + encodeURIComponent(show.provider_id) + '&id=' + show.series_id);
  } catch (err) {
    $('#series-message').textContent = err.message;
    return;
  }
  const seasons = info.seasons || [];
  $('#series-title').textContent = info.title || show.title;
  if (info.icon !== show.icon || !show.title) sheetArt('#series-art', info.icon || show.icon, info.title || show.title);
  $('#series-plot').textContent = info.plot || '';
  $('#series-details').textContent = [
    seasons.length ? seasons.length + (seasons.length > 1 ? ' saisons' : ' saison') : '',
    info.episode_count ? info.episode_count + (info.episode_count > 1 ? ' épisodes' : ' épisode') : ''
  ].filter(Boolean).join(' · ');
  $('#series-message').textContent = '';
  if (!seasons.length) {
    $('#series-message').textContent = 'Aucun épisode annoncé pour cette série.';
    return;
  }

  const select = $('#series-season');
  select.replaceChildren();
  seasons.forEach(s => select.add(new Option(s.season ? 'Saison ' + s.season : 'Épisodes', String(s.season))));
  // Une serie a saison unique n'a pas besoin d'un selecteur a une entree.
  $('#series-season-field').hidden = seasons.length < 2;

  const showSeason = () => {
    const chosen = seasons.find(s => String(s.season) === select.value) || seasons[0];
    const list = $('#series-episodes');
    list.replaceChildren();
    chosen.episodes.forEach(ep => {
      const li = el('li');
      const button = el('button');
      button.type = 'button';
      const numero = (ep.season ? 'S' + String(ep.season).padStart(2, '0') : '')
        + (ep.episode ? 'E' + String(ep.episode).padStart(2, '0') : '');
      // Certains panels remplissent le titre avec « S01E01 », deja affiche
      // dans la colonne de gauche : on evite de le repeter.
      const raw = (ep.title || '').trim();
      const redundant = !raw || raw.toUpperCase() === numero
        || /^S\s*\d+\s*E\s*\d+$/i.test(raw) || /^(episode|épisode)\s*\d+$/i.test(raw);
      button.append(
        el('span', 'ep-num', numero || '—'),
        el('span', 'ep-title', redundant ? 'Épisode ' + (ep.episode || '?') : raw),
        el('span', 'ep-dur', readableDuration(ep.duration))
      );
      button.onclick = () => {
        $('#series-dialog').close();
        // On rejoint la fiche de preparation des films : meme choix de
        // qualite, memes pistes, meme file d'attente.
        episodeDialog(info, ep);
      };
      li.append(button);
      list.append(li);
    });
  };
  select.onchange = showSeason;
  showSeason();
}

function episodeDialog(show, episode) {
  const numero = (episode.season ? 'S' + String(episode.season).padStart(2, '0') : '')
    + (episode.episode ? 'E' + String(episode.episode).padStart(2, '0') : '');
  // Beaucoup de panels donnent comme titre d'épisode le numéro seul, ou le nom
  // de la série suivi du numéro : on ne le garde que s'il apporte autre chose.
  const brut = String(episode.title || '').trim();
  const reste = brut.replace(show.title || '', '').replace(/S\d+\s*E\d+/gi, '').replace(/[\s·:|\-\[\]()]+/g, '');
  const label = [show.title, numero, reste ? brut : ''].filter(Boolean).join(' · ');
  state.movie = {
    kind: 'episode',
    provider_id: show.provider_id,
    stream_id: episode.episode_id,
    title: label,
    duration: episode.duration || '',
    plot: episode.plot || show.plot || ''
  };
  movieWording('episode');
  sheetArt('#movie-art', show.icon, show.title);
  $('#movie-kicker').textContent = 'Épisode · ' + (show.title || '');
  $('#movie-title').textContent = reste ? brut : (show.title || label);
  $('#movie-details').textContent = [seasonEpisode(episode.season, episode.episode), readableDuration(episode.duration)].filter(Boolean).join(' · ');
  $('#movie-plot').textContent = state.movie.plot;
  $('#movie-message').textContent = '';
  $('#track-fields').hidden = true;
  movieButtons(true);
  movieEstimate();
  openMovieDialog();
}

async function movieDialog(c) {
  if (state.mode === 'vod') setRoute(routeHash('vod', c.provider_id, c.stream_id), true);
  $('#movie-title').textContent = c.title || '';
  $('#movie-kicker').textContent = 'Film';
  sheetArt('#movie-art', c.icon, c.title);
  $('#movie-message').textContent = 'Chargement des informations…';
  $('#movie-details').textContent = '';
  $('#movie-plot').textContent = '';
  $('#track-fields').hidden = true;
  movieButtons(false);
  movieWording('movie');
  openMovieDialog();
  try {
    const info = await api('/vod/info?provider=' + encodeURIComponent(c.provider_id) + '&id=' + c.stream_id);
    state.movie = {...info, kind: 'movie'};
    $('#movie-title').textContent = info.title;
    if (info.icon !== c.icon || !c.title) sheetArt('#movie-art', info.icon || c.icon, info.title);
    $('#movie-details').textContent = [readableDuration(info.duration), info.size_bytes ? 'Source : ≈ ' + size(info.size_bytes) : ''].filter(Boolean).join(' · ');
    $('#movie-plot').textContent = info.plot || '';
    $('#movie-message').textContent = '';
    movieEstimate();
  } catch (err) {
    $('#movie-message').textContent = err.message;
    return;
  }
  movieButtons(true);
}

function durationSeconds(text) {
  const parts = String(text || '').split(':').map(Number);
  if (parts.length < 2 || parts.some(n => !Number.isFinite(n))) return 0;
  return parts.reduce((total, n) => total * 60 + n, 0);
}

// « 00:57:00 » devient « 57 min », « 01:42:10 » devient « 1 h 42 ».
function readableDuration(text) {
  const seconds = durationSeconds(text);
  if (!seconds) return text || '';
  const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
  return h ? h + ' h ' + String(m).padStart(2, '0') : m + ' min';
}

function movieWording(kind) {
  const what = kind === 'episode' ? 'l’épisode' : 'le film';
  $('#movie-note').textContent = 'La lecture démarre pendant que le serveur compresse ' + what
    + '. Il reste ensuite dans « Mes films prêts », téléchargeable une fois la préparation terminée.';
}

function openMovieDialog() {
  $('#movie-dialog').showModal();
  // Focus sur l'action principale plutôt que sur la croix de fermeture.
  const watch = $('#watch-movie');
  if (!watch.disabled) watch.focus();
}

function movieEstimate() {
  const rates = {240: 346000, 360: 496000, 480: 896000, 720: 1596000};
  const rate = rates[$('#movie-height').value] / 8;
  const seconds = durationSeconds(state.movie && state.movie.duration);
  $('#movie-estimate').textContent = seconds
    ? 'Environ ' + size(rate * seconds) + ' après préparation.'
    : 'Environ ' + size(rate * 3600) + '/h après préparation.';
}
$('#movie-height').value = store.get('movie_height', '720');
$('#movie-height').onchange = () => { store.set('movie_height', $('#movie-height').value); movieEstimate(); };

$('#check-tracks').onclick = async () => {
  if (!state.movie) return;
  $('#check-tracks').disabled = true;
  try {
    const tracks = await api('/vod/tracks?kind=' + (state.movie.kind || 'movie')
      + '&provider=' + encodeURIComponent(state.movie.provider_id) + '&id=' + state.movie.stream_id);
    if (!tracks.verified) throw new Error('Impossible de vérifier les pistes de cette source.');
    $('#movie-audio').replaceChildren(new Option('Piste par défaut', ''));
    tracks.audio.forEach(t => $('#movie-audio').add(new Option(t.label + ' · ' + t.codec, String(t.index))));
    $('#movie-subtitle').replaceChildren(new Option('Sans sous-titres', ''));
    tracks.subtitles.filter(t => t.supported).forEach(t => $('#movie-subtitle').add(new Option(t.label, String(t.index))));
    $('#track-fields').hidden = false;
  } catch (err) {
    $('#movie-message').textContent = err.message;
  } finally {
    $('#check-tracks').disabled = false;
  }
};

function movieButtons(enabled) {
  $('#watch-movie').disabled = !enabled;
  $('#prepare-movie').disabled = !enabled;
}

async function requestPreparation() {
  return post('/prepare', {
    kind: state.movie.kind || 'movie',
    provider: state.movie.provider_id,
    id: state.movie.stream_id,
    height: Number($('#movie-height').value),
    audio: $('#track-fields').hidden || $('#movie-audio').value === '' ? null : Number($('#movie-audio').value),
    subtitle: $('#track-fields').hidden || $('#movie-subtitle').value === '' ? null : Number($('#movie-subtitle').value)
  });
}

$('#prepare-movie').onclick = async () => {
  if (!state.movie) return;
  movieButtons(false);
  try {
    await requestPreparation();
    $('#movie-dialog').close();
    await setMode('prepared');
  } catch (err) {
    $('#movie-message').textContent = err.message;
  } finally {
    movieButtons(true);
  }
};

$('#watch-movie').onclick = async () => {
  if (!state.movie) return;
  movieButtons(false);
  let job;
  try {
    job = await requestPreparation();
    $('#movie-dialog').close();
  } catch (err) {
    $('#movie-message').textContent = err.message;
    return;
  } finally {
    movieButtons(true);
  }
  watchWhenPlayable(job).catch(err => {
    stageScreen(null);
    $('#player-status').textContent = err.message;
  });
};

// Le serveur sonde la source avant de connaitre la duree du film : quelques
// secondes pendant lesquelles on affiche l'ecran d'ouverture.
async function watchWhenPlayable(job, start) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.current = {label: cleanTitle(job.title)};
  playbackUI(cleanTitle(job.title), false);
  showJobTitle(job);
  $('#player-status').textContent = 'Analyse du film sur le serveur…';
  let deadline = Date.now() + 60000;
  while (attempt === state.playback && Date.now() < deadline) {
    const jobs = await api('/preparations').catch(() => []);
    const fresh = jobs.find(j => j.id === job.id);
    if (fresh && fresh.state === 'failed') throw new Error(fresh.error || 'Échec de la préparation.');
    if (fresh && (fresh.state === 'ready' || fresh.playable)) return playJob(fresh, start);
    if (fresh && fresh.stage === 'subtitles') {
      // Abonnement a une seule connexion : le serveur lit d'abord tout le
      // fichier pour en extraire les sous-titres.
      $('#player-status').textContent = 'Récupération des sous-titres sur le serveur…';
      deadline = Math.max(deadline, Date.now() + 30000);
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (attempt === state.playback) throw new Error('Le serveur n’a pas pu ouvrir ce film. Retrouvez-le dans « Mes films prêts ».');
}

async function refreshJobs() {
  const jobs = await api('/preparations').catch(() => []);
  $('#jobs').replaceChildren();
  if (!jobs.length) $('#jobs').append(el('p', 'empty', 'Aucun film prêt pour l’instant. Ouvrez un film ou un épisode et choisissez « Préparer sans regarder ».'));
  jobs.forEach(job => {
    const item = historyItem(watchOfJob(job));
    const card = el('article', 'job');
    const art = artwork(item && item.icon, cleanTitle(job.title));
    if (item && !item.finished && item.duration > 0 && item.position >= 120) art.append(bar(item.position / item.duration));
    const body = el('div', 'job-body');
    const status = (job.state === 'ready' ? job.height + 'p · ' + size(job.size_bytes) + ' · prêt' :
      job.state === 'failed' ? (job.error || 'Échec de la préparation.') :
      job.stage === 'subtitles' ? 'Récupération des sous-titres…' :
      job.height + 'p · préparation ' + job.progress + ' %' + (job.playable ? ' · lisible dès maintenant' : ''));
    body.append(el('h3', '', cleanTitle(job.title)), el('p', 'job-status' + (job.state === 'failed' ? ' failed' : ''), status));
    if (job.state === 'preparing') {
      const p = el('progress');
      p.max = 100;
      p.value = job.progress;
      p.setAttribute('aria-label', 'Progression de la préparation');
      body.append(p);
    }
    const actions = el('div', 'job-actions');
    if (job.state === 'ready' || (job.state === 'preparing' && job.playable)) {
      const playButton = el('button', 'primary', jobPosition(job) ? '▶ Reprendre' : '▶ Regarder');
      playButton.onclick = () => playJob(job).catch(failure);
      actions.append(playButton);
    }
    if (job.state === 'ready') {
      const download = el('a', '', 'Télécharger');
      download.href = '/media/' + job.id + '/' + job.download + '?download=1';
      download.setAttribute('download', '');
      actions.append(download);
    }
    if (job.state === 'failed') {
      const retry = el('button', '', 'Relancer');
      retry.onclick = () => retryPreparation(job, retry).catch(failure);
      actions.append(retry);
    }
    const remove = el('button', 'quiet', 'Supprimer');
    remove.onclick = () => deletePreparation(job, remove).catch(failure);
    actions.append(remove);
    body.append(actions);
    card.append(art, body);
    $('#jobs').append(card);
  });
}

async function deletePreparation(job, button) {
  const ok = await askConfirm({
    title: job.state === 'preparing' ? 'Arrêter et supprimer ?' : 'Supprimer ce film ?',
    text: '« ' + cleanTitle(job.title) + ' » ' + (job.state === 'preparing'
      ? 'est encore en préparation. Elle sera arrêtée et le fichier effacé.'
      : 'sera effacé du serveur. Il faudra le préparer à nouveau pour le regarder.'),
    ok: 'Supprimer', danger: true
  });
  if (!ok) return;
  button.disabled = true;
  try {
    if (state.job && state.job.id === job.id) await stop();
    await post('/prepare/delete', {id: job.id});
    store.remove('position_' + job.id);
    await refreshJobs();
  } finally {
    button.disabled = false;
  }
}

async function retryPreparation(job, button) {
  const attempt = state.playback;
  state.job = job;
  if (button) button.disabled = true;
  try {
    await post('/prepare/retry', {id: job.id});
    await refreshJobs();
    message('Relance demandée. Reprenez le statut dans quelques instants.');
  } catch (err) {
    failure(err);
  } finally {
    if (button) button.disabled = false;
    if (state.playback === attempt) state.job = null;
  }
}

// start : position voulue, en secondes. Absente, on reprend la ou le compte
// s'etait arrete ; 0 force le debut.
async function playJob(job, start) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.job = job;
  state.watch = watchOfJob(job);
  let from = start === undefined ? jobPosition(job) : Number(start) || 0;
  const known = jobDuration(job);
  if (known && from > known - 20) from = 0;
  state.startAt = from > 5 ? from : 0;
  state.savedAt = Date.now();
  hideResume();
  tabStore.set('playing', {kind: 'watch', watch: state.watch, position: state.startAt});
  state.current = {label: cleanTitle(job.title)};
  playbackUI(cleanTitle(job.title), false);
  showJobTitle(job);
  attach('/media/' + job.id + '/master.m3u8', false, attempt);
  jobUsage(job);
  if (job.subtitles_ready) subtitleTrack(job);
}

function jobUsage(job) {
  $('#usage').textContent = job.state === 'ready' ? 'Téléchargement complet : ' + size(job.size_bytes)
    : 'Préparation sur le serveur · ' + job.progress + ' %';
}

function subtitleTrack(job) {
  video.querySelectorAll('track').forEach(t => t.remove());
  const track = el('track');
  track.kind = 'subtitles';
  track.label = 'Sous-titres sélectionnés';
  track.srclang = 'und';
  track.src = '/media/' + job.id + '/subtitles.vtt';
  track.default = true;
  video.append(track);
  setTimeout(syncSubtitles, 0);
}

// Film lance avant la fin de sa preparation : progression, et sous-titres des
// que le serveur les a extraits.
let jobBusy = false;
setInterval(async () => {
  const job = state.job;
  if (!job || job.state === 'ready' || jobBusy) return;
  jobBusy = true;
  try {
    const fresh = (await api('/preparations')).find(j => j.id === job.id);
    if (!fresh || state.job !== job) return;
    const subtitlesArrived = fresh.subtitles_ready && !job.subtitles_ready;
    Object.assign(job, fresh);
    jobUsage(job);
    if (subtitlesArrived) subtitleTrack(job);
    if (job.state === 'failed') $('#player-status').textContent = job.error || 'Échec de la préparation.';
  } catch (err) {
    // Le prochain tour reessaiera.
  } finally {
    jobBusy = false;
  }
}, 5000);

// Administration
function frenchDate(epoch) {
  return new Date(epoch * 1000).toLocaleDateString('fr-FR', {day: 'numeric', month: 'long', year: 'numeric'});
}

function expiryLabel(details) {
  // exp_date absent = abonnement sans echeance ; exp_date inconnu = le panel
  // ne le dit pas. Les deux ne doivent pas s'afficher pareil.
  if (details.expires_at === null || details.expires_at === undefined) return 'échéance inconnue';
  if (!details.expires_at) return 'sans échéance';
  const days = Math.round((details.expires_at * 1000 - Date.now()) / 86400000);
  if (days < 0) return 'expiré depuis le ' + frenchDate(details.expires_at);
  if (days === 0) return 'expire aujourd’hui';
  return 'expire le ' + frenchDate(details.expires_at) + ' · dans ' + days + (days > 1 ? ' jours' : ' jour');
}

function providerSummary(details) {
  if (!details.reachable) return ['injoignable' + (details.error ? ' · ' + details.error : '')];
  const lines = [];
  const head = [
    details.status || '',
    details.trial ? 'essai' : '',
    expiryLabel(details)
  ].filter(Boolean).join(' · ');
  if (head) lines.push(head);
  if (details.max_connections) {
    lines.push('connexions : ' + details.active_connections + ' / ' + details.max_connections
      + (details.formats && details.formats.length ? ' · formats : ' + details.formats.join(', ') : ''));
  }
  const c = details.catalog || {};
  const catalogue = [
    c.channels ? c.channels.toLocaleString('fr-FR') + ' chaînes' : '',
    c.vod ? c.vod.toLocaleString('fr-FR') + ' films' : '',
    c.series ? c.series.toLocaleString('fr-FR') + ' séries' : ''
  ].filter(Boolean).join(' · ');
  lines.push(catalogue ? 'catalogue local : ' + catalogue : 'catalogue local vide — lancez une synchronisation');
  return lines;
}

async function loadProviderDetails() {
  const list = await api('/providers/info').catch(() => []);
  list.forEach(details => {
    const slot = document.querySelector('[data-provider-details="' + CSS.escape(details.id) + '"]');
    if (!slot) return;
    slot.replaceChildren();
    slot.classList.toggle('unreachable', !details.reachable);
    providerSummary(details).forEach(line => slot.append(el('span', 'detail-line', line)));
  });
}

async function refreshConfig() {
  const st = await api('/status').catch(() => ({}));
  const hours = st.catalog_refresh_hours;
  $('#catalog-refresh-info').textContent = hours > 0
    ? `Le catalogue est ensuite actualisé automatiquement toutes les ${hours} heures.`
    : hours === 0 ? 'L’actualisation périodique est désactivée. Vous pouvez synchroniser à tout moment.' : '';
  $('#status').textContent = 'Charge système (1 / 5 / 15 min) : ' + (st.load || []).map(n => n.toFixed(2)).join(' / ') + '\nCapacité : ' + (st.stream ? st.stream.capacity : 0) + ' chaîne(s) distincte(s)\n' + ((st.stream && st.stream.workers) || []).map(w => w.label + ' · ' + w.state + ' · ' + w.viewers + ' appareil(s) · ' + w.failovers + ' bascule(s)').join('\n');
  $('#sync-log').textContent = (st.sync_log || []).join('\n') || 'Aucune synchronisation en cours.';
  $('#provider-list').replaceChildren();
  (st.providers || []).forEach(p => {
    const li = el('li');
    li.append(el('span', 'provider-name', p.name));
    li.append(el('span', 'kind-badge', p.kind === 'm3u' ? 'M3U' : 'Xtream'));
    const details = el('span', 'provider-details');
    details.dataset.providerDetails = p.id;
    details.append(el('span', 'detail-line', 'Lecture des informations…'));
    li.append(details);
    const sync = el('button', 'quiet', 'Synchroniser');
    sync.onclick = async () => { await post('/sync', {id: p.id}); message('Synchronisation démarrée.'); };
    const del = el('button', 'quiet', 'Supprimer');
    del.onclick = async () => {
      if (await askConfirm({title: 'Supprimer cet abonnement ?', text: '« ' + p.name + ' » et son catalogue seront retirés de ce serveur.', ok: 'Supprimer', danger: true})) {
        await post('/providers/delete', {id: p.id});
        await refreshConfig();
      }
    };
    li.append(sync, del);
    $('#provider-list').append(li);
  });
  // Les details interrogent le panel : on ne bloque pas l'affichage de la
  // liste dessus, et le cache serveur evite un appel toutes les 8 secondes.
  loadProviderDetails().catch(() => {});
  // Sans abonnement, le formulaire d'ajout est la seule chose utile : on l'ouvre.
  if (!(st.providers || []).length) $('#add-provider').open = true;
}

$('#tab-conf').onclick = async () => {
  try {
    if (state.configOpen) await closeSettings();
    else await openSettings();
  } catch (err) { failure(err); }
};
$('#close-config').onclick = () => { closeSettings().catch(failure); };
function providerKind() {
  const picked = document.querySelector('input[name="p-kind"]:checked');
  return picked ? picked.value : 'xtream';
}

// Les champs masques gardent leur attribut required : le navigateur refuse
// alors de soumettre, en signalant un champ invisible. On l'accorde au mode.
function syncProviderFields() {
  const m3u = providerKind() === 'm3u';
  $$('.p-xtream').forEach(field => {
    field.hidden = m3u;
    field.querySelectorAll('input').forEach(i => { i.required = !m3u; });
  });
  $$('.p-m3u').forEach(field => {
    field.hidden = !m3u;
    field.querySelectorAll('input').forEach(i => { i.required = m3u; });
  });
}
$$('input[name="p-kind"]').forEach(radio => { radio.onchange = syncProviderFields; });
syncProviderFields();

$('#provider-form').onsubmit = async e => {
  e.preventDefault();
  $('#p-add').disabled = true;
  $('#p-msg').textContent = 'Vérification…';
  try {
    const common = {
      name: $('#p-name').value.trim(),
      max_connections: Number($('#p-connections').value)
    };
    const kind = providerKind();
    const payload = kind === 'm3u'
      ? {...common, url: $('#p-url').value.trim()}
      : {...common, host: $('#p-host').value.trim(),
         username: $('#p-user').value.trim(), password: $('#p-pass').value};
    const added = await post('/providers', payload);
    $('#provider-form').reset();
    syncProviderFields();
    $('#p-msg').textContent = (kind === 'm3u' && added.kind === 'xtream')
      ? 'Lien reconnu comme panel Xtream. Synchronisation automatique du catalogue lancée.'
      : 'Abonnement ajouté. Synchronisation automatique du catalogue lancée ; la progression apparaît ci-dessous.';
    await refreshConfig();
  } catch (err) {
    $('#p-msg').textContent = err.message;
  } finally {
    $('#p-add').disabled = false;
  }
};
$('#sync-all').onclick = async () => { await post('/sync'); message('Synchronisation démarrée sur le serveur.'); };
// Lecteurs externes : identifiants Xtream et lien M3U, a copier dans l'app.
const PLAYER_MODES = [
  ['eco', 'Économie · 360p max'],
  ['balanced', 'Équilibré · 480p max'],
  ['sport', 'Sport · 720p max']
];

async function copyText(text) {
  // navigator.clipboard n'existe qu'en HTTPS : repli pour un serveur en HTTP.
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (err) {
      // Page sans focus ou permission refusee : on tente la methode ancienne.
    }
  }
  const area = el('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if (!copied) throw new Error('Copie impossible : sélectionnez le texte à la main.');
}

function accessRow(label, value, shown) {
  const row = el('div', 'access-row');
  const text = el('code', 'access-value', shown);
  text.title = shown;
  const copy = el('button', 'quiet', 'Copier');
  copy.type = 'button';
  copy.onclick = async () => {
    try {
      await copyText(value);
      copy.textContent = 'Copié ✓';
      setTimeout(() => { copy.textContent = 'Copier'; }, 1600);
    } catch (err) { failure(err); }
  };
  row.append(el('span', 'access-label', label), text, copy);
  return row;
}

async function loadPlayerAccess() {
  const data = await api('/player-credentials');
  const box = $('#player-access');
  box.replaceChildren();
  (data.players || []).forEach(player => {
    const card = el('div', 'access-card');
    const reveal = !!state.revealPlayer;
    const hidden = '•'.repeat(player.password.length);
    const secret = value => reveal ? value : value.split(player.password).join(hidden);
    card.append(
      accessRow('Serveur', player.server, player.server),
      accessRow('Identifiant', player.username, player.username),
      accessRow('Mot de passe', player.password, secret(player.password)),
      accessRow('Lien M3U', player.m3u_url, secret(player.m3u_url))
    );

    const tools = el('div', 'access-tools');
    const mode = el('select');
    mode.setAttribute('aria-label', 'Qualité maximale proposée au lecteur');
    const allowed = PLAYER_MODES.slice(0, PLAYER_MODES.findIndex(m => m[0] === player.max_mode) + 1 || PLAYER_MODES.length);
    allowed.forEach(([value, label]) => {
      const option = el('option', '', label);
      option.value = value;
      option.selected = value === (allowed.some(m => m[0] === player.mode) ? player.mode : allowed[allowed.length - 1][0]);
      mode.append(option);
    });
    mode.onchange = async () => {
      try {
        await post('/player-credentials', {username: player.username, mode: mode.value});
        $('#player-msg').textContent = 'Qualité enregistrée. Elle s’applique à la prochaine chaîne ouverte.';
      } catch (err) { failure(err); }
    };
    const toggle = el('button', 'quiet', reveal ? 'Masquer' : 'Afficher');
    toggle.type = 'button';
    toggle.onclick = () => { state.revealPlayer = !reveal; loadPlayerAccess().catch(failure); };
    const renew = el('button', 'quiet', 'Nouveau mot de passe');
    renew.type = 'button';
    renew.onclick = async () => {
      if (!await askConfirm({title: 'Nouveau mot de passe ?', text: 'L’ancien cessera aussitôt de fonctionner : chaque lecteur devra être reconfiguré.', ok: 'Générer', danger: true})) return;
      try {
        await post('/player-credentials', {username: player.username, regenerate: true});
        $('#player-msg').textContent = 'Nouveau mot de passe généré.';
        await loadPlayerAccess();
      } catch (err) { failure(err); }
    };
    tools.append(mode, toggle, renew);
    card.append(tools);
    box.append(card);
  });
}

// Appareils de l'application, associes par un code a usage unique.
async function loadDevices() {
  const devices = await api('/devices');
  const list = $('#device-list');
  list.replaceChildren();
  devices.forEach(device => {
    const li = el('li');
    const since = new Date(device.created * 1000).toLocaleDateString('fr-FR');
    li.append(el('span', 'provider-name', device.name), el('span', 'detail-line', 'associé le ' + since));
    const remove = el('button', 'quiet', 'Retirer');
    remove.onclick = async () => {
      if (!await askConfirm({title: 'Retirer cet appareil ?', text: '« ' + device.name + ' » ne pourra plus utiliser ce serveur. Sa lecture en cours s’arrêtera.', ok: 'Retirer', danger: true})) return;
      await post('/devices/delete', {id: device.id});
      await loadDevices();
    };
    li.append(remove);
    list.append(li);
  });
}

$('#pair-code').onclick = async () => {
  try {
    const data = await post('/pair-code');
    // Groupe par quatre : plus simple a recopier sur un telephone.
    $('#pair-value').textContent = data.code.slice(0, 4).toUpperCase() + '-' + data.code.slice(4).toUpperCase()
      + '  ·  valable ' + Math.round(data.expires_in / 60) + ' min, utilisable une fois';
    $('#pair-value').hidden = false;
  } catch (err) { failure(err); }
};

$('#viewer-token').onclick = async () => {
  const data = await post('/viewer-token');
  $('#viewer-value').textContent = data.token;
  $('#viewer-value').hidden = false;
};

// ====================================================================
// Navigation Clavier & Télécommande D-Pad (Touches fléchées, Entrée, Esc, Raccourcis)
// ====================================================================
window.addEventListener('keydown', e => {
  const activeEl = document.activeElement;
  const isInput = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'SELECT' || activeEl.tagName === 'TEXTAREA');

  // Lecteur a l'ecran : espace pour la pause, fleches pour avancer (film) ou
  // zapper (chaine). Un bouton qui a le focus garde son comportement.
  const onButton = activeEl && (activeEl.tagName === 'BUTTON' || activeEl.tagName === 'A');
  if (!isInput && document.body.classList.contains('is-playing') && !$('#player-wrap').hidden
      && !document.querySelector('dialog[open]')) {
    if ((e.key === ' ' && !onButton) || e.key === 'k' || e.key === 'K') { togglePlay(); e.preventDefault(); return; }
    if (state.job && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { seekBy(e.key === 'ArrowLeft' ? -10 : 10); e.preventDefault(); return; }
    if (!state.job && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && (state.mode === 'live' || state.mode === 'favorites')) {
      zapChannel(e.key === 'ArrowUp' ? -1 : 1); e.preventDefault(); return;
    }
  }

  // Raccourcis globaux si non en cours de saisie
  if (!isInput) {
    if (e.key === 'PageUp') {
      zapChannel(-1);
      e.preventDefault();
      return;
    }
    if (e.key === 'PageDown') {
      zapChannel(1);
      e.preventDefault();
      return;
    }
    if (e.key === 'Escape') {
      if (document.querySelector('dialog[open]')) return;
      if (stageBox.classList.contains('rail-open') && !$('#player-wrap').hidden) {
        setRail(false);
        e.preventDefault();
        return;
      }
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
        e.preventDefault();
        return;
      }
      if (!$('#player-wrap').hidden) {
        stop().catch(failure);
        e.preventDefault();
        return;
      }
      if (state.configOpen) {
        closeSettings().catch(failure);
        e.preventDefault();
        return;
      }
    }
    if ((e.key === 'f' || e.key === 'F') && !$('#player-wrap').hidden) {
      $('#fullscreen').click();
      e.preventDefault();
      return;
    }
    if ((e.key === 'p' || e.key === 'P') && !$('#player-wrap').hidden && document.pictureInPictureEnabled) {
      $('#pip').click();
      e.preventDefault();
      return;
    }
    if ((e.key === 'm' || e.key === 'M') && !$('#player-wrap').hidden) {
      video.muted = !video.muted;
      message(video.muted ? 'Son coupé' : 'Son rétabli');
      e.preventDefault();
      return;
    }
    // Raccourci C pour replier/déplier la barre latérale des chaînes
    if ((e.key === 'c' || e.key === 'C') && !$('#player-wrap').hidden) {
      if (!$('#pui-channels').hidden) setRail(!stageBox.classList.contains('rail-open'));
      e.preventDefault();
      return;
    }
    // Raccourci I pour afficher/masquer la télémétrie
    if ((e.key === 'i' || e.key === 'I') && !$('#player-wrap').hidden) {
      toggleTelemetry();
      e.preventDefault();
      return;
    }
  }

  // Navigation par flèches dans la grille de chaînes
  if (['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Enter'].includes(e.key) && !isInput) {
    // Accueil : ses propres rangees. Entree reste a l'element qui a le focus.
    if (state.mode === 'home') {
      const busy = document.body.classList.contains('is-playing') || document.querySelector('dialog[open]') || state.configOpen;
      if (!busy && e.key !== 'Enter' && homeArrow(e)) e.preventDefault();
      return;
    }
    // Liste masquee (films prets, reglages) : Entree lancait une chaine invisible.
    if ($('#catalogue').hidden || state.configOpen) return;
    const cards = Array.from($$('#channels > .channel-card'));
    if (!cards.length) return;

    // Déterminer le nombre de colonnes dans la grille actuelle
    let cols = 1;
    if (cards.length >= 2) {
      const firstTop = cards[0].getBoundingClientRect().top;
      const secondTop = cards[1].getBoundingClientRect().top;
      if (Math.abs(firstTop - secondTop) < 10) {
        cols = cards.findIndex(c => Math.abs(c.getBoundingClientRect().top - firstTop) > 10);
        if (cols <= 0) cols = cards.length;
      }
    }

    let currentIndex = state.selectedChannelIndex;
    if (currentIndex < 0) {
      const activeCardIndex = cards.findIndex(c => c.contains(activeEl));
      if (activeCardIndex >= 0) currentIndex = activeCardIndex;
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      currentIndex = currentIndex < 0 ? 0 : Math.min(cards.length - 1, currentIndex + cols);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      currentIndex = currentIndex < 0 ? 0 : Math.max(0, currentIndex - cols);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      currentIndex = currentIndex < 0 ? 0 : Math.min(cards.length - 1, currentIndex + 1);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      currentIndex = currentIndex < 0 ? 0 : Math.max(0, currentIndex - 1);
    } else if (e.key === 'Enter' && currentIndex >= 0 && currentIndex < cards.length) {
      e.preventDefault();
      const mainBtn = cards[currentIndex].querySelector('.channel-main');
      if (mainBtn) mainBtn.click();
      return;
    }

    if (currentIndex >= 0 && currentIndex < cards.length) {
      state.selectedChannelIndex = currentIndex;
      cards.forEach((c, idx) => c.classList.toggle('keyboard-selected', idx === currentIndex));
      const targetCard = cards[currentIndex];
      targetCard.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      const btn = targetCard.querySelector('.channel-main');
      if (btn) btn.focus({preventScroll: true});
    }
  }
});

// Initialisation session
(async () => {
  try {
    const me = await api('/me', {skipAuthRedirect: true});
    await connected(me.role);
  } catch (_) {
    const legacy = store.get('token');
    if (legacy) {
      try {
        const me = await post('/login', {token: legacy});
        await connected(me.role);
      } catch (_) {
        store.remove('token');
        requireLogin('La session a expiré, reconnectez-vous.');
      }
    } else {
      requireLogin();
    }
  }
  // Le formulaire s'ouvrait sans champ actif : sur un televiseur ou au
  // clavier, il fallait une tabulation avant de pouvoir taper.
  if (!$('#login').hidden) {
    const first = $('#user-input') || $('#token-input');
    if (first) first.focus({preventScroll: true});
  }
})();
