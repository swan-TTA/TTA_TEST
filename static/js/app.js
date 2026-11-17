/* 스마트 중복 파일 정리기 — 프론트엔드 로직
 *  - 비동기 API 통신, 진행률 폴링
 *  - 그룹/항목 렌더링, 스마트 일괄 선택, 미리보기 인스펙터
 *  - 정리 이력 조회 및 원위치 복구
 */
'use strict';

// ============================================================================
// 공통 유틸
// ============================================================================
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 파일명·경로는 사용자 데이터이므로 HTML 에 넣기 전 반드시 이스케이프 */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${units[i]}`;
}

function fmtTime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  const p = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** GET: api(url) / POST: api(url, body) — 실패 시 서버가 준 error 메시지로 예외 발생 */
async function api(url, body) {
  const options = body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new Error('서버에 연결할 수 없습니다. python app.py 가 실행 중인지 확인하세요.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
}

function toast(message, type = '') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), type === 'error' ? 6000 : 3500);
}

/** 백그라운드 작업이 끝날 때까지 /api/progress 를 폴링. onTick 으로 진행 상황 전달 */
async function pollTask(taskId, onTick) {
  for (;;) {
    const task = await api(`/api/progress/${taskId}`);
    onTick?.(task);
    if (task.status !== 'RUNNING') return task;
    await sleep(400);
  }
}

/** 확인 모달. 사용자가 확인을 누르면 true */
function confirmDialog({ title, html, okText, okClass = 'danger' }) {
  const dlg = $('#confirmDialog');
  $('#confirmTitle').textContent = title;
  $('#confirmBody').innerHTML = html;
  const ok = $('#confirmOk');
  ok.textContent = okText;
  ok.className = `btn ${okClass}`;
  return new Promise((resolve) => {
    // 확인 버튼은 클릭 즉시 결과를 확정하고, 취소·ESC·바깥 클릭은 close 이벤트로 받는다.
    // (close 이벤트는 브라우저가 늦게 보낼 수 있어 '확인' 판정을 여기에 맡기지 않음)
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      ok.removeEventListener('click', onOk);
      dlg.removeEventListener('close', onClose);
      resolve(result);
    };
    const onOk = () => { finish(true); dlg.close(); };
    const onClose = () => finish(false);
    ok.addEventListener('click', onOk);
    dlg.addEventListener('close', onClose);
    dlg.showModal();
  });
}

$$('dialog').forEach((dlg) => {
  dlg.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]') || e.target === dlg) dlg.close('');
  });
});

// ============================================================================
// 상태
// ============================================================================
const PAGE_SIZE = 60;  // 그룹을 한 번에 그리는 개수 (수천 그룹에서도 화면이 멈추지 않도록)

const state = {
  phase: 'IDLE',        // IDLE → ANALYZING → REVIEWING → ORGANIZING
  taskId: null,
  sessionId: null,
  groups: [],           // [{id, group_number, reclaimable_bytes, items: [...]}]
  items: new Map(),     // item.id → item
  groupOf: new Map(),   // item.id → group
  rendered: 0,
  activeItemId: null,
};

function setPhase(phase) {
  state.phase = phase;
  const btn = $('#btnPrimary');
  const busy = phase === 'ANALYZING' || phase === 'ORGANIZING';
  const labels = {
    IDLE: ['분석 시작', 'primary'],
    ANALYZING: ['분석 중지', 'neutral'],
    REVIEWING: ['파일 정리 확정', 'danger'],
    ORGANIZING: ['_Garbage_ 이동 중...', 'danger'],
  };
  btn.textContent = labels[phase][0];
  btn.className = `btn ${labels[phase][1]}`;
  btn.disabled = phase === 'ORGANIZING';

  $('#rootPath').disabled = phase !== 'IDLE';
  $('#btnBrowse').disabled = phase !== 'IDLE';
  $$('.tuning input').forEach((el) => { el.disabled = phase !== 'IDLE'; });
  $('#btnReset').hidden = phase !== 'REVIEWING';
  $('#progressBox').hidden = !busy;
  $('#toolbar').hidden = phase !== 'REVIEWING';
  $('#review').hidden = phase !== 'REVIEWING';
  $('#floatBar').hidden = phase !== 'REVIEWING';
  $('#emptyState').hidden = phase !== 'IDLE' || !$('#doneCard').hidden;
}

function showProgress(task) {
  $('#progressBar').style.width = `${task.percent}%`;
  $('#progressPct').textContent = `${task.percent}%`;
  $('#progressMsg').textContent = task.message || '';
  $('#progressFile').textContent = task.current_file || '';
}

// ============================================================================
// 분석
// ============================================================================
function readConfig() {
  return {
    include_subfolders: $('#optSub').checked,
    min_size_kb: Number($('#optMinKb').value),
    size_tolerance_percent: Number($('#optSizeTol').value),
    max_time_diff_hours: Number($('#optTime').value),
    name_similarity_percent: Number($('#optNameSim').value),
    enable_image_phash: $('#optPhash').checked,
    strict_hex_verification: $('#optHex').checked,
  };
}

async function startScan() {
  const rootPath = $('#rootPath').value.trim();
  if (!rootPath) { toast('정리 대상 폴더 경로를 입력해 주세요.', 'error'); $('#rootPath').focus(); return; }

  $('#doneCard').hidden = true;
  try {
    const started = await api('/api/scan', { root_path: rootPath, config: readConfig() });
    state.taskId = started.task_id;
    state.sessionId = started.session_id;
    $('#rootPath').value = started.root_path;
    localStorage.setItem('so.rootPath', started.root_path);
    showProgress({ percent: 0, message: '분석 준비 중...' });
    setPhase('ANALYZING');

    const task = await pollTask(started.task_id, showProgress);
    if (task.status === 'COMPLETED') {
      await loadSession(started.session_id);
    } else if (task.status === 'CANCELLED') {
      toast('분석을 중지했습니다.');
      setPhase('IDLE');
    } else {
      toast(`분석 실패: ${task.error || '알 수 없는 오류'}`, 'error');
      setPhase('IDLE');
    }
  } catch (e) {
    toast(e.message, 'error');
    setPhase('IDLE');
  }
}

async function cancelScan() {
  if (!state.taskId) return;
  try { await api(`/api/cancel/${state.taskId}`, {}); } catch (e) { /* 이미 끝난 작업이면 무시 */ }
}

/** 세션의 분석 결과를 불러와 검토 화면으로 전환 (이력 탭의 '검토 이어하기'에서도 사용) */
async function loadSession(sessionId) {
  const data = await api(`/api/sessions/${sessionId}/groups`);
  state.sessionId = sessionId;
  state.groups = data.groups;
  state.items.clear();
  state.groupOf.clear();
  for (const g of data.groups) {
    for (const it of g.items) { state.items.set(it.id, it); state.groupOf.set(it.id, g); }
  }
  $('#rootPath').value = data.session.root_path;

  if (!data.groups.length) {
    toast(`중복/유사 파일을 찾지 못했습니다. (검사한 파일 ${Number(data.session.total_files).toLocaleString()}개)`, 'ok');
    setPhase('IDLE');
    return;
  }
  state.rendered = 0;
  state.activeItemId = null;
  $('#groups').innerHTML = '';
  $('#summaryLine').innerHTML =
    `검사한 파일 <b>${Number(data.session.total_files).toLocaleString()}</b>개 중 ` +
    `중복/유사 그룹 <b>${data.groups.length.toLocaleString()}</b>개 · ` +
    `최대 절약 가능 <b>${fmtBytes(data.groups.reduce((s, g) => s + g.reclaimable_bytes, 0))}</b>`;
  renderMoreGroups();
  clearInspector();
  setPhase('REVIEWING');
  refreshSelectionUI();
}

// ============================================================================
// 그룹 / 항목 렌더링
// ============================================================================
function itemHtml(it) {
  const thumb = it.is_image
    ? `<img class="thumb" loading="lazy" alt="" src="/api/preview?thumb=1&size=96&path=${encodeURIComponent(it.file_path)}">`
    : `<span class="thumb ext">${esc((it.ext || '').replace('.', '') || 'file')}</span>`;
  return `
    <div class="item ${it.is_selected ? 'move' : 'keep'}" data-id="${it.id}">
      <input type="checkbox" ${it.is_selected ? 'checked' : ''} aria-label="정리 대상으로 선택">
      <span class="pill">${it.is_selected ? '이동 예정' : '보존'}</span>
      ${thumb}
      <div>
        <div class="name">${esc(it.file_name)}</div>
        <span class="badge">${esc(it.match_reason)}</span>
        <div class="path">${esc(it.file_path)}</div>
      </div>
      <div class="size">${fmtBytes(it.file_size)}</div>
      <div class="mtime">${fmtTime(it.mtime)}</div>
    </div>`;
}

function groupHtml(g) {
  return `
    <details class="group" open data-gid="${g.id}">
      <summary>
        <span class="g-title">Group ${g.group_number}</span>
        <span class="g-meta">파일 ${g.items.length}개 · 최대 절약 ${fmtBytes(g.reclaimable_bytes)}</span>
        <span class="g-sel"></span>
        <span class="g-actions">
          <button class="btn" data-gact="all">모두 선택</button>
          <button class="btn" data-gact="none">모두 해제</button>
          <select data-gstrat aria-label="이 그룹에 스마트 선택 적용">
            <option value="">이 그룹만 스마트 선택…</option>
            <option value="newest">최신 파일만 남기기</option>
            <option value="oldest">오래된 파일만 남기기</option>
            <option value="shallowest">상위 폴더 원본만 남기기</option>
            <option value="largest">최대 용량·고화질만 남기기</option>
          </select>
        </span>
      </summary>
      <div class="rows">${g.items.map(itemHtml).join('')}</div>
    </details>`;
}

function renderMoreGroups() {
  const next = state.groups.slice(state.rendered, state.rendered + PAGE_SIZE);
  $('#groups').insertAdjacentHTML('beforeend', next.map(groupHtml).join(''));
  state.rendered += next.length;
  const remaining = state.groups.length - state.rendered;
  const more = $('#btnMore');
  more.hidden = remaining <= 0;
  more.textContent = `그룹 더 보기 (${remaining.toLocaleString()}개 남음)`;
}

/** 선택 상태(state.items)를 화면(행 색상·배지·그룹 요약·하단 플로팅 바)에 반영 */
function refreshSelectionUI() {
  for (const row of $$('#groups .item')) {
    const it = state.items.get(Number(row.dataset.id));
    row.classList.toggle('move', it.is_selected);
    row.classList.toggle('keep', !it.is_selected);
    row.querySelector('input').checked = it.is_selected;
    row.querySelector('.pill').textContent = it.is_selected ? '이동 예정' : '보존';
  }
  for (const el of $$('#groups .group')) {
    const g = state.groups.find((x) => x.id === Number(el.dataset.gid));
    const sel = g.items.filter((it) => it.is_selected);
    const label = el.querySelector('.g-sel');
    const allSelected = sel.length === g.items.length;
    label.classList.toggle('warn', allSelected);
    label.textContent = allSelected
      ? '남는 사본 없음'
      : (sel.length ? `${sel.length}개 이동 예정 · ${fmtBytes(sel.reduce((s, it) => s + it.file_size, 0))}` : '');
  }
  let count = 0, bytes = 0;
  for (const it of state.items.values()) {
    if (it.is_selected) { count++; bytes += it.file_size; }
  }
  $('#selCount').textContent = count.toLocaleString();
  $('#selBytes').textContent = fmtBytes(bytes);
  $('#btnConfirmFloat').disabled = count === 0;
}

const selectedIds = () => [...state.items.values()].filter((it) => it.is_selected).map((it) => it.id);

/** 선택이 바뀔 때마다 조금 기다렸다가(디바운스) 서버 DB 에 저장 → 새로고침해도 선택이 유지됨 */
let syncTimer = null;
function selectionChanged() {
  refreshSelectionUI();
  clearTimeout(syncTimer);
  const sessionId = state.sessionId;
  syncTimer = setTimeout(() => {
    api(`/api/sessions/${sessionId}/selection`, { selected_ids: selectedIds() }).catch((e) => toast(e.message, 'error'));
  }, 500);
}

// ============================================================================
// 스마트 일괄 선택
// ============================================================================
/** 전략별 '보존 우선순위' 점수. 배열을 앞에서부터 비교해 가장 큰 항목 1개를 남김 */
const STRATEGIES = {
  newest: (it) => [it.mtime, -it.folder_depth],                              // 수정일이 가장 최신
  oldest: (it) => [-it.mtime, -it.folder_depth],                             // 수정일이 가장 오래됨
  shallowest: (it) => [-it.folder_depth, -it.file_path.length, it.mtime],    // 폴더 깊이가 가장 얕음
  largest: (it) => [it.file_size, it.mtime],                                 // 용량이 가장 큼(고화질)
};

function compareMetric(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1;
  }
  return 0;
}

function applyStrategy(strategy, groups = state.groups) {
  const metric = STRATEGIES[strategy];
  for (const g of groups) {
    let keep = g.items[0];
    let best = metric(keep);
    for (const it of g.items) {
      const m = metric(it);
      if (compareMetric(m, best) > 0) { keep = it; best = m; }
    }
    for (const it of g.items) it.is_selected = it !== keep;
  }
  selectionChanged();
}

function setAll(selected, groups = state.groups) {
  for (const g of groups) for (const it of g.items) it.is_selected = selected;
  selectionChanged();
}

// ============================================================================
// 인스펙터 (미리보기 · 상세)
// ============================================================================
function clearInspector() {
  $('#inspPreview').innerHTML = '<span>파일을 클릭하면 여기에 표시됩니다.</span>';
  $('#inspMeta').innerHTML = '';
  $('#btnOpenLocal').disabled = true;
}

function showInspector(itemId) {
  const it = state.items.get(itemId);
  if (!it) return;
  state.activeItemId = itemId;
  $$('#groups .item.active').forEach((el) => el.classList.remove('active'));
  $(`#groups .item[data-id="${itemId}"]`)?.classList.add('active');

  const q = encodeURIComponent(it.file_path);
  $('#inspPreview').innerHTML = it.is_image
    ? `<a href="/api/preview?path=${q}" target="_blank" rel="noopener" title="새 탭에서 원본 보기">
         <img alt="미리보기" src="/api/preview?thumb=1&size=640&path=${q}"></a>`
    : `<div><div class="file-ico">${esc((it.ext || '').replace('.', '').toUpperCase() || 'FILE')}</div>
         <div>미리보기를 지원하지 않는 형식입니다.<br>아래 버튼으로 열어 확인하세요.</div></div>`;
  $('#inspMeta').innerHTML = `
    <dt>파일명</dt><dd>${esc(it.file_name)}</dd>
    <dt>크기</dt><dd>${fmtBytes(it.file_size)} (${it.file_size.toLocaleString()} bytes)</dd>
    <dt>판단 사유</dt><dd>${esc(it.match_reason)}</dd>
    <dt>신뢰도</dt><dd>${Number(it.confidence_score).toFixed(1)}%</dd>
    <dt>수정일</dt><dd>${fmtTime(it.mtime)}</dd>
    <dt>생성일</dt><dd>${fmtTime(it.ctime)}</dd>
    <dt>폴더 깊이</dt><dd>대상 폴더 기준 ${it.folder_depth}단계</dd>
    <dt>경로</dt><dd class="mono">${esc(it.file_path)}</dd>`;
  $('#btnOpenLocal').disabled = false;
}

