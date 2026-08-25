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
  if (!page || !rows || !message || !refresh || !claimAll) return;

  const autoClaimEnabled = page.dataset.autoClaim === 'true';
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
  });
  load().finally(schedule);
})();