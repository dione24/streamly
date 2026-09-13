/* Streamly — client web.
 *
 * Lecture HLS : Safari lit le HLS nativement (meilleure batterie, AirPlay et
 * PiP gratuits), on ne charge donc hls.js que sur les navigateurs qui en ont
 * besoin. Le selecteur de qualite manuel compte autant que l'ABR automatique :
 * sur une connexion facturee au volume, on veut pouvoir forcer le barreau bas.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const state = {
  token: localStorage.getItem('streamly_token') || '',
  lang: localStorage.getItem('streamly_lang') || '',
  category: '',
  query: '',
  favorites: [],
  favoritesOnly: false,
  hls: null,
  current: null,
};

/* ------------------------------------------------------------------ API */

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {
    ...options,
    headers: {
      'X-Token': state.token,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) throw new Error('unauthorized');
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

const post = (path, body) =>
  api(path, { method: 'POST', body: JSON.stringify(body || {}) });

/* ------------------------------------------------------------ connexion */

async function signIn(token) {
  state.token = token;
  await api('/status');                       // leve si le jeton est mauvais
  localStorage.setItem('streamly_token', token);
  $('#login').hidden = true;
  $('#app').hidden = false;
  await boot();
}

$('#token-go').onclick = async () => {
  try {
    await signIn($('#token-input').value.trim());
  } catch (e) {
    $('#login-error').textContent =
      e.message === 'unauthorized' ? 'Jeton refusé.' : e.message;
  }
};

$('#token-input').onkeydown = (e) => { if (e.key === 'Enter') $('#token-go').click(); };

/* -------------------------------------------------------------- lecture */

function destroyPlayer() {
  if (state.hls) { state.hls.destroy(); state.hls = null; }
  const v = $('#video');
  v.removeAttribute('src');
  v.load();
}

function fillQualityMenu(levels, current) {
  const sel = $('#quality');
  sel.innerHTML = '<option value="-1">Auto</option>';
  levels.forEach((lvl, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = (lvl.height ? lvl.height + 'p' : 'niveau ' + i) +
      ' — ' + Math.round((lvl.bitrate || 0) / 1000) + ' kbps';
    sel.appendChild(opt);
  });
  sel.value = String(current ?? -1);
}

async function play(channel) {
  const q = new URLSearchParams({ lang: channel.lang || '', canonical: channel.canonical });
  const info = await api('/resolve?' + q.toString());

  destroyPlayer();
  state.current = channel;
  $('#player-wrap').hidden = false;
  $('#now-playing').textContent = channel.label;
  $('#bitrate').textContent = '';

  const url = info.play_url;
  const video = $('#video');

  // Safari (iOS/macOS) : HLS natif, ABR gere par le systeme.
  const native = video.canPlayType('application/vnd.apple.mpegurl');
  if (native && !window.Hls?.isSupported()) {
    video.src = url;
    video.play().catch(() => {});
    $('#quality').innerHTML = '<option value="-1">Auto (natif)</option>';
  } else if (window.Hls?.isSupported()) {
    const hls = new Hls({
      lowLatencyMode: false,
      maxBufferLength: 30,          // marge confortable sur reseau instable
      manifestLoadingRetryDelay: 1000,
      fragLoadingMaxRetry: 6,
    });
    state.hls = hls;
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      fillQualityMenu(hls.levels, hls.currentLevel);
      video.play().catch(() => {});
    });
    hls.on(Hls.Events.LEVEL_SWITCHED, (_e, d) => {
      const lvl = hls.levels[d.level];
      if (lvl) {
        $('#bitrate').textContent =
          lvl.height + 'p · ' + Math.round(lvl.bitrate / 1000) + ' kbps';
      }
      if ($('#quality').value === '-1') return;
      $('#quality').value = String(hls.currentLevel);
    });
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (!d.fatal) return;
      // Sur reseau instable, on retente plutot que d'abandonner.
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else destroyPlayer();
    });
  } else {
    video.src = url;
    video.play().catch(() => {});
  }

  $('#player-wrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('#quality').onchange = (e) => {
  if (!state.hls) return;
  state.hls.currentLevel = parseInt(e.target.value, 10);
};

$('#stop').onclick = async () => {
  destroyPlayer();
  $('#player-wrap').hidden = true;
  state.current = null;
  try { await post('/stop'); } catch (_) { /* sans consequence */ }
};

/* ------------------------------------------------------------- catalogue */

function favKey(c) { return (c.lang || '') + '|' + c.canonical; }

function isFavorite(c) {
  return state.favorites.some((f) => favKey(f) === favKey(c));
}

async function toggleFavorite(c, button) {
  if (isFavorite(c)) {
    await post('/favorites/delete', { lang: c.lang, canonical: c.canonical });
  } else {
    await post('/favorites', { lang: c.lang, canonical: c.canonical, label: c.label });
  }
  state.favorites = await api('/favorites');
  button.classList.toggle('on', isFavorite(c));
  if (state.favoritesOnly) renderChannels();
}