async function openLocal(path) {
  try { await api('/api/open-local', { path }); }
  catch (e) { toast(e.message, 'error'); }
}

// ============================================================================
// 파일 정리 확정 (_Garbage_ 이동)
// ============================================================================
async function confirmCleanup() {
  const ids = selectedIds();
  if (!ids.length) { toast('정리할 파일이 선택되지 않았습니다.', 'error'); return; }
  const bytes = ids.reduce((s, id) => s + state.items.get(id).file_size, 0);
  const emptied = state.groups.filter((g) => g.items.every((it) => it.is_selected)).length;

  const ok = await confirmDialog({
    title: '파일 정리 확정',
    okText: `${ids.length.toLocaleString()}개 이동`,
    html: `
      <p>선택한 <b>${ids.length.toLocaleString()}개</b> 파일(<b>${fmtBytes(bytes)}</b>)을 대상 폴더의
         <code>_Garbage_</code> 폴더로 이동합니다.</p>
      <p>영구 삭제되지 않으며, '정리 이력 · 복구' 탭에서 원래 위치로 되돌릴 수 있습니다.</p>
      ${emptied ? `<div class="warn"><b>${emptied.toLocaleString()}개 그룹</b>은 모든 파일이 선택되어 원래 위치에 사본이 하나도 남지 않습니다.</div>` : ''}`,
  });
  if (!ok) return;

  clearTimeout(syncTimer);
  const sessionId = state.sessionId;
  try {
    const started = await api('/api/cleanup', { session_id: sessionId, selected_ids: ids });
    showProgress({ percent: 0, message: '이동 준비 중...' });
    setPhase('ORGANIZING');
    const task = await pollTask(started.task_id, showProgress);
    if (task.status !== 'COMPLETED') throw new Error(task.error || '파일 정리에 실패했습니다.');
    showDoneCard(task.result);
  } catch (e) {
    toast(e.message, 'error');
  }
  resetReview();
}

