(() => {
  const button = document.querySelector('#sync-now');
  const status = document.querySelector('#sync-status');
  if (!button) return;
  const csrf = document.querySelector('meta[name=csrf-token]')?.content || '';
  button.addEventListener('click', async () => {
    button.disabled = true; status.textContent = '正在提交同步任务…';
    const response = await fetch('/api/v1/sync', { method: 'POST', headers: {'X-CSRF-Token': csrf} });
    const result = await response.json();
    if (!response.ok) { status.textContent = result.message || '同步提交失败'; button.disabled = false; return; }
    const timer = setInterval(async () => {
      const current = await (await fetch(`/api/v1/sync/${result.job_id}`)).json();
      status.textContent = `${current.message} (${current.progress}%)`;
      if (!['queued', 'running'].includes(current.status)) { clearInterval(timer); button.disabled = false; if (current.status === 'succeeded') window.location.reload(); }
    }, 1000);
  });
})();