function channelRow(c) {
  const li = document.createElement('li');

  if (c.icon) {
    const img = document.createElement('img');
    img.src = c.icon;
    img.loading = 'lazy';
    img.onerror = () => img.remove();
    li.appendChild(img);
  }

  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = c.label;
  const sub = document.createElement('div');
  sub.className = 'sub';
  sub.textContent = [c.category, c.sources > 1 ? c.sources + ' sources' : null]
    .filter(Boolean).join(' · ');
  meta.append(name, sub);
  li.appendChild(meta);

  const star = document.createElement('button');
  star.className = 'star' + (isFavorite(c) ? ' on' : '');
  star.textContent = '★';
  star.onclick = (e) => { e.stopPropagation(); toggleFavorite(c, star); };
  li.appendChild(star);

  li.onclick = () => play(c).catch((err) => alert('Lecture impossible : ' + err.message));
  return li;
}

async function renderChannels() {
  const list = $('#channels');
  list.innerHTML = '';

  let items;
  if (state.favoritesOnly) {
    items = state.favorites.map((f) => ({ ...f, sources: 0, category: '' }));
  } else {
    const q = new URLSearchParams();
    if (state.lang) q.set('lang', state.lang);
    if (state.category) q.set('category', state.category);
    if (state.query) q.set('q', state.query);
    q.set('limit', '300');
    items = await api('/channels?' + q.toString());
  }

  items.forEach((c) => list.appendChild(channelRow(c)));
  $('#count').textContent = items.length + ' chaine(s)';
  $('#empty').hidden = items.length > 0;
}

async function loadFilters() {
  const langs = await api('/languages');
  const sel = $('#lang');
  sel.innerHTML = '<option value="">Toutes les langues</option>';
  langs.forEach((l) => {
    const o = document.createElement('option');
    o.value = l.lang;
    o.textContent = l.lang + ' (' + l.n + ')';
    sel.appendChild(o);
  });
  sel.value = state.lang;
  await loadCategories();
}

async function loadCategories() {
  const cats = await api('/categories' + (state.lang ? '?lang=' + encodeURIComponent(state.lang) : ''));
  const sel = $('#category');
  sel.innerHTML = '<option value="">Toutes les catégories</option>';
  cats.forEach((c) => {
    const o = document.createElement('option');
    o.value = c.name;
    o.textContent = c.name + ' (' + c.n + ')';
    sel.appendChild(o);
  });
  sel.value = state.category;
}

$('#lang').onchange = async (e) => {
  state.lang = e.target.value;
  state.category = '';
  localStorage.setItem('streamly_lang', state.lang);   // la langue est memorisee
  await loadCategories();
  await renderChannels();
};

$('#category').onchange = async (e) => {
  state.category = e.target.value;
  await renderChannels();
};

let searchTimer;
$('#search').oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = e.target.value.trim();
    state.favoritesOnly = false;
    renderChannels();
  }, 250);
};

$('#tab-fav').onclick = () => {
  state.favoritesOnly = !state.favoritesOnly;
  $('#tab-fav').style.borderColor = state.favoritesOnly ? '#ffc53d' : '';
  renderChannels();
};

/* -------------------------------------------------------------- reglages */

$('#tab-conf').onclick = async () => {
  const panel = $('#config');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await refreshConfig();
};

async function refreshConfig() {
  const st = await api('/status');
  $('#status').textContent = JSON.stringify(st.stream, null, 1) +
    '\n\nSynchro :\n' + JSON.stringify(st.sync, null, 1);
  $('#sync-log').textContent = (st.sync_log || []).join('\n') || '(aucune synchro)';

  const list = $('#provider-list');
  list.innerHTML = '';
  st.providers.forEach((p) => {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = p.name + '  (' + p.id + ')';
    span.style.flex = '1';
    const sync = document.createElement('button');
    sync.className = 'chip';
    sync.textContent = 'Synchroniser';
    sync.onclick = async () => {
      sync.textContent = '...';
      await post('/sync', { id: p.id });
      setTimeout(refreshConfig, 1500);
    };
    const del = document.createElement('button');
    del.className = 'chip';
    del.textContent = 'Supprimer';
    del.onclick = async () => {
      if (!confirm('Supprimer ' + p.name + ' ?')) return;
      await post('/providers/delete', { id: p.id });
      await refreshConfig();
    };
    li.append(span, sync, del);
    list.appendChild(li);
  });
}

$('#p-add').onclick = async () => {
  const msg = $('#p-msg');
  msg.textContent = 'Vérification des identifiants...';
  try {
    await post('/providers', {
      name: $('#p-name').value.trim(),
      host: $('#p-host').value.trim(),
      username: $('#p-user').value.trim(),
      password: $('#p-pass').value,
    });
    msg.textContent = 'Provider ajouté. Lancez une synchronisation.';
    ['#p-name', '#p-host', '#p-user', '#p-pass'].forEach((s) => ($(s).value = ''));
    await refreshConfig();
  } catch (e) {
    msg.textContent = 'Échec : ' + e.message;
  }
};

$('#sync-all').onclick = async () => {
  await post('/sync', {});
  $('#sync-log').textContent = 'Synchronisation démarrée...';
  const timer = setInterval(refreshConfig, 3000);
  setTimeout(() => clearInterval(timer), 180000);
};

/* ----------------------------------------------------------- demarrage */

async function boot() {
  state.favorites = await api('/favorites');
  await loadFilters();
  await renderChannels();
}

if (state.token) {
  signIn(state.token).catch(() => {
    $('#login').hidden = false;
    $('#app').hidden = true;
  });
} else {
  $('#login').hidden = false;
}
