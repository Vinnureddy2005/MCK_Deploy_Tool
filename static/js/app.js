/* McKesson Deployment Tool - dashboard controller (vanilla JS, no frameworks). */
'use strict';

const STAGE_LABELS = {
  validate: 'Validate',
  download: 'Download',
  connect: 'Connect',
  upload_to_copydata: 'Upload to CopyData',
  backup: 'Backup',
  update_checksum: 'Update Checksum',
  daemon_reload: 'Daemon Reload',
  copy_to_binaries: 'Copy to Binaries',
  restart: 'Restart',
  health_check: 'Health Check',
  live_logs: 'Live Logs',
};

const STAGE_ICONS = {
  waiting: '○',    // ○
  running: '⟳',    // ⟳
  completed: '✓',  // ✓
  failed: '✕',     // ✕
  skipped: '–',    // –
};

const el = (id) => document.getElementById(id);
const ui = {
  service: el('service'),
  version: el('version'),
  checksum: el('checksum'),
  verifyBtn: el('verify-btn'),
  verifyMsg: el('verify-msg'),
  deployBtn: el('deploy-btn'),
  overwrite: el('overwrite-backup'),
  pipeline: el('pipeline'),
  logs: el('logs'),
  autoscroll: el('autoscroll'),
  clearLogs: el('clear-logs'),
  socketBadge: el('socket-badge'),
  modeBadge: el('mode-badge'),
  liveBadge: el('live-badge'),
  targetHost: el('target-host'),
  info: {
    service: el('info-service'),
    jar: el('info-jar'),
    unit: el('info-unit'),
    port: el('info-port'),
    backup: el('info-backup'),
    current: el('info-current'),
    newValue: el('info-new'),
    checksum: el('info-checksum'),
  },
  fetchCurrent: el('fetch-current'),
  last: {
    panel: el('last-deployment'),
    status: el('last-status'),
    service: el('last-service'),
    jar: el('last-jar'),
    checksum: el('last-checksum'),
    finished: el('last-finished'),
    error: el('last-error'),
  },
  overlay: el('port-overlay'),
  conflictPort: el('conflict-port'),
  conflictList: el('conflict-list'),
  conflictDetail: el('conflict-detail'),
  viewProcess: el('view-process'),
  killProcess: el('kill-process'),
  cancelConflict: el('cancel-conflict'),
};

const state = {
  services: [],
  config: null,
  verified: false,
  deploying: false,
  currentChecksum: '',
  logCleared: false,
  selectedPid: null,
  conflictPort: null,
  socket: null,
  retry: 0,
};

/* ----------------------------------------------------------------- helpers */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (err) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error((payload && payload.detail) || `Request failed (HTTP ${response.status})`);
  }
  return payload;
}

function currentService() {
  return state.services.find((s) => s.key === ui.service.value) || null;
}

function jarName() {
  const service = currentService();
  if (!service) return '—';
  const version = ui.version.value.trim() || service.default_version;
  return `${service.jar_prefix}-${version}.jar`;
}

function setMessage(node, text, kind) {
  node.textContent = text;
  node.className = `message ${kind}`;
  node.hidden = !text;
}

/* -------------------------------------------------------------------- logs */

function appendLog(message, kind = 'info', time = null) {
  if (!state.logCleared) {
    ui.logs.textContent = '';
    state.logCleared = true;
  }
  const line = document.createElement('span');
  line.className = `log-line log-${kind}`;

  const stamp = document.createElement('span');
  stamp.className = 'log-time';
  stamp.textContent = `[${time || new Date().toTimeString().slice(0, 8)}]`;

  line.appendChild(stamp);
  line.appendChild(document.createTextNode(message));
  ui.logs.appendChild(line);
  ui.logs.appendChild(document.createTextNode('\n'));

  if (ui.autoscroll.checked) ui.logs.scrollTop = ui.logs.scrollHeight;
}

/* ---------------------------------------------------------------- pipeline */

function renderPipeline(deploymentState) {
  const order = (deploymentState && deploymentState.stage_order) || Object.keys(STAGE_LABELS);
  const stages = (deploymentState && deploymentState.stages) || {};
  ui.pipeline.textContent = '';

  order.forEach((key) => {
    const info = stages[key] || { status: 'waiting', message: '' };
    const item = document.createElement('li');
    item.className = `stage ${info.status}`;
    item.title = info.message || STAGE_LABELS[key] || key;

    const icon = document.createElement('span');
    icon.className = 'stage-icon';
    icon.textContent = STAGE_ICONS[info.status] || STAGE_ICONS.waiting;

    const label = document.createElement('span');
    label.textContent = STAGE_LABELS[key] || key;

    item.appendChild(icon);
    item.appendChild(label);
    ui.pipeline.appendChild(item);
  });
}