function showDoneCard(r) {
  const links = (r.reports || []).map((name) => {
    const url = `/api/report-file/${r.session_id}/${encodeURIComponent(name)}`;
    return name.endsWith('.html')
      ? `<a class="btn sm" href="${url}" target="_blank" rel="noopener">HTML 보고서 열기</a>
         <a class="btn sm" href="${url}?download=1">HTML 다운로드</a>`
      : `<a class="btn sm" href="${url}?download=1">CSV 다운로드</a>`;
  }).join('');
  const fails = (r.failed || []).map((f) => `<li>${esc(f.path)} — ${esc(f.error)}</li>`).join('');
  const card = $('#doneCard');
  card.innerHTML = `
    <h3>파일 정리가 완료되었습니다</h3>
    <p><b>${r.moved_count.toLocaleString()}개</b> 파일을 이동해 <b>${fmtBytes(r.saved_bytes)}</b>를 확보했습니다.
       ${r.failed_count ? `건너뛴 파일 ${r.failed_count.toLocaleString()}개.` : ''}</p>
    <p>격리 폴더: <code>${esc(r.garbage_dir)}</code></p>
    ${fails ? `<ul class="fail-list">${fails}</ul>` : ''}
    <div class="links">${links}<button class="btn sm" id="btnGoHistory">정리 이력 · 복구 보기</button></div>`;
  card.hidden = false;
  window.scrollTo({ top: 0 });
}

