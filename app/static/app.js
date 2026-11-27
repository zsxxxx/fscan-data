/* 商品数据扫描 · 前端交互 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    platforms: [],
    platformMap: {},
    selected: '',
    products: [],
    lastResponse: null,
    loginPoll: null,
    loginChecking: false,
    loginFrameActive: false,
    page: 1,
    pageSize: 100,
  };

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function safeUrl(value) {
    const url = String(value || '');
    return /^https?:\/\//i.test(url) ? url : '';
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    let data;
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (!resp.ok) {
      const detail = data.detail || data.message || `请求失败 (${resp.status})`;
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return data;
  }

  let toastTimer = null;
  function toast(message, type) {
    const el = $('toast');
    el.textContent = message;
    el.className = 'toast' + (type ? ' ' + type : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add('hidden'), 3600);
  }

  function showLoading(show, text) {
    $('loadingText').textContent = text || '采集中…';
    $('loading').classList.toggle('hidden', !show);
  }

  function setBadge(status, text) {
    const map = { ok: 'badge-ok', no: 'badge-no', wait: 'badge-wait', idle: 'badge-idle' };
    const badge = $('loginBadge');
    badge.className = 'badge ' + (map[status] || map.idle);
    badge.textContent = text;
  }

  function setNavStatus(platformId, status) {
    const dot = document.querySelector(`[data-nav="${platformId}"] .status-dot`);
    if (!dot) return;
    dot.className = 'status-dot' + (status === 'ok' ? ' on' : status === 'wait' ? ' wait' : '');
  }

  function renderNav() {
    const nav = $('platformNav');
    nav.innerHTML = state.platforms.map((p) => `
      <button type="button" class="platform-item${p.id === state.selected ? ' active' : ''}${p.enabled ? '' : ' disabled'}"
              data-nav="${escapeHtml(p.id)}" title="${escapeHtml(p.note || p.name)}">
        <span class="swatch" style="background:${escapeHtml(p.color)}"></span>
        <span class="name">${escapeHtml(p.name)}</span>
        <span class="status-dot"></span>
      </button>`).join('');
    nav.querySelectorAll('.platform-item').forEach((btn) => {
      btn.addEventListener('click', () => selectPlatform(btn.dataset.nav));
    });
  }

  function filterMsOptions(ms, query) {
    const items = Array.from(ms.querySelectorAll('.ms-item'));
    let visible = 0;
    items.forEach((item) => {
      const label = item.dataset.label || item.textContent.toLowerCase();
      const match = !query || label.indexOf(query) >= 0;
      item.classList.toggle('hidden', !match);
      if (match) visible++;
    });
    const emptyTip = ms.querySelector('.ms-empty');
    if (emptyTip) emptyTip.classList.toggle('hidden', visible > 0);
  }

  function renderPlatformFields(platform) {
    const container = $('platformFields');
    const fields = platform.search_fields || [];
    $('searchConditionNote').textContent = fields.length
      ? `${platform.name}专属条件已加载（${fields.length} 项）`
      : '';
    container.innerHTML = fields.map((f) => {
      const key = escapeHtml(f.key);
      const label = escapeHtml(f.label);

      // 两级联动分类：一级 select + 二级 select（按一级动态填充）
      if (f.type === 'cascade') {
        const first = (f.options || []).map((o) =>
          `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`
        ).join('');
        return `<div class="field"><span>${label}</span><div class="cascade" data-key="${key}">
          <select class="cascade-l1" data-filter="${key}">${first}</select>
          <select class="cascade-l2" data-filter="${key}_sub"><option value="">全部子类</option></select>
        </div></div>`;
      }

      // 下拉多选：固定宽度触发器 + 按内容自适应的横铺勾选面板 + 实时模糊搜索
      if (f.type === 'multiselect') {
        const items = (f.options || []).map((o) =>
          `<label class="ms-item" data-label="${escapeHtml(o.label.toLowerCase())}"><input type="checkbox" value="${escapeHtml(o.value)}"><span>${escapeHtml(o.label)}</span></label>`
        ).join('');
        return `<div class="field"><span>${label}</span>
          <div class="ms" data-filter="${key}">
            <button type="button" class="ms-trigger"><span class="ms-text">不限</span><span class="ms-caret">▾</span></button>
            <div class="ms-panel">
              <input type="text" class="ms-search" placeholder="搜索筛选…" autocomplete="off">
              <div class="ms-options">${items}</div>
              <div class="ms-empty hidden">无匹配项</div>
            </div>
          </div></div>`;
      }

      // 价格区间：内置区间 + 自定义起止价
      if (f.key === 'price_range') {
        const options = (f.options || []).map((o) =>
          `<option value="${escapeHtml(o.value)}"${o.value === f.default ? ' selected' : ''}>${escapeHtml(o.label)}</option>`
        ).join('');
        return `<div class="field"><span>${label}</span>
          <select data-filter="price_range">${options}</select>
          <div class="price-custom hidden">
            <input id="priceMin" type="number" min="0" step="0.01" placeholder="最低价">
            <span class="price-sep">—</span>
            <input id="priceMax" type="number" min="0" step="0.01" placeholder="最高价">
          </div>
        </div>`;
      }

      // 单选下拉
      const options = (f.options || []).map((o) =>
        `<option value="${escapeHtml(o.value)}"${o.value === f.default ? ' selected' : ''}>${escapeHtml(o.label)}</option>`
      ).join('');
      return `<label class="field"><span>${label}</span><select data-filter="${key}">${options}</select></label>`;
    }).join('');

    // 自定义价格区间：选中时才显示起止价输入
    const priceSelect = container.querySelector('[data-filter="price_range"]');
    if (priceSelect) {
      const box = container.querySelector('.price-custom');
      const toggle = () => box.classList.toggle('hidden', priceSelect.value !== 'custom');
      priceSelect.addEventListener('change', toggle);
      toggle();
    }

    // 两级联动：一级变化时重建二级选项
    container.querySelectorAll('.cascade').forEach((box) => {
      const key = box.dataset.key;
      const l1 = box.querySelector('.cascade-l1');
      const l2 = box.querySelector('.cascade-l2');
      const meta = (platform.search_fields || []).find((f) => f.key === key);
      const map = (meta && meta.cascade) || {};
      const fill = () => {
        const subs = map[l1.value] || [];
        l2.innerHTML = [ '<option value="">全部子类</option>' ].concat(
          subs.map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`)
        ).join('');
      };
      l1.addEventListener('change', fill);
      fill();
    });

    // 下拉多选：展开/收起、勾选回显、实时模糊搜索、点击外部关闭
    container.querySelectorAll('.ms').forEach((ms) => {
      const trigger = ms.querySelector('.ms-trigger');
      const text = ms.querySelector('.ms-text');
      const search = ms.querySelector('.ms-search');
      const emptyTip = ms.querySelector('.ms-empty');
      trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        const willOpen = !ms.classList.contains('open');
        container.querySelectorAll('.ms.open').forEach((x) => x.classList.remove('open'));
        if (willOpen) {
          ms.classList.add('open');
          if (search) { search.value = ''; filterMsOptions(ms, ''); search.focus(); }
        }
      });
      ms.querySelectorAll('input[type=checkbox]').forEach((cb) => {
        cb.addEventListener('change', () => {
          const selected = Array.from(ms.querySelectorAll('input:checked')).map((c) => c.value);
          text.textContent = selected.length ? `已选 ${selected.length} 项` : '不限';
        });
      });
      // 实时模糊搜索：按选项文字过滤，大小写不敏感
      if (search) {
        search.addEventListener('input', () => filterMsOptions(ms, search.value.trim().toLowerCase()));
        search.addEventListener('click', (e) => e.stopPropagation());
      }
      ms.addEventListener('click', (e) => e.stopPropagation());
    });
    document.addEventListener('click', () => {
      container.querySelectorAll('.ms.open').forEach((x) => x.classList.remove('open'));
    });
  }

  function setSearchCollapsed(collapsed) {
    const form = $('searchForm');
    form.classList.toggle('hidden', collapsed);
    $('searchToggleBtn').textContent = collapsed ? '▸' : '▾';
    $('searchToggleBtn').setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    $('searchSummary').textContent = collapsed
      ? '已折叠，点击展开筛选条件'
      : '筛选条件已展开';
  }

  function collectFilters() {
    const filters = {};
    document.querySelectorAll('#platformFields [data-filter]').forEach((el) => {
      // 下拉多选
      if (el.classList.contains('ms')) {
        const selected = Array.from(el.querySelectorAll('input:checked')).map((c) => c.value);
        if (selected.length) filters[el.dataset.filter] = selected.join(',');
        return;
      }
      const value = (el.value || '').trim();
      if (el.dataset.filter === 'price_range' && value === 'custom') {
        const min = ($('priceMin').value || '').trim();
        const max = ($('priceMax').value || '').trim();
        if (min && max && Number(min) > Number(max)) {
          toast('自定义价格区间：最低价不能大于最高价', 'error');
          return null;
        }
        if (min || max) filters.price_range = `${min}-${max}`;
        return;
      }
      if (value !== '') filters[el.dataset.filter] = value;
    });
    return filters;
  }

  function selectPlatform(id) {
    state.selected = id;
    const platform = state.platformMap[id];
    $('platformTitle').textContent = platform.name;
    $('platformNote').textContent = platform.note || '';
    $('keyword').placeholder = `例如：在${platform.name}搜索的商品名称`;
    $('searchBtn').disabled = !platform.enabled;
    $('loginModalTitle').textContent = `登录${platform.name}`;
    renderPlatformFields(platform);
    renderNav();
    setBadge('idle', '未检测');
    $('loginDetail').textContent = '正在检测登录态…';
    refreshStatus(false).catch((err) => {
      $('loginDetail').textContent = err.message;
      setBadge('no', '检测失败');
    });
  }

  async function refreshStatus(force) {
    const status = await api(`/api/platforms/${encodeURIComponent(state.selected)}/status?force=${force ? 1 : 0}`);
    if (status.logged_in) {
      setBadge('ok', '已登录');
      setNavStatus(status.id, 'ok');
    } else {
      setBadge('no', '未登录');
      setNavStatus(status.id, '');
    }
    $('loginDetail').textContent = status.detail || '';
    const task = status.login_task || {};
    if (task.status === 'running') {
      setBadge('wait', '登录中');
      setNavStatus(status.id, 'wait');
    }
    return status;
  }

  function stopLoginPoll() {
    if (state.loginPoll) {
      clearInterval(state.loginPoll);
      state.loginPoll = null;
    }
  }

  async function pollLoginTask() {
    try {
      const task = await api(`/api/platforms/${encodeURIComponent(state.selected)}/login-task`);
      $('loginModalMsg').textContent = task.message || '正在等待登录…';
      if (task.status === 'success') {
        stopLoginPoll();
        $('loginModal').classList.add('hidden');
        toast(`${state.platformMap[state.selected].name} 登录成功`, 'success');
        refreshStatus(true);
      } else if (['failed', 'timeout', 'cancelled'].includes(task.status)) {
        stopLoginPoll();
        $('loginModal').classList.add('hidden');
        toast(task.message || '登录未完成', task.status === 'cancelled' ? '' : 'error');
        refreshStatus(true);
      }
    } catch (err) {
      $('loginModalMsg').textContent = err.message;
    }
  }

  let loginFrameTimer = null;

  function clearLoginFrameTimer() {
    if (loginFrameTimer) {
      clearTimeout(loginFrameTimer);
      loginFrameTimer = null;
    }
  }

  function hideAllLoginStates() {
    ['loginLoading', 'loginFrame', 'loginFallback', 'loginCheckFail', 'loginCollectorWait']
      .forEach((id) => $(id).classList.add('hidden'));
  }

  function showLoginLoading(message) {
    clearLoginFrameTimer();
    hideAllLoginStates();
    $('loginLoading').classList.remove('hidden');
    if (message) $('loginModalMsg').textContent = message;
  }

  function showLoginFallback(message) {
    clearLoginFrameTimer();
    hideAllLoginStates();
    $('loginFallback').classList.remove('hidden');
    $('loginModalMsg').textContent = message || '登录页无法在当前页面显示';
  }

  function showLoginCheckFail(message) {
    clearLoginFrameTimer();
    hideAllLoginStates();
    $('loginCheckFail').classList.remove('hidden');
    $('loginCheckFailMsg').textContent = message || '未检测到采集器登录态';
    $('loginModalMsg').textContent = message || '未检测到采集器登录态';
  }

  function showLoginCollectorWait(message) {
    clearLoginFrameTimer();
    hideAllLoginStates();
    $('loginCollectorWait').classList.remove('hidden');
    $('loginCollectorMsg').textContent = message || '正在打开采集器浏览器窗口，请扫码或账号登录…';
    $('loginModalMsg').textContent = '采集器浏览器已打开，等待登录完成…';
  }

  function handleLoginFrameLoad() {
    if (!state.loginFrameActive) return;  // 重置为 about:blank 等场景忽略
    clearLoginFrameTimer();
    let hasContent = false;
    try {
      const doc = $('loginFrame').contentDocument;
      hasContent = !!doc && !!doc.body && (doc.body.innerText || '').trim().length > 0;
    } catch (err) { hasContent = false; }
    if (hasContent) {
      hideAllLoginStates();
      $('loginFrame').classList.remove('hidden');
      $('loginModalMsg').textContent = '登录页已显示，请在页面内扫码登录（此方式仅当前浏览器可见，采集需采集器登录）';
    } else {
      showLoginFallback();
    }
  }

  async function startLogin() {
    const platform = state.platformMap[state.selected];
    stopLoginPoll();
    $('loginModalTitle').textContent = `登录${platform.name}`;
    showLoginLoading('正在加载平台登录页，请稍候…');
    const frame = $('loginFrame');
    state.loginFrameActive = true;
    frame.onload = handleLoginFrameLoad;
    frame.src = platform.login_url || platform.home_url;
    $('loginModal').classList.remove('hidden');
    loginFrameTimer = setTimeout(() => showLoginFallback(), 8000);
    setBadge('wait', '登录中');
    setNavStatus(platform.id, 'wait');
  }

  function retryInlineLogin() {
    const platform = state.platformMap[state.selected];
    showLoginLoading('正在重新加载登录页…');
    state.loginFrameActive = true;
    const frame = $('loginFrame');
    const url = platform.login_url || platform.home_url;
    const sep = url.includes('?') ? '&' : '?';
    frame.src = `${url}${sep}_retry=${Date.now()}`;
    loginFrameTimer = setTimeout(() => showLoginFallback(), 8000);
  }

  async function cancelLogin() {
    stopLoginPoll();
    clearLoginFrameTimer();
    state.loginFrameActive = false;
    $('loginFrame').src = 'about:blank';
    $('loginModal').classList.add('hidden');
    refreshStatus(false);
  }

  async function markLoginDone() {
    if (state.loginChecking) return;
    state.loginChecking = true;
    stopLoginPoll();
    state.loginFrameActive = false;  // 退出 iframe 流程，避免 about:blank 的 onload 覆盖反馈态
    const doneBtn = $('loginDoneBtn');
    const cancelBtn = $('loginCancelBtn');
    doneBtn.disabled = true;
    cancelBtn.disabled = true;
    showLoginLoading('正在检测采集器登录态，请稍候…');
    const checkingText = '正在检测采集器登录态…（首次检测需启动浏览器，约 10~40 秒）';
    $('loginModalMsg').textContent = checkingText;

    // 前端进度提示 + 超时保护，避免后端慢检测时按钮"无反应"
    const tips = [
      '仍在检测中…平台页面加载较慢，请耐心等待',
      '检测耗时较长，正在等待平台页面响应…',
      '若长时间无结果，可点击「用采集器浏览器登录」重新登录',
    ];
    let tipIdx = 0;
    const tipTimer = setInterval(() => {
      if (tipIdx < tips.length) $('loginModalMsg').textContent = tips[tipIdx++];
    }, 8000);
    let timeoutTimer = null;
    const timeoutPromise = new Promise((_, reject) => {
      timeoutTimer = setTimeout(() => reject(new Error('检测超时（超过 45 秒）')), 45000);
    });

    try {
      const status = await Promise.race([refreshStatus(true), timeoutPromise]);
      $('loginFrame').src = 'about:blank';
      if (status.logged_in) {
        $('loginModal').classList.add('hidden');
        toast(`${state.platformMap[state.selected].name} 登录状态已保存`, 'success');
      } else {
        showLoginCheckFail('当前浏览器内嵌登录的 Cookie 无法同步给采集器，采集器仍未登录。请用采集器浏览器完成登录。');
      }
    } catch (err) {
      showLoginCheckFail(`登录态检测失败：${err.message}。请点击「用采集器浏览器登录」重试。`);
    } finally {
      clearInterval(tipTimer);
      if (timeoutTimer) clearTimeout(timeoutTimer);
      state.loginChecking = false;
      doneBtn.disabled = false;
      cancelBtn.disabled = false;
      doneBtn.textContent = '我已完成登录';
    }
  }

  async function startCollectorLogin() {
    const platform = state.platformMap[state.selected];
    if (state.loginChecking) return;
    stopLoginPoll();
    state.loginFrameActive = false;
    $('loginFrame').src = 'about:blank';
    showLoginCollectorWait('正在打开采集器浏览器窗口，请稍候…');
    try {
      await api(`/api/platforms/${encodeURIComponent(state.selected)}/login`, { method: 'POST' });
      $('loginModalMsg').textContent = '采集器浏览器已打开，请在窗口中扫码或账号登录…';
      // 每 2 秒轮询登录任务，成功后自动关闭弹层并刷新状态
      state.loginPoll = setInterval(async () => {
        try {
          const task = await api(`/api/platforms/${encodeURIComponent(state.selected)}/login-task`);
          if (task.message) $('loginCollectorMsg').textContent = task.message;
          if (task.status === 'success') {
            stopLoginPoll();
            $('loginModal').classList.add('hidden');
            toast(`${platform.name} 登录成功，登录态已保存`, 'success');
            refreshStatus(false);
          } else if (['failed', 'timeout', 'cancelled'].includes(task.status)) {
            stopLoginPoll();
            showLoginCheckFail(task.message || '采集器登录未完成，请重试');
          }
        } catch (err) {
          $('loginCollectorMsg').textContent = err.message;
        }
      }, 2000);
    } catch (err) {
      showLoginCheckFail(`启动采集器浏览器失败：${err.message}`);
    }
  }

  function openLoginInTab() {
    const platform = state.platformMap[state.selected];
    window.open(platform.login_url || platform.home_url, '_blank', 'noopener');
  }

  async function clearLogin() {
    if (!confirm(`确定清除 ${state.platformMap[state.selected].name} 的登录态吗？下次需要重新扫码登录。`)) return;
    try {
      await api(`/api/platforms/${encodeURIComponent(state.selected)}/login`, { method: 'DELETE' });
      toast('登录态已清除', 'success');
      refreshStatus(true);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  function renderResults(data) {
    state.lastResponse = data;
    state.products = data.products || [];
    state.page = 1;
    $('resultCount').textContent = `${data.total} 条`;
    $('resultMeta').textContent = `${data.keyword || ''} · 耗时 ${data.elapsed}s${data.filtered ? '' : ' · 规格未命中，显示未过滤结果'}${data.stale ? ' · 限流中，展示缓存结果' : ''}${data.detail_fallback ? ' · 详情页直采' : ''}${data.run_id ? ` · 批次 ${data.run_id}` : ''}`;

    const bar = $('messageBar');
    if (data.message) {
      bar.textContent = data.message;
      bar.classList.remove('hidden');
    } else {
      bar.classList.add('hidden');
    }

    renderTable();
  }

  function renderPagination(total, pageCount) {
    $('pageInfo').textContent = `共 ${total} 条`;
    $('pageNum').textContent = `${state.page} / ${pageCount}`;
    $('firstPageBtn').disabled = $('prevPageBtn').disabled = state.page <= 1;
    $('nextPageBtn').disabled = $('lastPageBtn').disabled = state.page >= pageCount;
  }

  function renderTable() {
    const body = $('resultBody');
    const total = state.products.length;
    const size = state.pageSize;
    const pageCount = Math.max(1, Math.ceil(total / size));
    if (state.page > pageCount) state.page = pageCount;
    renderPagination(total, pageCount);

    if (!total) {
      body.innerHTML = '<tr class="empty-row"><td colspan="12">未采集到商品数据</td></tr>';
      return;
    }
    const start = (state.page - 1) * size;
    const pageItems = state.products.slice(start, start + size);
    const fmt = (v) => v == null ? '—' : `¥${Number(v).toFixed(2)}`;
    body.innerHTML = pageItems.map((p, i) => {
      const index = start + i + 1;
      const link = safeUrl(p.url);
      const cell = (value, width) => `<td title="${escapeHtml(value)}"${width ? ` style="max-width:${width}px"` : ''}>${escapeHtml(value) || '<span style="color:#9ca3af">—</span>'}</td>`;
      const promoPrice = p.promo_price != null
        ? `<span class="price-current">¥${Number(p.promo_price).toFixed(2)}</span>`
        : '<span style="color:#9ca3af">—</span>';
      const discount = p.discount_amount != null && p.discount_amount > 0
        ? `<span class="price-down">-¥${Number(p.discount_amount).toFixed(2)}</span>`
        : '<span style="color:#9ca3af">—</span>';
      const low = p.history_low != null ? fmt(p.history_low) : '—';
      const stock = p.stock || (p.stock_count != null ? String(p.stock_count) : '—');
      let promoTag = '—';
      if (p.promo_type) {
        const cls = p.promo_type.includes('0元') ? 'free' : p.promo_type === '优惠券' ? 'coupon' : '';
        promoTag = `<span class="promo-tag ${cls}" title="${escapeHtml(p.promo_label || p.promo_type)}">${escapeHtml(p.promo_type)}</span>`;
      }
      return `<tr>
        <td class="col-index">${index}</td>
        ${cell(p.platform_name)}
        ${cell(p.title, 300)}
        <td class="price">${p.list_price != null ? `<span class="price-strike">¥${Number(p.list_price).toFixed(2)}</span><br>` : ''}${escapeHtml(p.price) || '—'}</td>
        <td>${promoPrice}</td>
        <td>${discount}</td>
        <td>${low}</td>
        ${cell(stock, 90)}
        <td>${promoTag}</td>
        ${cell(p.shop, 120)}
        ${cell(p.sales, 90)}
        <td>${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">查看商品 ↗</a>` : '—'}</td>
      </tr>`;
    }).join('');
  }

  function clearResults() {
    state.products = [];
    state.lastResponse = null;
    state.page = 1;
    renderTable();
    $('resultBody').innerHTML = '<tr class="empty-row"><td colspan="12">暂无数据，请先搜索商品</td></tr>';
    $('resultCount').textContent = '0 条';
    $('resultMeta').textContent = '';
    $('messageBar').classList.add('hidden');
  }

  function csvField(value) {
    const text = String(value == null ? '' : value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function exportCsv() {
    if (!state.products.length) {
      toast('暂无数据可导出');
      return;
    }
    const header = ['序号', '平台', '商品标题', '当前价', '划线价', '优惠后价', '优惠金额', '历史低价', '库存', '优惠类型', '优惠标签', '店铺', '销量', '商品链接', '关键词', '采集时间'];
    const rows = state.products.map((p, i) => [
      i + 1, p.platform_name, p.title, p.price, p.list_price ?? '', p.promo_price ?? '',
      p.discount_amount ?? '', p.history_low ?? '', p.stock, p.promo_type, p.promo_label,
      p.shop, p.sales, p.url, p.keyword, p.crawled_at,
    ]);
    const csv = '\ufeff' + [header, ...rows].map((row) => row.map(csvField).join(',')).join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const ts = new Date().toISOString().slice(0, 10);
    a.download = `商品数据_${state.selected}_${ts}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
    toast('CSV 已导出', 'success');
  }

  async function doSearch(event) {
    event.preventDefault();
    const platform = state.platformMap[state.selected];
    if (!platform.enabled) {
      toast(`${platform.name} 适配器开发中，暂不支持采集`, 'error');
      return;
    }
    const keyword = $('keyword').value.trim();
    if (!keyword) { toast('请输入搜索关键词', 'error'); return; }

    const payload = {
      platform: state.selected,
      keyword,
      specs: $('specs').value.trim(),
      filters: collectFilters(),
      pages: Number($('pages').value) || 1,
      max_results: $('maxResults').value === '' ? 0 : Number($('maxResults').value),
    };
    if (!payload.filters) return;

    $('searchBtn').disabled = true;
    showLoading(true, `正在采集 ${platform.name} 的商品数据…`);
    try {
      const data = await api('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderResults(data);
      setSearchCollapsed(true);
      toast(`采集完成，共 ${data.total} 条`, 'success');
    } catch (err) {
      $('messageBar').textContent = err.message;
      $('messageBar').classList.remove('hidden');
      toast(err.message, 'error');
    } finally {
      showLoading(false);
      $('searchBtn').disabled = !platform.enabled;
    }
  }

  // -------------------------------------------------------------- 优惠监控
  let alertPoll = null;

  function switchView(view) {
    $('searchView').classList.toggle('hidden', view !== 'search');
    $('monitorView').classList.toggle('hidden', view !== 'monitor');
    $('diagView').classList.toggle('hidden', view !== 'diag');
    document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    if (view === 'monitor') { loadTasks(); loadAlerts(); }
    if (view === 'diag') { loadDiag(); }
  }

  async function loadDiag() {
    try {
      const data = await api('/api/crawl/diagnostics');
      const guard = data.guard || {};
      const parts = Object.entries(guard).map(([k, v]) =>
        `${k}: 请求${v.requests || 0} / 限流${v.limited || 0} / 缓存${v.cache_hits || 0} / 旧缓存${v.stale_hits || 0} / 冷却${v.cooldown_remaining || 0}s`
      );
      $('guardStats').textContent = parts.length ? parts.join('　|　') : '暂无请求记录';

      const proxies = data.proxies || [];
      const pBody = $('proxyBody');
      pBody.innerHTML = proxies.length ? proxies.map(p => {
        const state = p.healthy ? (p.cooldown_remaining > 0 ? '冷却中' : '可用') : '已禁用';
        return `<tr>
          <td>${escapeHtml(p.name || p.id)}</td>
          <td>${escapeHtml(p.ip || '—')}</td>
          <td>${state}</td>
          <td>${p.fail_count}</td>
          <td>${p.cooldown_remaining}s</td>
          <td><button class="btn btn-ghost" data-proxy-reset="${escapeHtml(p.id)}">重置</button></td>
        </tr>`;
      }).join('') : '<tr class="empty-row"><td colspan="6">未配置代理（直连）</td></tr>';
      pBody.querySelectorAll('[data-proxy-reset]').forEach(b => b.addEventListener('click', async () => {
        await api(`/api/proxies/${b.dataset.proxyReset}/reset`, { method: 'POST' });
        loadDiag(); toast('代理已重置', 'success');
      }));

      const runs = data.runs || [];
      $('runBody').innerHTML = runs.length ? runs.map(r => `<tr>
        <td>${escapeHtml(r.id)}</td>
        <td>${escapeHtml(r.platform)}</td>
        <td>${escapeHtml(r.keyword)}</td>
        <td>${escapeHtml(r.status)}</td>
        <td>${r.product_count}</td>
        <td>${r.elapsed}s</td>
        <td>${escapeHtml(r.proxy || '直连')}</td>
        <td>${escapeHtml(r.started_at)}</td>
      </tr>`).join('') : '<tr class="empty-row"><td colspan="8">暂无采集记录</td></tr>';
    } catch (err) { toast(err.message, 'error'); }
  }

  async function loadTasks() {
    try {
      const data = await api('/api/monitor/tasks');
      const tasks = data.tasks || [];
      const body = $('taskBody');
      if (!tasks.length) {
        body.innerHTML = '<tr class="empty-row"><td colspan="7">暂无监控任务</td></tr>';
        return;
      }
      body.innerHTML = tasks.map(t => {
        const statusCls = t.status === 'error' ? 'badge-no' : t.enabled ? 'badge-ok' : 'badge-idle';
        const statusText = !t.enabled ? '已暂停' : t.status === 'error' ? '异常' : '运行中';
        return `<tr>
          <td>${escapeHtml(t.platform_name || t.platform)}</td>
          <td>${escapeHtml(t.keyword)}</td>
          <td>${t.interval}s</td>
          <td><span class="badge ${statusCls}">${statusText}</span>${t.last_error ? `<div class="hint" title="${escapeHtml(t.last_error)}">${escapeHtml(t.last_error.slice(0,20))}</div>` : ''}</td>
          <td>${t.run_count}</td>
          <td>${escapeHtml(t.last_run_at || '—')}</td>
          <td>
            <button class="btn btn-ghost" data-toggle="${escapeHtml(t.id)}">${t.enabled ? '暂停' : '启动'}</button>
            <button class="btn btn-ghost danger" data-del="${escapeHtml(t.id)}">删除</button>
          </td>
        </tr>`;
      }).join('');
      body.querySelectorAll('[data-toggle]').forEach(b => b.addEventListener('click', async () => {
        await api(`/api/monitor/tasks/${b.dataset.toggle}/toggle`, { method: 'POST' });
        loadTasks();
      }));
      body.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {
        if (!confirm('删除该监控任务？')) return;
        await api(`/api/monitor/tasks/${b.dataset.del}`, { method: 'DELETE' });
        loadTasks(); toast('任务已删除', 'success');
      }));
    } catch (err) { toast(err.message, 'error'); }
  }

  const ALERT_ICONS = { price_drop: '↓', low_price: '★', promo: '促', coupon: '券', free_trial: '免', free_buy: '0' };
  const ALERT_LABELS = { price_drop: '降价', low_price: '历史低价', promo: '促销', coupon: '优惠券', free_trial: '0元试用', free_buy: '0元购' };

  async function loadAlerts() {
    try {
      const data = await api('/api/monitor/alerts?limit=100');
      const alerts = data.alerts || [];
      $('alertBadge').textContent = `${alerts.length} 条`;
      const pill = $('alertPill');
      $('alertCount').textContent = `${alerts.length} 条优惠`;
      pill.classList.toggle('has-alert', alerts.length > 0);
      const box = $('alertList');
      if (!alerts.length) { box.innerHTML = '<div class="empty-alert">暂无优惠消息</div>'; return; }
      box.innerHTML = alerts.map(a => {
        const icon = ALERT_ICONS[a.alert_type] || '促';
        const label = ALERT_LABELS[a.alert_type] || a.alert_type || '优惠';
        const price = a.new_price != null ? `¥${Number(a.new_price).toFixed(2)}` : '';
        const link = safeUrl(a.url);
        return `<div class="alert-item ${a.is_read ? '' : 'unread'}">
          <div class="alert-icon ${escapeHtml(a.alert_type)}">${icon}</div>
          <div class="alert-body">
            <div class="alert-title">${escapeHtml(a.message || a.title)}</div>
            <div class="alert-meta">${escapeHtml(a.platform_name || a.platform)} · ${escapeHtml(label)} · ${escapeHtml(a.created_at)}</div>
          </div>
          ${price ? `<div class="alert-price">${price}</div>` : ''}
          ${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener">查看 ↗</a>` : ''}
        </div>`;
      }).join('');
    } catch (err) { /* 静默 */ }
  }

  function openTaskModal() {
    const sel = $('taskPlatform');
    sel.innerHTML = state.platforms.filter(p => p.enabled).map(p =>
      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join('');
    $('taskModal').classList.remove('hidden');
  }

  async function saveTask() {
    const keyword = $('taskKeyword').value.trim();
    if (!keyword) { toast('请输入监控关键词', 'error'); return; }
    try {
      await api('/api/monitor/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform: $('taskPlatform').value,
          keyword,
          specs: $('taskSpecs').value.trim(),
          interval: Number($('taskInterval').value) || 300,
        }),
      });
      $('taskModal').classList.add('hidden');
      $('taskKeyword').value = '';
      toast('监控任务已创建', 'success');
      loadTasks();
    } catch (err) { toast(err.message, 'error'); }
  }

  async function init() {
    $('loginBtn').addEventListener('click', startLogin);
    $('refreshLoginBtn').addEventListener('click', () => refreshStatus(true).then(() => toast('登录态已更新')).catch((e) => toast(e.message, 'error')));
    $('clearLoginBtn').addEventListener('click', clearLogin);
    $('loginCancelBtn').addEventListener('click', cancelLogin);
    $('loginDoneBtn').addEventListener('click', markLoginDone);
    $('loginCheckRetryBtn').addEventListener('click', markLoginDone);
    $('loginCollectorBtn').addEventListener('click', startCollectorLogin);
    $('loginCollectorBtn2').addEventListener('click', startCollectorLogin);
    $('loginOpenTabBtn').addEventListener('click', openLoginInTab);
    $('loginRetryBtn').addEventListener('click', retryInlineLogin);
    $('searchForm').addEventListener('submit', doSearch);
    $('searchToggleBtn').addEventListener('click', () => {
      const collapsed = !$('searchForm').classList.contains('hidden');
      setSearchCollapsed(collapsed);
    });
    $('exportBtn').addEventListener('click', exportCsv);
    $('clearBtn').addEventListener('click', clearResults);
    $('pageSize').addEventListener('change', (e) => {
      state.pageSize = Number(e.target.value) || 100;
      state.page = 1;
      renderTable();
    });
    $('firstPageBtn').addEventListener('click', () => { state.page = 1; renderTable(); });
    $('prevPageBtn').addEventListener('click', () => { if (state.page > 1) { state.page--; renderTable(); } });
    $('nextPageBtn').addEventListener('click', () => { state.page++; renderTable(); });
    $('lastPageBtn').addEventListener('click', () => {
      state.page = Math.max(1, Math.ceil(state.products.length / state.pageSize));
      renderTable();
    });

    // 监控相关
    document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
    $('alertPill').addEventListener('click', () => switchView('monitor'));
    $('refreshDiagBtn').addEventListener('click', () => loadDiag());
    $('probeProxiesBtn').addEventListener('click', async () => {
      await api('/api/proxies/probe', { method: 'POST', body: '{}' });
      loadDiag(); toast('代理探测完成', 'success');
    });
    $('addTaskBtn').addEventListener('click', openTaskModal);
    $('taskCancelBtn').addEventListener('click', () => $('taskModal').classList.add('hidden'));
    $('taskSaveBtn').addEventListener('click', saveTask);
    $('readAllBtn').addEventListener('click', async () => {
      await api('/api/monitor/alerts/read-all', { method: 'POST' });
      loadAlerts();
    });
    $('clearAlertsBtn').addEventListener('click', async () => {
      if (!confirm('清空所有优惠消息？')) return;
      await api('/api/monitor/alerts', { method: 'DELETE' });
      loadAlerts();
    });

    // 定时轮询告警
    loadAlerts();
    alertPoll = setInterval(loadAlerts, 15000);

    try {
      const data = await api('/api/platforms');
      state.platforms = data.platforms || [];
      state.platformMap = Object.fromEntries(state.platforms.map((p) => [p.id, p]));
      const first = state.platforms.find((p) => p.enabled) || state.platforms[0];
      if (first) selectPlatform(first.id);
      $('servicePill').className = 'pill online';
      $('servicePill').innerHTML = '<span class="dot"></span>服务在线';
    } catch (err) {
      $('servicePill').className = 'pill offline';
      $('servicePill').innerHTML = '<span class="dot"></span>服务不可用';
      toast('无法连接后端服务', 'error');
    }
  }

  init();
})();
