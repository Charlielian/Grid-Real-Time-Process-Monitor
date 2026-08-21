(() => {
  const page = document.querySelector('.pending-page');
  const rows = document.querySelector('#pending-rows');
  const message = document.querySelector('#pending-message');
  const refresh = document.querySelector('#pending-refresh');
  if (!page || !rows || !message || !refresh) return;

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
      button.textContent = '人工领取';
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
    if (!automatic) message.textContent = '正在加载…';
    try {
      const result = await request(queryUrl(), {signal: controller.signal});
      refreshFailures = 0;
      render(result.items || []);
      message.textContent = `共 ${Number(result.total || 0)} 条待领取任务`;
      if (autoClaimEnabled && allowAutoClaim && !autoClaimPaused && result.items?.length) {
        message.textContent = `自动领取 ${result.items.length} 条任务…`;
        try {
          await claimIds(result.items.map((task) => task.task_id));
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

  refresh.addEventListener('click', async () => {
    refreshFailures = 0;
    await load();
    schedule();
  });
  rows.addEventListener('click', (event) => {
    const button = event.target.closest('.pending-claim');
    if (button) claim(button);
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