function resetReview() {
  state.groups = [];
  state.items.clear();
  state.groupOf.clear();
  state.sessionId = null;
  $('#groups').innerHTML = '';
  setPhase('IDLE');
}

// ============================================================================
// 폴더 찾아보기 모달
// ============================================================================
let browsePath = '';

async function browseTo(path) {
  try {
    const data = await api(`/api/browse-dirs?path=${encodeURIComponent(path || '')}`);
    browsePath = data.path;
    $('#browseCurrent').textContent = data.path;
    $('#browseUp').disabled = !data.parent;
    $('#browseUp').dataset.path = data.parent || '';
    $('#browseShortcuts').innerHTML = data.shortcuts
      .map((s) => `<button class="chip" data-path="${esc(s.path)}">${esc(s.name)}</button>`).join('');
    $('#browseList').innerHTML = data.dirs.length
      ? data.dirs.map((d) => `<li data-path="${esc(d.path)}">${esc(d.name)}</li>`).join('')
      : '<li class="empty">하위 폴더가 없습니다.</li>';
    $('#browseList').scrollTop = 0;
  } catch (e) {
    toast(e.message, 'error');
  }
}

$('#btnBrowse').addEventListener('click', async () => {
  // 입력된 경로가 유효하면 거기서, 아니면 홈 폴더에서 시작
  await browseTo($('#rootPath').value.trim());
  if (!browsePath) await browseTo('');
  $('#browseDialog').showModal();
});
$('#browseList').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-path]');
  if (li) browseTo(li.dataset.path);
});
$('#browseShortcuts').addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (chip) browseTo(chip.dataset.path);
});
$('#browseUp').addEventListener('click', (e) => browseTo(e.currentTarget.dataset.path));
$('#browseSelect').addEventListener('click', () => {
  $('#rootPath').value = browsePath;
  $('#browseDialog').close('');
});

