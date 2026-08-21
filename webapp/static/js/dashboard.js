(() => {
  const button = document.querySelector('#sync-now');
  const status = document.querySelector('#sync-status');
  if (!button) return;
  const {request} = window.GridApi;
  let timer = null;
  let jobId = null;
  let stopped = false;

  const stop = () => {
    if (timer) window.clearTimeout(timer);
    timer = null;
  };
  const poll = async () => {
    if (stopped || !jobId) return;
    try {
      const current = await request(`/api/v1/sync/${encodeURIComponent(jobId)}`);
      status.textContent = `${current.message || '同步中…'} (${current.progress ?? 0}%)`;
      if (['queued', 'running'].includes(current.status)) {
        timer = window.setTimeout(poll, 1000);
        return;
      }
      if (current.status === 'succeeded') window.location.reload();
      else status.textContent = current.error || '同步未完成';
      button.disabled = false;
    } catch (error) {
      status.textContent = error.message || '同步状态查询失败';
      timer = window.setTimeout(poll, 2000);
    }
  };
  button.addEventListener('click', async () => {
    stopped = false;
    stop();
    button.disabled = true;
    status.textContent = '正在提交同步任务…';
    try {
      const result = await request('/api/v1/sync', {method: 'POST', retries: 0});
      jobId = result.job_id;
      await poll();
    } catch (error) {
      status.textContent = error.message || '同步提交失败';
      button.disabled = false;
    }
  });
  window.addEventListener('pagehide', () => { stopped = true; stop(); });
})();
