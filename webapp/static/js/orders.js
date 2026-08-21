(() => {
  const page = document.querySelector('.orders-page');
  const status = document.querySelector('#orders-refresh-status');
  const bubble = document.querySelector('#new-orders-bubble');
  const message = document.querySelector('#new-orders-message');
  if (!page || !status || !bubble || !message) return;

  const interval = Math.max(5, Number.parseInt(page.dataset.pollInterval || '60', 10) || 60);
  const autoSync = page.dataset.autoSync === 'true';
  const {request} = window.GridApi;
  let remaining = interval;
  let timer = null;
  let statusTimer = null;
  let currentJobId = null;
  let polling = false;
  let syncing = false;
  let pollFailures = 0;
  const maxPollFailures = 5;

  const clearTimers = () => {
    if (timer) window.clearInterval(timer);
    if (statusTimer) window.clearTimeout(statusTimer);
    timer = null;
    statusTimer = null;
  };
  const setCountdown = () => {
    if (!autoSync || syncing) return;
    status.textContent = `下次刷新：${remaining} 秒`;
  };
  const restartCountdown = () => {
    remaining = interval;
    syncing = false;
    currentJobId = null;
    polling = false;
    pollFailures = 0;
    clearTimers();
    setCountdown();
    if (!autoSync) return;
    timer = window.setInterval(() => {
      if (document.hidden || syncing) return;
      remaining -= 1;
      if (remaining <= 0) {
        clearTimers();
        startSync();
      } else setCountdown();
    }, 1000);
  };
  const showNewOrders = (count) => {
    message.textContent = `发现 ${count} 条新工单，点击查看`;
    bubble.hidden = false;
  };
  const pollJob = async (jobId) => {
    if (jobId !== currentJobId || polling || document.hidden) return;
    polling = true;
    try {
      const current = await request(`/api/v1/sync/${encodeURIComponent(jobId)}`);
      pollFailures = 0;
      status.textContent = current.message || '同步中…';
      if (['queued', 'running'].includes(current.status)) {
        polling = false;
        statusTimer = window.setTimeout(() => pollJob(jobId), 1500);
        return;
      }
      polling = false;
      if (current.status === 'succeeded') {
        const added = Number(current.summary?.added || 0);
        if (added > 0) showNewOrders(added);
      } else if (current.status === 'failed' || current.status === 'cancelled') {
        status.textContent = current.error || '同步未完成，将稍后重试';
      }
      restartCountdown();
    } catch (error) {
      polling = false;
      pollFailures += 1;
      status.textContent = error.message || '同步状态查询失败';
      if (pollFailures >= maxPollFailures) {
        restartCountdown();
      } else {
        statusTimer = window.setTimeout(() => pollJob(jobId), Math.min(10000, 1000 * (2 ** (pollFailures - 1))));
      }
    }
  };
  const startSync = async () => {
    if (!autoSync || syncing) return;
    syncing = true;
    status.textContent = '正在同步工单…';
    try {
      const result = await request('/api/v1/sync', {method: 'POST', retries: 0});
      currentJobId = result.job_id;
      pollFailures = 0;
      pollJob(currentJobId);
    } catch (error) {
      status.textContent = error.message || '同步提交失败，将稍后重试';
      restartCountdown();
    }
  };

  bubble.addEventListener('click', () => { bubble.hidden = true; window.location.reload(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !syncing || !currentJobId) return;
    if (statusTimer) window.clearTimeout(statusTimer);
    statusTimer = null;
    polling = false;
    status.textContent = '正在恢复同步状态…';
    pollJob(currentJobId);
  });
  window.addEventListener('pagehide', clearTimers);
  if (autoSync) restartCountdown();
})();