// ============================================================================
// 정리 이력 · 복구 탭
// ============================================================================
const openLogs = new Set();  // 파일 목록을 펼쳐 둔 세션 id

async function loadHistory() {
  let data;
  try { data = await api('/api/history'); }
  catch (e) { toast(e.message, 'error'); return; }
  const t = data.totals;

  $('#historyStats').innerHTML = `
    <div class="stat"><div class="label">정리 세션</div><div class="value">${t.cleanup_sessions.toLocaleString()}</div><div class="sub">전체 스캔 ${data.sessions.length.toLocaleString()}회</div></div>
    <div class="stat"><div class="label">현재 절약 중인 용량</div><div class="value">${fmtBytes(t.active_bytes)}</div><div class="sub">누적 이동 ${fmtBytes(t.moved_bytes)}</div></div>
    <div class="stat"><div class="label">격리 중인 파일</div><div class="value">${t.active_count.toLocaleString()}</div><div class="sub">_Garbage_ 폴더에 보관 중</div></div>
    <div class="stat"><div class="label">복구한 파일</div><div class="value">${t.restored_count.toLocaleString()}</div><div class="sub">누적 이동 ${t.moved_count.toLocaleString()}개 중</div></div>`;
  $('#btnRestoreAll').disabled = t.active_count === 0;

  const statusLabel = { COMPLETED: '분석 완료', CANCELLED: '중지됨', FAILED: '실패', RUNNING: '분석 중' };
  $('#historyList').innerHTML = data.sessions.length ? data.sessions.map((s) => {
    const reports = (s.reports || []).map((name) => {
      const url = `/api/report-file/${s.id}/${encodeURIComponent(name)}`;
      return name.endsWith('.html')
        ? `<a class="btn sm" href="${url}" target="_blank" rel="noopener">HTML 보고서</a>`
        : `<a class="btn sm" href="${url}?download=1">CSV</a>`;
    }).join('');
    const canResume = s.status === 'COMPLETED' && s.reviewable_groups > 0;
    return `
      <div class="session" data-sid="${s.id}">
        <div class="session-head">
          <div class="session-info">
            <div class="session-title">세션 #${s.id} <span class="status ${esc(s.status)}">${statusLabel[s.status] || esc(s.status)}</span>
              <span class="muted" style="font-weight:400">${esc(s.created_at)}</span></div>
            <div class="session-path">${esc(s.root_path)}</div>
            <div class="session-nums">
              검사 <b>${s.total_files.toLocaleString()}</b>개 · 그룹 <b>${s.duplicate_groups_count.toLocaleString()}</b>개
              ${s.moved_count ? ` · 이동 <b>${s.moved_count.toLocaleString()}</b>개 (${fmtBytes(s.moved_bytes)}) · 격리 중 <b>${s.active_count.toLocaleString()}</b>개 (${fmtBytes(s.active_bytes)})` : ' · 정리 이력 없음'}
            </div>
          </div>
          <div class="session-actions">
            ${canResume ? '<button class="btn sm" data-act="resume">검토 이어하기</button>' : ''}
            ${s.moved_count ? '<button class="btn sm" data-act="logs">파일 목록</button>' : ''}
            ${reports}
            ${s.active_count ? '<button class="btn sm success" data-act="restore-session">이 세션 전체 복구</button>' : ''}
          </div>
        </div>
        <div class="logs" hidden></div>
      </div>`;
  }).join('') : '<div class="empty-state"><strong>아직 이력이 없습니다.</strong><span>분석 · 정리 탭에서 첫 스캔을 시작해 보세요.</span></div>';

  // 새로고침 전에 펼쳐 두었던 파일 목록은 다시 펼쳐서 보여 줌
  for (const sid of openLogs) {
    const el = $(`.session[data-sid="${sid}"]`);
    if (el) await renderLogs(el, sid);
  }
}

