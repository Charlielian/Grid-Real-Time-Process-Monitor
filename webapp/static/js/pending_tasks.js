/*
 * 待领取任务列表控制器。
 *
 * 普通刷新和自动领取是两条独立流程：网络失败或自动领取熔断只会影响对应动作，
 * 不会阻止列表继续刷新。渲染使用 DOM API/textContent，避免把上游标题直接拼接
 * 到 HTML；页面隐藏时取消请求，恢复可见后再补偿加载当前 URL 的筛选结果。
 *
 * 领取权限由后端统一控制：只有标题匹配 target_title_keywords（默认阳江）的
 * 任务才可以领取，API 返回的 claimable_ids 字段标明哪些允许领取。自动领取、
 * 一键领取和单个领取按钮都受此约束。
 */
(() => {
  const page = document.querySelector('.pending-page');
  const rows = document.querySelector('#pending-rows');
  const message = document.querySelector('#pending-message');
  const refresh = document.querySelector('#pending-refresh');
  const claimAll = document.querySelector('#pending-claim-all');
  const autoClaimToggle = document.querySelector('#pending-auto-claim-toggle');
  const statsPanel = document.querySelector('#auto-claim-stats-panel');
  if (!page || !rows || !message || !refresh || !claimAll || !autoClaimToggle || !statsPanel) return;

  let autoClaimEnabled = page.dataset.autoClaim === 'true';
  let autoClaimTimer = null;
  const configuredPollInterval = Number(page.dataset.pollInterval);
  const pollInterval = (Number.isFinite(configuredPollInterval) && configuredPollInterval > 0
    ? Math.max(5, configuredPollInterval)
    : 60) * 1000;
  const {request} = window.GridApi;
  let timer = null;
  let inFlight = false;
  let controller = null;
  let autoClaimFailures = 0;
  let refreshFailures = 0;
  let autoClaimPaused = false;
  let reloadRequested = false;
  // claimable_ids tracks which task IDs the backend says are OK to claim.
  let claimableIds = [];
  const maxAutoClaimFailures = 3;

  // ---- 统计面板 ----
  const statsTotal = document.querySelector('#stats-total');
  const statsSession = document.querySelector('#stats-session');
  const statsLast = document.querySelector('#stats-last');
  const statsAccountsBody = document.querySelector('#stats-accounts-body');
  const statsHistoryBody = document.querySelector('#stats-history-body');

  const formatLastTime = (iso) => {
    if (!iso) return '暂无';
    const when = new Date(iso);
    if (!Number.isFinite(when.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
  };

  const renderStats = (s) => {
    const data = s || {};
    statsPanel.hidden = false;
    statsTotal.textContent = `${Number(data.total_claimed || 0)} 条`;
    statsSession.textContent = `${Number(data.session_claimed || 0)} 条`;
    const last = data.last_claim;
    const lastText = formatLastTime(last && last.time);
    statsLast.textContent = lastText;
    let recent = false;
    if (last && last.time) {
      const when = new Date(last.time);
      if (Number.isFinite(when.getTime())) recent = (Date.now() - when.getTime()) <= 30 * 60 * 1000;
    }
    statsLast.classList.toggle('recent', recent);

    const perAccount = data.per_account || {};
    const accountEntries = Object.entries(perAccount).sort((a, b) => b[1] - a[1]);
    statsAccountsBody.replaceChildren();
    if (!accountEntries.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 2;
      td.className = 'muted';
      td.textContent = '暂无领取记录';
      tr.append(td);
      statsAccountsBody.append(tr);
    } else {
      for (const [loginId, count] of accountEntries) {
        const tr = document.createElement('tr');
        for (const value of [loginId, `${count} 条`]) {
          const td = document.createElement('td');
          td.textContent = value;
          tr.append(td);
        }
        statsAccountsBody.append(tr);
      }
    }

    const history = data.history || [];
    statsHistoryBody.replaceChildren();
    if (!history.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      td.className = 'muted';
      td.textContent = '暂无领取记录';
      tr.append(td);
      statsHistoryBody.append(tr);
    } else {
      for (const item of history) {
        const tr = document.createElement('tr');
        for (const value of [formatLastTime(item.time), item.login_id, `${item.count} 条`]) {
          const td = document.createElement('td');
          td.textContent = value;
          tr.append(td);
        }
        statsHistoryBody.append(tr);
      }
    }
  };

  const loadStats = async () => {
    try {
      const s = await request('/api/v1/auto-claim-stats', {signal: new AbortController().signal});
      renderStats(s);
    } catch {
      // 统计为辅助信息，加载失败不打断主流程，面板保持上次内容。
    }
  };

  const queryUrl = () => {
    const query = new URLSearchParams(window.location.search);
    if (!query.has('page_size')) query.set('page_size', page.dataset.pageSize || '50');
    return `/api/v1/pending-tasks?${query.toString()}`;
  };
  const render = (items) => {
    rows.replaceChildren();
    if (!items?.length) {
      const empty = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 6;
      cell.className = 'muted';
      cell.textContent = '暂无待领取任务';
      empty.append(cell);
      rows.append(empty);
      return;
    }
    items.forEach((task) => {
      const claimable = claimableIds.includes(task.task_id);
      const row = document.createElement('tr');
      row.dataset.taskId = task.task_id;
      for (const value of [task.number, task.title, task.current_node, task.created_at, task.due_at]) {
        const cell = document.createElement('td');
        cell.textContent = value || '';
        row.append(cell);
      }
      const actionCell = document.createElement('td');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button pending-claim';
      button.dataset.taskId = task.task_id;
      if (claimable) {
        button.textContent = '人工领取';
      } else {
        button.textContent = '不可领取';
        button.disabled = true;
        button.style.opacity = '0.4';
      }
      actionCell.append(button);
      row.append(actionCell);
      rows.append(row);
    });
  };
  const claimIds = (taskIds) => request('/api/v1/pending-tasks/claim', {
    method: 'POST',
    body: {task_ids: taskIds},
    retries: 0,
  });
  const load = async ({automatic = false, allowAutoClaim = true} = {}) => {
    if (inFlight || document.hidden) return;
    inFlight = true;
    controller = new AbortController();
    refresh.disabled = true;
    claimAll.disabled = true;
    if (!automatic) message.textContent = '正在加载…';
    try {
      const result = await request(queryUrl(), {signal: controller.signal});
      refreshFailures = 0;
      claimableIds = result.claimable_ids || [];
      render(result.items || []);
      const claimableCount = claimableIds.length;
      message.textContent = `共 ${Number(result.total || 0)} 条待领取任务（其中 ${claimableCount} 条可领取）`;
      const claimableItems = (result.items || []).filter((t) => claimableIds.includes(t.task_id));
      if (autoClaimEnabled && allowAutoClaim && !autoClaimPaused && claimableItems.length) {
        message.textContent = `自动领取 ${claimableItems.length} 条任务…`;
        try {
          await claimIds(claimableItems.map((task) => task.task_id));
          autoClaimFailures = 0;
          message.textContent = '自动领取成功，正在刷新…';
          reloadRequested = true;
        } catch (error) {
          autoClaimFailures += 1;
          if (autoClaimFailures >= maxAutoClaimFailures) autoClaimPaused = true;
          message.textContent = autoClaimPaused
            ? `${error.message || '自动领取失败'}，已暂停自动领取，列表仍会继续刷新`
            : (error.message || '自动领取失败，将稍后重试');
        }
      }
      loadStats();
    } catch (error) {
      if (error.code === 'timeout' || error.code === 'network') refreshFailures += 1;
      message.textContent = error.message || '待领取任务加载失败';
      if (!automatic) render([]);
    } finally {
      controller = null;
      inFlight = false;
      refresh.disabled = false;
      claimAll.disabled = false;
      if (reloadRequested && !document.hidden) {
        reloadRequested = false;
        await load({automatic: true, allowAutoClaim: false});
      }
    }
  };
  const schedule = () => {
    clearTimeout(timer);
    if (document.hidden) return;
    const delay = Math.min(120000, pollInterval * (2 ** Math.min(refreshFailures, 4)));
    timer = setTimeout(async () => {
      await load({automatic: true});
      loadStats();
      schedule();
    }, delay);
  };
  const claim = async (button) => {
    button.disabled = true;
    button.textContent = '领取中…';
    try {
      const result = await claimIds([button.dataset.taskId]);
      message.textContent = result.message || '领取成功，正在刷新…';
      await load();
    } catch (error) {
      button.disabled = false;
      button.textContent = '人工领取';
      message.textContent = error.message || '领取失败';
    }
  };
  const startAutoClaim = () => {
    if (autoClaimTimer) return;
    autoClaimEnabled = true;
    autoClaimPaused = false;
    autoClaimFailures = 0;
    autoClaimToggle.textContent = '停止自动领取';
    autoClaimToggle.classList.add('active');
    message.textContent = '自动领取已启动，每 60 秒轮询一次';
    autoClaimTimer = setInterval(async () => {
      if (document.hidden) return;
      try {
        await load({automatic: true, allowAutoClaim: true});
      } catch {
        // individual load errors are handled inside load()
      }
    }, 60000);
  };
  const stopAutoClaim = () => {
    if (autoClaimTimer) {
      clearInterval(autoClaimTimer);
      autoClaimTimer = null;
    }
    autoClaimEnabled = false;
    autoClaimToggle.textContent = '启动自动领取';
    autoClaimToggle.classList.remove('active');
    message.textContent = '自动领取已停止';
  };
  const toggleAutoClaim = () => {
    if (autoClaimTimer) {
      stopAutoClaim();
    } else {
      startAutoClaim();
    }
  };
  const claimAllTasks = async () => {
    const ids = claimableIds;
    if (!ids.length) {
      message.textContent = '没有可领取的任务';
      return;
    }
    claimAll.disabled = true;
    message.textContent = `正在一键领取 ${ids.length} 条任务…`;
    try {
      const result = await claimIds(ids);
      message.textContent = result.message || '一键领取成功，正在刷新…';
      await load();
    } catch (error) {
      message.textContent = error.message || '一键领取失败';
    } finally {
      claimAll.disabled = false;
    }
  };

  refresh.addEventListener('click', async () => {
    refreshFailures = 0;
    await load();
    schedule();
  });
  claimAll.addEventListener('click', claimAllTasks);
  autoClaimToggle.addEventListener('click', toggleAutoClaim);
  rows.addEventListener('click', (event) => {
    const button = event.target.closest('.pending-claim');
    if (button && !button.disabled) claim(button);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
      controller?.abort();
      return;
    }
    refreshFailures = 0;
    load({automatic: true}).finally(schedule);
  });
  window.addEventListener('beforeunload', () => {
    clearTimeout(timer);
    controller?.abort();
    stopAutoClaim();
  });
  load().finally(schedule);
})();