/* -------------------------------------------------------------- info panel */

function refreshInfo() {
  const service = currentService();
  if (!service) return;
  ui.info.service.textContent = service.display_name;
  ui.info.jar.textContent = jarName();
  ui.info.unit.textContent = service.systemd_service;
  ui.info.port.textContent = service.default_port === null ? 'n/a' : String(service.default_port);
  ui.version.placeholder = service.default_version;
}

function invalidateVerification(reason) {
  state.verified = false;
  ui.deployBtn.disabled = true;
  ui.info.checksum.textContent = reason;
  ui.info.checksum.className = '';
  ui.verifyMsg.hidden = true;
  showNewChecksum();
}

/* Shows the pasted value, and whether it differs from what is on the server. */
function showNewChecksum() {
  const value = ui.checksum.value.trim();
  if (!value) {
    ui.info.newValue.textContent = '—';
    ui.info.newValue.className = 'mono';
    return;
  }
  ui.info.newValue.textContent = value;
  ui.info.newValue.title = value;

  if (!state.currentChecksum) {
    ui.info.newValue.className = 'mono';
    return;
  }
  const same = value.toLowerCase() === state.currentChecksum.toLowerCase();
  ui.info.newValue.className = same ? 'mono warn' : 'mono ok';
  ui.info.newValue.textContent = same ? `${value}  (same as server — no change)` : value;
}

async function fetchCurrentChecksum() {
  const service = currentService();
  if (!service) return;

  ui.fetchCurrent.disabled = true;
  ui.info.current.textContent = 'reading…';
  ui.info.current.className = 'mono';
  try {
    const result = await api(
      `/api/deployment/current-checksum?service_key=${encodeURIComponent(service.key)}`
    );
    if (result.found) {
      state.currentChecksum = result.checksum;
      ui.info.current.textContent = result.checksum;
      ui.info.current.title = `${result.path}\n${result.checksum}`;
      ui.info.current.className = 'mono ok';
    } else {
      state.currentChecksum = '';
      ui.info.current.textContent = result.message || 'not found';
      ui.info.current.className = 'mono err';
    }
  } catch (error) {
    state.currentChecksum = '';
    ui.info.current.textContent = error.message;
    ui.info.current.className = 'mono err';
  } finally {
    ui.fetchCurrent.disabled = false;
    showNewChecksum();
  }
}

/* --------------------------------------------------------- last deployment */

// Read back from disk rather than memory, so it survives an app restart —
// after a failure the first question is usually "what did the last run do?".
async function loadLastDeployment() {
  let record = null;
  try {
    record = (await api('/api/deployment/last')).last;
  } catch (error) {
    return;
  }
  if (!record) {
    ui.last.panel.hidden = true;
    return;
  }

  const failed = record.status === 'failed';
  ui.last.status.textContent = record.dry_run
    ? 'DRY RUN'
    : failed
    ? 'FAILED'
    : 'SUCCESS';
  ui.last.status.className = 'pill ' + (record.dry_run ? 'dry' : failed ? 'failed' : 'success');

  ui.last.service.textContent = record.display_name || '—';
  ui.last.jar.textContent = record.jar || '—';
  ui.last.checksum.textContent = record.checksum || '—';
  ui.last.checksum.title = record.checksum || '';
  ui.last.finished.textContent = (record.finished_at || '').replace('T', ' ') || '—';

  if (failed && record.error) {
    ui.last.error.textContent =
      'Stopped at ' + (record.error_stage || '?').replace(/_/g, ' ') + ': ' + record.error;
    ui.last.error.hidden = false;
  } else {
    ui.last.error.hidden = true;
  }

  ui.last.panel.hidden = false;
}

/* ------------------------------------------------------------------ boot */

async function loadConfig() {
  const [config, services] = await Promise.all([api('/api/config'), api('/api/services')]);
  state.config = config;
  state.services = services.services;

  ui.service.textContent = '';
  state.services.forEach((service) => {
    const option = document.createElement('option');
    option.value = service.key;
    option.textContent = service.display_name;
    ui.service.appendChild(option);
  });

  ui.targetHost.textContent = `${config.username}@${config.host}  •  ${config.binaries_dir}`;
  ui.info.backup.textContent = config.backup_dir;
  ui.modeBadge.hidden = !config.dry_run;
  ui.liveBadge.hidden = config.dry_run;
  refreshInfo();
  renderPipeline(null);
  await loadLastDeployment();
}