async function renderLogs(sessionEl, sessionId) {
  const box = sessionEl.querySelector('.logs');
  try {
    const { logs } = await api(`/api/sessions/${sessionId}/logs`);
    box.innerHTML = `
      <table>
        <thead><tr><th>상태</th><th>파일명</th><th class="num">크기</th><th>원래 위치</th><th>이동 시각</th><th></th></tr></thead>
        <tbody>${logs.map((l) => `
          <tr>
            <td>${l.restored_at ? '<span class="tag out">복구됨</span>' : '<span class="tag in">격리 중</span>'}</td>
            <td><b>${esc(l.file_name)}</b><div class="muted">${esc(l.match_reason)}</div></td>
            <td class="num">${fmtBytes(l.file_size)}</td>
            <td class="path">${esc(l.original_path)}</td>
            <td class="num">${esc(l.moved_at)}${l.restored_at ? `<div class="muted">복구 ${esc(l.restored_at)}</div>` : ''}</td>
            <td class="num">${l.restored_at ? '' : `<button class="btn sm" data-open="${esc(l.garbage_path)}">열기</button> <button class="btn sm success" data-restore="${l.id}">원위치 복구</button>`}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
    box.hidden = false;
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function runRestore(body, question) {
  const ok = await confirmDialog({ title: '원위치 복구', okText: '복구', okClass: 'success', html: question });
  if (!ok) return;
  const box = $('#historyProgress');
  try {
    const started = await api('/api/restore', body);
    box.hidden = false;
    const task = await pollTask(started.task_id, (t) => {
      $('#histBar').style.width = `${t.percent}%`;
      $('#histMsg').textContent = `${t.message} ${t.current_file || ''}`;
    });
    if (task.status !== 'COMPLETED') throw new Error(task.error || '복구에 실패했습니다.');
    const r = task.result;
    let msg = `${r.restored_count.toLocaleString()}개 파일을 원래 위치로 복구했습니다.`;
    if (r.renamed.length) msg += ` (원래 자리에 다른 파일이 있어 ${r.renamed.length}개는 '복구본' 이름으로 복구)`;
    toast(msg, 'ok');
    if (r.failed_count) toast(`${r.failed_count}개 복구 실패: ${r.failed[0].error}`, 'error');
  } catch (e) {
    toast(e.message, 'error');
  }
  box.hidden = true;
  loadHistory();
}

$('#btnRefreshHistory').addEventListener('click', loadHistory);
$('#btnRestoreAll').addEventListener('click', () => runRestore(
  { all: true }, '<p><code>_Garbage_</code>에 격리 중인 <b>모든 파일</b>을 원래 위치로 되돌립니다.</p>'));

$('#historyList').addEventListener('click', async (e) => {
  const sessionEl = e.target.closest('.session');
  if (!sessionEl) return;
  const sid = Number(sessionEl.dataset.sid);

  const restoreBtn = e.target.closest('[data-restore]');
  if (restoreBtn) {
    return runRestore({ log_ids: [Number(restoreBtn.dataset.restore)] }, '<p>이 파일을 원래 위치로 되돌립니다.</p>');
  }
  const openBtn = e.target.closest('[data-open]');
  if (openBtn) return openLocal(openBtn.dataset.open);

  const act = e.target.closest('[data-act]')?.dataset.act;
  if (act === 'logs') {
    const box = sessionEl.querySelector('.logs');
    if (box.hidden) { openLogs.add(sid); await renderLogs(sessionEl, sid); }
    else { openLogs.delete(sid); box.hidden = true; }
  } else if (act === 'restore-session') {
    runRestore({ session_id: sid }, `<p>세션 #${sid}에서 격리한 파일을 <b>모두</b> 원래 위치로 되돌립니다.</p>`);
  } else if (act === 'resume') {
    if (state.phase === 'ANALYZING' || state.phase === 'ORGANIZING') { toast('진행 중인 작업이 끝난 뒤 다시 시도하세요.', 'error'); return; }
    try {
      $('#doneCard').hidden = true;
      await loadSession(sid);
      switchTab('analyze');
    } catch (err) { toast(err.message, 'error'); }
  }
});

// ============================================================================
// 이벤트 연결
// ============================================================================
function switchTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === `tab-${name}`));
  $('#floatBar').hidden = !(name === 'analyze' && state.phase === 'REVIEWING');
  if (name === 'history') loadHistory();
}
$$('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));

$('#btnPrimary').addEventListener('click', () => {
  if (state.phase === 'IDLE') startScan();
  else if (state.phase === 'ANALYZING') cancelScan();
  else if (state.phase === 'REVIEWING') confirmCleanup();
});
$('#btnConfirmFloat').addEventListener('click', confirmCleanup);
$('#btnReset').addEventListener('click', () => { clearTimeout(syncTimer); resetReview(); });
$('#rootPath').addEventListener('keydown', (e) => { if (e.key === 'Enter' && state.phase === 'IDLE') startScan(); });
$('#btnMore').addEventListener('click', () => { renderMoreGroups(); refreshSelectionUI(); });
$('#btnOpenLocal').addEventListener('click', () => {
  const it = state.items.get(state.activeItemId);
  if (it) openLocal(it.file_path);
});
$('#doneCard').addEventListener('click', (e) => { if (e.target.id === 'btnGoHistory') switchTab('history'); });

// 툴바: 전체 대상 스마트 선택
$('#toolbar').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.strategy) applyStrategy(btn.dataset.strategy);
  else if (btn.dataset.all !== undefined) setAll(btn.dataset.all === '1');
});

