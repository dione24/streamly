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
  if (!str) return '#232b24';
  let hash = 0;
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
  const tints = ['#202e23', '#28311e', '#1f3032', '#322b1f', '#212734', '#2d2133'];
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
    const payload = username ? {username, password} : {token: password, password};
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

function playbackUI(title, live) {
  $('#player-wrap').hidden = false;
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

  $('#live-badge').innerHTML = live ? '<span class="pulse-dot" aria-hidden="true"></span> DIRECT' : '● FILM PRÊT';
  $('#back-live').hidden = !live;
  $('#player-status').textContent = 'Préparation de la lecture…';
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
      capLevelToPlayerSize: true
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
video.addEventListener('waiting', () => { if (state.frames) state.stalls++; $('#player-status').textContent = 'Mise en réserve…'; });
video.addEventListener('playing', () => {
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
      $('#player-status').textContent = st.error ? ('Aucune source disponible : ' + st.error) : 'Aucune source disponible. Réessayez plus tard.';
      return;
    }
    if ((state.generation === null && st.generation > 0) || (state.generation !== null && state.generation !== st.generation)) {
      $('#player-status').textContent = 'Passage à une source de secours…';
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
function row(c, movie = false) {
  const li = el('li', 'channel-card' + (movie ? ' movie-card' : ''));
  li.dataset.channelKey = channelKey(c);
  const button = el('button', 'channel-main');
  button.type = 'button';
  const nameLabel = movie ? c.title : c.label;
  const fallback = (nameLabel || '?').trim().slice(0, 2).toUpperCase();
  const iconSlot = el('span', 'logo-slot');
  iconSlot.style.backgroundColor = getChannelColor(nameLabel);
  const placeholder = el('span', 'logo-placeholder', fallback);
  const loader = el('span', 'logo-loader');
  iconSlot.append(placeholder, loader);

  if (c.icon && /^https?:\/\//.test(c.icon)) {
    const img = el('img', 'channel-logo');
    img.src = c.icon;
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.onload = () => {
      placeholder.hidden = true;
      if (loader.isConnected) loader.remove();
    };
    img.onerror = () => {
      img.remove();
      placeholder.hidden = false;
      if (loader.isConnected) loader.remove();
    };
    iconSlot.append(img);
  } else if (loader.isConnected) {
    loader.remove();
  }

  button.append(iconSlot);
  const meta = el('span', 'meta');
  meta.append(
    el('div', 'name', nameLabel),
    el('div', 'sub', movie ? (c.category_name || 'Film') : [c.category || 'Direct', c.lang].filter(Boolean).join(' · '))
  );
  button.append(meta);
  button.onclick = () => (movie ? movieDialog(c) : play(c)).catch(failure);
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
      items = await api((state.mode === 'vod' ? '/vod?' : '/channels?') + q, {signal: controller.signal});
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
    items.forEach(c => fragment.append(row(c, state.mode === 'vod')));
    $('#channels').append(fragment);
    $('#load-more').hidden = !hasMore;
    $('#empty').hidden = $('#channels').children.length > 0;
    const totalRendered = $('#channels').children.length;
    $('#count').textContent = totalRendered + (state.mode === 'vod' ? ' films affichés' : ' chaînes affichées') + (hasMore ? ' · suite disponible' : '');
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

let filterGeneration = 0;
async function loadFilters() {
  await loadProviders();
  const langs = await api('/languages' + (state.provider ? '?provider=' + encodeURIComponent(state.provider) : '')).catch(() => []);
  const sel = $('#lang');
  sel.replaceChildren(new Option('Toutes les langues', ''));
  langs.forEach(l => sel.add(new Option(l.lang, l.lang)));
  sel.value = state.lang;
  if (sel.selectedIndex < 0) { state.lang = ''; sel.value = ''; }
  await loadCategories();
}

function normalizeCategoryValue(value) {
  if (!value) return '';
  return typeof value === 'string' ? value : (value.name || value.slug || '');
}

function renderCategoryChips(items = []) {
  const chips = $('#category-chips');
  if (!chips) return;

  const values = [...new Set(items.map(normalizeCategoryValue).filter(Boolean))];
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
      renderCategoryChips(values);
      renderChannels();
    };
    return chip;
  };

  chips.append(makeChip('', 'Toutes les catégories'));
  values.forEach(value => chips.append(makeChip(value, value)));
}

async function loadCategories() {
  const serial = ++filterGeneration;
  const params = new URLSearchParams();
  if (state.lang) params.set('lang', state.lang);
  if (state.provider) params.set('provider', state.provider);
  const suffix = params.toString() ? '?' + params : '';
  const cats = await api((state.mode === 'vod' ? '/vod/categories' : '/categories') + suffix).catch(() => []);
  if (serial !== filterGeneration) return;
  const sel = $('#category');
  const rawValues = cats.map(normalizeCategoryValue).filter(Boolean);
  const values = [...new Set(rawValues)];
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
  state.mode = mode;
  state.category = '';
  state.query = '';
  $('#search').value = '';
  updateClearSearch();
  $('#config').hidden = true;
  $('#preparations').hidden = mode !== 'prepared';
  $('#catalogue').hidden = mode === 'prepared';
  $('#filters').hidden = mode === 'favorites';
  $('#recent-wrap').hidden = mode !== 'live' || !store.json('recents', []).length;
  
  const modeClass = 'mode-' + (mode === 'favorites' ? 'favorites' : mode);
  document.body.classList.remove('mode-live', 'mode-vod', 'mode-favorites', 'mode-prepared');
  document.body.classList.add(modeClass);

  ['live', 'vod', 'fav', 'prepared'].forEach(k => {
    $('#tab-' + k).classList.toggle('active', (k === 'fav' ? 'favorites' : k) === mode);
  });
  $('#catalogue-title').textContent = {live: 'À l’antenne.', vod: 'Une soirée cinéma.', favorites: 'Vos incontournables.'}[mode] || '';
  $('#search').placeholder = mode === 'vod' ? 'Rechercher un film…' : 'Rechercher une chaîne…';
  
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
  state.configSnapshot = ['#player-wrap', '#preferences', '#recent-wrap', '#catalogue', '#preparations']
    .map(id => {
      const element = $(id);
      if (!element) return null;
      return {element, hidden: element.hidden};
    }).filter(Boolean);
  state.configSnapshot.forEach(item => { item.element.hidden = true; });
  $('#config').hidden = false;
  clearInterval(state.configTimer);
  await refreshConfig();
  $('#config').scrollIntoView({behavior: 'smooth', block: 'start'});
  state.configTimer = setInterval(() => refreshConfig().catch(failure), 8000);
}

async function closeSettings() {
  if (!state.configOpen) return;
  state.configOpen = false;
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
async function movieDialog(c) {
  $('#movie-title').textContent = c.title;
  $('#movie-message').textContent = 'Chargement des informations…';
  $('#movie-details').textContent = '';
  $('#movie-plot').textContent = '';
  $('#track-fields').hidden = true;
  $('#prepare-movie').disabled = true;
  $('#movie-dialog').showModal();
  try {
    const info = await api('/vod/info?provider=' + encodeURIComponent(c.provider_id) + '&id=' + c.stream_id);
    state.movie = info;
    $('#movie-title').textContent = info.title;
    $('#movie-details').textContent = [info.duration, info.size_bytes ? 'Source : ≈ ' + size(info.size_bytes) : 'Taille source inconnue'].filter(Boolean).join(' · ');
    $('#movie-plot').textContent = info.plot || '';
    $('#movie-message').textContent = '';
    movieEstimate();
  } catch (err) {
    $('#movie-message').textContent = err.message;
    return;
  }
  $('#prepare-movie').disabled = false;
}

function movieEstimate() {
  const rates = {240: 346000, 360: 496000, 480: 896000, 720: 1596000};
  $('#movie-estimate').textContent = 'Environ ' + size(rates[$('#movie-height').value] * 3600 / 8) + '/h après préparation.';
}
$('#movie-height').onchange = movieEstimate;

$('#check-tracks').onclick = async () => {
  if (!state.movie) return;
  $('#check-tracks').disabled = true;
  try {
    const tracks = await api('/vod/tracks?provider=' + encodeURIComponent(state.movie.provider_id) + '&id=' + state.movie.stream_id);
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

$('#prepare-movie').onclick = async () => {
  if (!state.movie) return;
  $('#prepare-movie').disabled = true;
  try {
    await post('/prepare', {
      provider: state.movie.provider_id,
      id: state.movie.stream_id,
      height: Number($('#movie-height').value),
      audio: $('#track-fields').hidden || $('#movie-audio').value === '' ? null : Number($('#movie-audio').value),
      subtitle: $('#track-fields').hidden || $('#movie-subtitle').value === '' ? null : Number($('#movie-subtitle').value)
    });
    $('#movie-dialog').close();
    await setMode('prepared');
  } catch (err) {
    $('#movie-message').textContent = err.message;
  } finally {
    $('#prepare-movie').disabled = false;
  }
};

async function refreshJobs() {
  const jobs = await api('/preparations').catch(() => []);
  $('#jobs').replaceChildren();
  if (!jobs.length) $('#jobs').append(el('p', 'empty', 'Choisissez un film dans la bibliothèque pour préparer une version plus légère.'));
  jobs.forEach(job => {
    const card = el('article', 'job');
    const status = (job.state === 'ready' ? size(job.size_bytes) + ' · prêt à regarder' :
      job.state === 'failed' ? (job.error || 'Échec de la préparation.') : 'Préparation sur le serveur · ' + job.progress + ' %');
    card.append(el('h3', '', job.title), el('p', 'muted small', job.height + 'p · ' + status));
    if (job.state === 'preparing') {
      const p = el('progress');
      p.max = 100;
      p.value = job.progress;
      p.setAttribute('aria-label', 'Progression de la préparation');
      card.append(p);
    }
    const actions = el('div', 'job-actions');
    if (job.state === 'ready') {
      const playButton = el('button', 'primary', store.get('position_' + job.id) ? 'Reprendre ▶' : 'Regarder ▶');
      playButton.onclick = () => playJob(job).catch(failure);
      const download = el('a', '', 'Télécharger ↓');
      download.href = '/media/' + job.id + '/' + job.download + '?download=1';
      download.setAttribute('download', '');
      actions.append(playButton, download);
    }
    if (job.state === 'failed') {
      const retry = el('button', '', 'Relancer');
      retry.onclick = () => retryPreparation(job, retry).catch(failure);
      actions.append(retry);
    }
    if (actions.children.length) card.append(actions);
    $('#jobs').append(card);
  });
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
  $('#usage').textContent = 'Téléchargement complet : ' + size(job.size_bytes);
  if (job.subtitles) {
    const track = el('track');
    track.kind = 'subtitles';
    track.label = 'Sous-titres sélectionnés';
    track.srclang = 'und';
    track.src = '/media/' + job.id + '/subtitles.vtt';
    track.default = true;
    video.append(track);
  }
}

// Administration
async function refreshConfig() {
  const st = await api('/status').catch(() => ({}));
  $('#status').textContent = 'Charge système (1 / 5 / 15 min) : ' + (st.load || []).map(n => n.toFixed(2)).join(' / ') + '\nCapacité : ' + (st.stream ? st.stream.capacity : 0) + ' chaîne(s) distincte(s)\n' + ((st.stream && st.stream.workers) || []).map(w => w.label + ' · ' + w.state + ' · ' + w.viewers + ' appareil(s) · ' + w.failovers + ' bascule(s)').join('\n');
  $('#sync-log').textContent = (st.sync_log || []).join('\n') || 'Aucune synchronisation en cours.';
  $('#provider-list').replaceChildren();
  (st.providers || []).forEach(p => {
    const li = el('li');
    li.append(el('span', '', p.name));
    li.append(el('span', 'kind-badge', p.kind === 'm3u' ? 'M3U' : 'Xtream'));
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
      ? 'Lien reconnu comme panel Xtream : programme et films disponibles. Vous pouvez synchroniser.'
      : 'Abonnement ajouté. Vous pouvez synchroniser son catalogue.';
    await refreshConfig();
  } catch (err) {
    $('#p-msg').textContent = err.message;
  } finally {
    $('#p-add').disabled = false;
  }
};
$('#sync-all').onclick = async () => { await post('/sync'); message('Synchronisation démarrée sur le serveur.'); };
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
})();