/* -------------------------------------------------------------- websocket */

function connectSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/logs`);
  state.socket = socket;

  socket.onopen = () => {
    state.retry = 0;
    ui.socketBadge.textContent = 'connected';
    ui.socketBadge.className = 'badge badge-conn online';
  };

  socket.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    handleEvent(payload);
  };

  socket.onclose = () => {
    ui.socketBadge.textContent = 'reconnecting…';
    ui.socketBadge.className = 'badge badge-conn offline';
    state.retry = Math.min(state.retry + 1, 6);
    setTimeout(connectSocket, state.retry * 1000);
  };

  socket.onerror = () => socket.close();
}

function handleEvent(event) {
  switch (event.type) {
    case 'connected':
      if (event.state) renderPipeline(event.state);
      break;

    case 'log':
      appendLog(event.message, event.level || 'info', event.time);
      break;

    case 'journal':
      appendLog(event.message, 'journal', event.time);
      break;

    case 'applog':
      appendLog(event.message, 'applog', event.time);
      break;

    case 'stage':
      renderPipeline(event.state);
      if (event.status === 'running') {
        appendLog(`── ${(STAGE_LABELS[event.stage] || event.stage).toUpperCase()}`, 'stage', event.time);
      }
      break;

    case 'port_conflict':
      showConflict(event.conflict);
      break;

    case 'complete':
      finishDeployment(event.status, event.state);
      break;

    default:
      break;
  }
}

function finishDeployment(status, deploymentState) {
  state.deploying = false;
  ui.deployBtn.disabled = !state.verified;
  ui.deployBtn.textContent = 'Deploy';
  if (deploymentState) renderPipeline(deploymentState);
  loadLastDeployment();

  if (status === 'success') {
    appendLog('='.repeat(52), 'success');
    appendLog(deploymentState && deploymentState.dry_run ? 'DRY RUN COMPLETE' : 'DEPLOYMENT SUCCESSFUL', 'success');
    appendLog('='.repeat(52), 'success');
  } else {
    appendLog('='.repeat(52), 'error');
    appendLog('DEPLOYMENT FAILED', 'error');
    appendLog('='.repeat(52), 'error');
  }
}

/* ---------------------------------------------------------------- actions */

async function verifyChecksum() {
  const service = currentService();
  if (!service) return;

  ui.verifyBtn.disabled = true;
  try {
    const result = await api('/api/checksum/verify', {
      method: 'POST',
      body: JSON.stringify({
        service_key: service.key,
        checksum: ui.checksum.value,
        version: ui.version.value.trim() || null,
      }),
    });
    state.verified = true;
    ui.deployBtn.disabled = false;
    setMessage(ui.verifyMsg, `Verified – ${result.length}-character checksum accepted`, 'ok');
    ui.info.checksum.textContent = 'Verified';
    ui.info.checksum.className = 'ok';
    ui.info.jar.textContent = result.service.jar;
  } catch (error) {
    state.verified = false;
    ui.deployBtn.disabled = true;
    setMessage(ui.verifyMsg, error.message, 'error');
    ui.info.checksum.textContent = 'Verification failed';
    ui.info.checksum.className = 'err';
  } finally {
    ui.verifyBtn.disabled = false;
  }
}

async function deploy() {
  const service = currentService();
  if (!service || !state.verified || state.deploying) return;

  if (!state.config.dry_run) {
    const confirmed = window.confirm(
      `LIVE DEPLOYMENT\n\n` +
        `Service : ${service.display_name}\n` +
        `JAR     : ${jarName()}\n` +
        `Unit    : ${service.systemd_service}\n` +
        `Server  : ${state.config.host}\n\n` +
        `This will back up, upload, update APP_CHECKSUM and restart the service. Continue?`
    );
    if (!confirmed) return;
  }

  state.deploying = true;
  ui.deployBtn.disabled = true;
  ui.deployBtn.textContent = 'Deploying…';
  ui.logs.textContent = '';
  state.logCleared = true;

  try {
    await api('/api/deployment/deploy', {
      method: 'POST',
      body: JSON.stringify({
        service_key: service.key,
        checksum: ui.checksum.value,
        version: ui.version.value.trim() || null,
        overwrite_backup: ui.overwrite.checked,
      }),
    });
  } catch (error) {
    state.deploying = false;
    ui.deployBtn.disabled = false;
    ui.deployBtn.textContent = 'Deploy';
    appendLog(error.message, 'error');
  }
}

/* --------------------------------------------------- port conflict dialog */

function showConflict(conflict) {
  state.conflictPort = conflict.port;
  state.selectedPid = conflict.processes.length ? conflict.processes[0].pid : null;

  ui.conflictPort.textContent = String(conflict.port);
  ui.conflictDetail.hidden = true;
  ui.conflictDetail.textContent = '';
  ui.conflictList.textContent = '';

  conflict.processes.forEach((process) => {
    const row = document.createElement('div');
    row.className = 'conflict-row' + (process.pid === state.selectedPid ? ' selected' : '');
    [
      ['PID', String(process.pid)],
      ['Process', process.command],
      ['User', process.user],
      ['Socket', process.name],
    ].forEach(([label, value]) => {
      const key = document.createElement('span');
      key.textContent = label;
      const val = document.createElement('span');
      val.textContent = value;
      row.appendChild(key);
      row.appendChild(val);
    });
    row.addEventListener('click', () => {
      state.selectedPid = process.pid;
      Array.from(ui.conflictList.children).forEach((child) => child.classList.remove('selected'));
      row.classList.add('selected');
    });
    ui.conflictList.appendChild(row);
  });

  ui.killProcess.disabled = state.selectedPid === null;
  ui.overlay.hidden = false;
}

function hideConflict() {
  ui.overlay.hidden = true;
  state.selectedPid = null;
}

async function viewProcess() {
  if (state.conflictPort === null) return;
  try {
    const result = await api('/api/deployment/find-port-process', {
      method: 'POST',
      body: JSON.stringify({ port: state.conflictPort }),
    });
    ui.conflictDetail.textContent = result.raw || 'No process is holding this port any more.';
    ui.conflictDetail.hidden = false;
  } catch (error) {
    ui.conflictDetail.textContent = error.message;
    ui.conflictDetail.hidden = false;
  }
}

async function killProcess() {
  if (state.selectedPid === null) return;
  const confirmed = window.confirm(
    `Terminate PID ${state.selectedPid} on ${state.config.host}?\n\nThis sends SIGTERM to the process.`
  );
  if (!confirmed) return;

  ui.killProcess.disabled = true;
  try {
    const result = await api('/api/deployment/kill-process', {
      method: 'POST',
      body: JSON.stringify({ pid: state.selectedPid, confirmed: true }),
    });
    appendLog(result.message, result.killed ? 'warn' : 'error');
    if (!result.killed) {
      ui.killProcess.disabled = false;
      return;
    }
    hideConflict();

    const service = currentService();
    appendLog('Restarting service after resolving the port conflict…', 'info');
    state.deploying = true;
    ui.deployBtn.disabled = true;
    await api('/api/deployment/restart-after-kill', {
      method: 'POST',
      body: JSON.stringify({ service_key: service.key, version: ui.version.value.trim() || null }),
    });
  } catch (error) {
    appendLog(error.message, 'error');
    ui.killProcess.disabled = false;
  }
}

/* ----------------------------------------------------------------- events */

ui.service.addEventListener('change', () => {
  refreshInfo();
  // the previous service's checksum says nothing about this one
  state.currentChecksum = '';
  ui.info.current.textContent = '—';
  ui.info.current.className = 'mono';
  invalidateVerification('Waiting for verification');
});
ui.fetchCurrent.addEventListener('click', fetchCurrentChecksum);
ui.version.addEventListener('input', () => {
  refreshInfo();
  invalidateVerification('Waiting for verification');
});
ui.checksum.addEventListener('input', () => invalidateVerification('Waiting for verification'));
ui.verifyBtn.addEventListener('click', verifyChecksum);
ui.deployBtn.addEventListener('click', deploy);
ui.clearLogs.addEventListener('click', () => {
  ui.logs.textContent = '[Waiting for deployment...]';
  state.logCleared = false;
});
ui.viewProcess.addEventListener('click', viewProcess);
ui.killProcess.addEventListener('click', killProcess);
ui.cancelConflict.addEventListener('click', hideConflict);

loadConfig()
  .then(connectSocket)
  .catch((error) => {
    ui.targetHost.textContent = 'Configuration could not be loaded';
    appendLog(`Startup error: ${error.message}`, 'error');
  });

setInterval(() => {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) state.socket.send('ping');
}, 25000);