// 그룹 영역: 이벤트 위임 (행이 수천 개여도 리스너는 하나)
const groupsEl = $('#groups');
groupsEl.addEventListener('click', (e) => {
  // 그룹 헤더의 버튼/셀렉트를 눌렀을 때 아코디언이 접히지 않도록
  if (e.target.closest('.g-actions')) {
    e.preventDefault();
    const btn = e.target.closest('[data-gact]');
    if (btn) {
      const g = state.groups.find((x) => x.id === Number(btn.closest('.group').dataset.gid));
      setAll(btn.dataset.gact === 'all', [g]);
    }
    return;
  }
  const row = e.target.closest('.item');
  if (!row) return;
  if (e.target.matches('input[type="checkbox"]')) return;  // 체크박스는 change 이벤트에서 처리
  showInspector(Number(row.dataset.id));
});
groupsEl.addEventListener('change', (e) => {
  if (e.target.matches('.item input[type="checkbox"]')) {
    const it = state.items.get(Number(e.target.closest('.item').dataset.id));
    it.is_selected = e.target.checked;
    selectionChanged();
  } else if (e.target.matches('[data-gstrat]') && e.target.value) {
    const g = state.groups.find((x) => x.id === Number(e.target.closest('.group').dataset.gid));
    applyStrategy(e.target.value, [g]);
    e.target.value = '';
  }
});
groupsEl.addEventListener('dblclick', (e) => {
  const row = e.target.closest('.item');
  if (row && !e.target.matches('input')) openLocal(state.items.get(Number(row.dataset.id)).file_path);
});

// ============================================================================
// 시작
// ============================================================================
$('#rootPath').value = localStorage.getItem('so.rootPath') || '';
setPhase('IDLE');
