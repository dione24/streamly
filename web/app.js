'use strict';
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const store = {
  get(k, fallback = '') { try { return localStorage.getItem('streamly_' + k) || fallback; } catch (_) { return fallback; } },
  set(k, v) { try { localStorage.setItem('streamly_' + k, v); } catch (_) {} },
  remove(k) { try { localStorage.removeItem('streamly_' + k); } catch (_) {} },
  json(k, fallback) { try { return JSON.parse(this.get(k)) || fallback; } catch (_) { return fallback; } },
};

const state = {
  mode: 'live', lang: store.get('lang'), provider: store.get('provider'), category: '', query: '', favorites: [],
  page: 0, pageSize: 48, request: 0, controller: null, hls: null, ticket: null, current: null,
  generation: null, retries: 0, statusMisses: 0, recoveryTimer: null, playback: 0, stalls: 0, started: 0,
  frames: false, movie: null, job: null, role: 'viewer', configTimer: null, jobTimer: null,
  configOpen: false, configReturnMode: 'live', configSnapshot: null,
  channelList: [], zapTimer: null,
  selectedChannelIndex: -1
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
  state.favorites = await api('/favorites').catch(() => []);
  await loadFilters();
  await renderChannels();
  renderRecents();
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
$('#play-mode').value = store.get('playmode', 'balanced');
$('#connection').value = store.get('connection', 'stable');
function modeHint() {
  const mode = $('#play-mode').value;
  $('#budget-fields').hidden = mode !== 'budget';
  $('#mode-hint').textContent = {
    eco: 'Qualité plafonnée pour préserver votre forfait mobile.',
    balanced: 'Un équilibre optimal entre netteté et consommation.',
    sport: 'Cadence d’images source préservée (jusqu’à 60 fps) pour un direct ultra fluide.',
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
  const cards = Array.from($$('#channels li[data-channel-key]'));
  cards.forEach(card => card.classList.toggle('now-playing', card.dataset.channelKey === key));
  if (!channel) return;
  const idx = state.channelList.findIndex(item => sameChannel(item, channel));
  state.selectedChannelIndex = idx >= 0 ? idx : state.selectedChannelIndex;
}

function nextChannelFromCurrent(offset) {
  if (!state.current) return null;
  if (state.mode !== 'live') return null;
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
  if (!state.current || state.mode !== 'live' || $('#player-wrap').hidden) return;
  const target = nextChannelFromCurrent(offset);
  if (!target) return;
  showZapOverlay(target, offset);
  play(target).catch(failure);
}

async function stop() {
  ++state.playback;
  const ticket = state.ticket;
  state.ticket = null;
  destroyPlayer();
  state.current = null;
  state.job = null;
  clearZapOverlay();
  $('#player-wrap').hidden = true;
  stageScreen(null);
  $('#program-info').hidden = true;
  $('#idle-hero').hidden = false;
  document.body.classList.remove('is-playing', 'reader-focus', 'catalogue-collapsed');
  $('#preferences').hidden = false;
  renderRecents();
  if (ticket) await post('/stop', {ticket}, {skipAuthRedirect: true}).catch(failure);
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
  const prefCollapsed = store.get('sidebar_collapsed') === '1';
  document.body.classList.toggle('catalogue-collapsed', prefCollapsed);
  updateSidebarUI();

  const telem = $('#telemetry');
  if (telem) {
    telem.hidden = store.get('telemetry_open') !== '1';
    updateTelemetryUI();
  }

  $('#live-badge').innerHTML = live ? '<span class="pulse-dot" aria-hidden="true"></span> DIRECT' : '● FILM';
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
  $('#player-wrap').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function play(channel) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.current = channel;
  state.generation = null;
  playbackUI(channel.label, true);
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
    remember(channel);
  } catch (err) {
    if (attempt === state.playback) {
      stageScreen(null);
      $('#player-status').textContent = err.message;
      message(err.message);
    }
  }
}

function attach(url, live, attempt) {
  if (attempt !== state.playback) return;
  if (state.hls) state.hls.destroy();
  state.hls = null;
  const v = $('#video');
  const startPlaying = () => v.play().catch(() => {
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
      abrEwmaDefaultEstimate: 450000,
      capLevelToPlayerSize: true,
      // Film en preparation : apres un saut, le serveur encode le passage
      // demande avant de repondre. On attend plutot que d'abandonner.
      ...(live ? {} : {startPosition: 0, fragLoadPolicy: {default: {
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
      select.replaceChildren(new Option('Automatique', '-1'));
      hls.levels.forEach((level, i) => {
        select.add(new Option((level.height ? level.height + 'p' : 'Qualité ' + (i + 1)) + ' · ≈ ' + size(level.bitrate * 3600 / 8) + '/h', String(i)));
      });
      startPlaying();
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, (_, d) => {
      const level = hls.levels[d.level];
      if (level) $('#bitrate').textContent = (level.height ? level.height + 'p · ' : '') + '≈ ' + size(level.bitrate * 3600 / 8) + '/h';
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
    $('#quality').replaceChildren(new Option('Auto · plafond respecté', '-1'));
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
$('#fullscreen').onclick = async () => {
  const v = $('#video');
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (v.requestFullscreen) {
      await v.requestFullscreen();
    } else if (v.webkitEnterFullscreen) {
      v.webkitEnterFullscreen();
    }
  } catch (err) { failure(err); }
};
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
video.addEventListener('timeupdate', () => { if (state.job && video.currentTime > 5) store.set('position_' + state.job.id, String(video.currentTime)); });
video.addEventListener('loadedmetadata', () => {
  if (state.job) {
    const pos = Number(store.get('position_' + state.job.id));
    if (pos && pos < video.duration - 20) video.currentTime = pos;
  }
});

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
  const li = el('li', 'channel-card' + (movie ? ' movie-card' : ''));
  li.dataset.channelKey = channelKey(c);
  const button = el('button', 'channel-main');
  button.type = 'button';
  if (!movie && number) button.append(el('span', 'ch-num', String(number)));
  const nameLabel = movie ? c.title : c.label;
  const fallback = (nameLabel || '?').trim().slice(0, 2).toUpperCase();
  const iconSlot = el('span', 'logo-slot');
  iconSlot.style.backgroundColor = getChannelColor(nameLabel);
  const placeholder = el('span', 'logo-placeholder', fallback);
  iconSlot.append(placeholder);

  if (c.icon && /^https?:\/\//.test(c.icon)) {
    // Les logos viennent de serveurs tiers, lents, et souvent en http (bloques
    // sur une page https). Le serveur les relaie et les garde : la vignette
    // s'affiche tout de suite, le logo la remplace quand il arrive.
    const img = el('img', 'channel-logo');
    img.src = '/api/logo?u=' + encodeURIComponent(c.icon);
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.onload = () => { img.classList.add('ready'); placeholder.hidden = true; };
    img.onerror = () => img.remove();
    iconSlot.append(img);
  }

  button.append(iconSlot);
  const meta = el('span', 'meta');
  meta.append(
    el('div', 'name', nameLabel),
    el('div', 'sub', movie ? (c.category_name || (kind === 'series' ? 'Série' : 'Film'))
                           : [c.category || 'Direct', c.lang].filter(Boolean).join(' · '))
  );
  if (!movie && c.epg_id) {
    // Rempli apres coup par loadNowNext : la liste s'affiche sans attendre le guide.
    const now = el('div', 'epg-now');
    now.dataset.epgId = c.epg_id;
    now.hidden = true;
    now.append(el('span', 'epg-title'), el('span', 'epg-bar'));
    meta.append(now);
  }
  button.append(meta);
  button.onclick = () => (kind === 'series' ? seriesDialog(c)
                        : kind === 'movie' ? movieDialog(c) : play(c)).catch(failure);
  if (!movie) button.ondblclick = () => fullscreenWhenReady();
  li.append(button);

  if (!movie) {
    const star = el('button', 'star' + (isFav(c) ? ' on' : ''), '★');
    star.setAttribute('aria-label', 'Favori : ' + c.label);
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
  }
  return li;
}

// Programme en cours : une requete par page de chaines, sur le guide en cache du serveur.
const clock = epoch => new Date(epoch * 1000).toLocaleTimeString('fr-FR', {hour: '2-digit', minute: '2-digit'});
const progressOf = p => Math.max(0, Math.min(100, (Date.now() / 1000 - p.start) / Math.max(1, p.stop - p.start) * 100));

async function loadNowNext(items) {
  const ids = [...new Set(items.map(c => c.epg_id).filter(Boolean))];
  if (!ids.length) return;
  const guide = await api('/guide/now?ids=' + encodeURIComponent(ids.join(',')), {skipAuthRedirect: true});
  $$('#channels .epg-now').forEach(slot => {
    const entry = guide[slot.dataset.epgId];
    const program = entry && (entry.now || entry.next);
    if (!program) return;
    slot.querySelector('.epg-title').textContent = (entry.now ? '' : clock(program.start) + ' · ') + program.title;
    slot.querySelector('.epg-bar').style.setProperty('--progress', (entry.now ? progressOf(entry.now) : 0) + '%');
    slot.hidden = false;
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
    if (state.mode === 'live') {
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

async function setMode(mode) {
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
  $('#preparations').hidden = mode !== 'prepared';
  $('#catalogue').hidden = mode === 'prepared';
  $('#filters').hidden = mode === 'favorites' || mode === 'prepared';
  $('#recent-wrap').hidden = mode !== 'live' || !store.json('recents', []).length;
  
  const modeClass = 'mode-' + (mode === 'favorites' ? 'favorites' : mode);
  document.body.classList.remove('mode-live', 'mode-vod', 'mode-series', 'mode-favorites', 'mode-prepared');
  document.body.classList.add(modeClass);

  ['live', 'vod', 'series', 'fav', 'prepared'].forEach(k => {
    $('#tab-' + k).classList.toggle('active', (k === 'fav' ? 'favorites' : k) === mode);
  });
  $('#catalogue-title').textContent = {live: 'À l’antenne.', vod: 'Une soirée cinéma.',
    series: 'Vos séries.', favorites: 'Vos incontournables.'}[mode] || '';
  $('#search').placeholder = {vod: 'Rechercher un film…', series: 'Rechercher une série…'}[mode]
    || 'Rechercher une chaîne…';
  
  clearInterval(state.jobTimer);
  if (mode === 'prepared') {
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
  $$('.rail .tab').forEach(t => t.classList.toggle('active', t.id === 'tab-conf'));
  state.configSnapshot = ['#player-wrap', '#preferences', '#recent-wrap', '#catalogue', '#preparations', '#filters', '#idle-hero']
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

$('#tab-live').onclick = () => setMode('live').catch(failure);
$('#tab-vod').onclick = () => setMode('vod').catch(failure);
$('#tab-series').onclick = () => setMode('series').catch(failure);
$('#tab-fav').onclick = () => setMode('favorites').catch(failure);
$('#tab-prepared').onclick = () => setMode('prepared').catch(failure);

function remember(c) {
  const list = store.json('recents', []).filter(x => favKey(x) !== favKey(c));
  list.unshift(c);
  store.set('recents', JSON.stringify(list.slice(0, 6)));
  renderRecents();
}

function renderRecents() {
  const list = store.json('recents', []);
  $('#recent-wrap').hidden = !list.length || state.mode !== 'live';
  $('#recents').replaceChildren();
  list.forEach(c => {
    const b = el('button', '', c.label);
    b.onclick = () => play(c).catch(failure);
    $('#recents').append(b);
  });
}

// Dialog films
async function seriesDialog(show) {
  $('#series-title').textContent = show.title || show.name;
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
        el('span', 'ep-dur', ep.duration || '')
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
  $('#movie-title').textContent = label;
  $('#movie-details').textContent = readableDuration(episode.duration);
  $('#movie-plot').textContent = state.movie.plot;
  $('#movie-message').textContent = '';
  $('#track-fields').hidden = true;
  movieButtons(true);
  movieEstimate();
  openMovieDialog();
}

async function movieDialog(c) {
  $('#movie-title').textContent = c.title;
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
$('#movie-height').onchange = movieEstimate;

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
async function watchWhenPlayable(job) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.current = {label: job.title};
  playbackUI(job.title, false);
  $('#player-status').textContent = 'Analyse du film sur le serveur…';
  let deadline = Date.now() + 60000;
  while (attempt === state.playback && Date.now() < deadline) {
    const jobs = await api('/preparations').catch(() => []);
    const fresh = jobs.find(j => j.id === job.id);
    if (fresh && fresh.state === 'failed') throw new Error(fresh.error || 'Échec de la préparation.');
    if (fresh && (fresh.state === 'ready' || fresh.playable)) return playJob(fresh);
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
  if (!jobs.length) $('#jobs').append(el('p', 'empty', 'Choisissez un film dans la bibliothèque pour préparer une version plus légère.'));
  jobs.forEach(job => {
    const card = el('article', 'job');
    const status = (job.state === 'ready' ? size(job.size_bytes) + ' · prêt à regarder' :
      job.state === 'failed' ? (job.error || 'Échec de la préparation.') :
      job.stage === 'subtitles' ? 'Récupération des sous-titres…' :
      'Préparation sur le serveur · ' + job.progress + ' %' + (job.playable ? ' · lisible dès maintenant' : ''));
    card.append(el('h3', '', job.title), el('p', 'muted small', job.height + 'p · ' + status));
    if (job.state === 'preparing') {
      const p = el('progress');
      p.max = 100;
      p.value = job.progress;
      p.setAttribute('aria-label', 'Progression de la préparation');
      card.append(p);
    }
    const actions = el('div', 'job-actions');
    if (job.state === 'ready' || (job.state === 'preparing' && job.playable)) {
      const playButton = el('button', 'primary', store.get('position_' + job.id) ? 'Reprendre ▶' : 'Regarder ▶');
      playButton.onclick = () => playJob(job).catch(failure);
      actions.append(playButton);
    }
    if (job.state === 'ready') {
      const download = el('a', '', 'Télécharger ↓');
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
    if (actions.children.length) card.append(actions);
    $('#jobs').append(card);
  });
}

async function deletePreparation(job, button) {
  const question = job.state === 'preparing'
    ? 'Arrêter la préparation de « ' + job.title + ' » et la supprimer ?'
    : 'Supprimer « ' + job.title + ' » ? Il faudra le préparer à nouveau pour le regarder.';
  if (!confirm(question)) return;
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

async function playJob(job) {
  const stopping = stop();
  const attempt = state.playback;
  await stopping;
  if (attempt !== state.playback) return;
  state.job = job;
  state.current = {label: job.title};
  playbackUI(job.title, false);
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
      if (confirm('Supprimer cet abonnement ?')) {
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
      if (!confirm('L’ancien mot de passe cessera aussitôt de fonctionner : chaque lecteur devra être reconfiguré. Continuer ?')) return;
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
      if (!confirm('Retirer cet appareil ? Sa lecture en cours s’arrêtera.')) return;
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
      toggleCatalogueSidebar();
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
