(() => {
  const page = document.querySelector('.pending-page');
  const rows = document.querySelector('#pending-rows');
  const message = document.querySelector('#pending-message');
  const refresh = document.querySelector('#pending-refresh');
  if (!page || !rows || !message || !refresh) return;
  const autoClaim = page.dataset.autoClaim === 'true';
  const pollInterval = Math.max(5, Number(page.dataset.pollInterval || 60)) * 1000;
  const {request} = window.GridApi;
  let timer = null;
  let inFlight = false;
  let autoClaimFailures = 0;
  const maxAutoClaimFailures = 3;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const claimIds = (taskIds) => request('/api/v1/pending-tasks/claim', {
    method: 'POST',
    body: {task_ids: taskIds},
    retries: 0,
  });
  const load = async ({automatic = false} = {}) => {
    if (inFlight || document.hidden) return;
    inFlight = true;
    refresh.disabled = true;
    if (!automatic) message.textContent = '正在加载…';
    try {
      const result = await request(`/api/v1/pending-tasks?page_size=${encodeURIComponent(page.dataset.pageSize || '50')}`);
      if (autoClaim && result.items?.length) {
        message.textContent = `自动领取 ${result.items.length} 条任务…`;
        try {
          await claimIds(result.items.map((task) => task.task_id));
          autoClaimFailures = 0;
          message.textContent = '自动领取成功，正在刷新…';
          inFlight = false;
          return await load({automatic: true});
        } catch (error) {
          autoClaimFailures += 1;
          message.textContent = autoClaimFailures >= maxAutoClaimFailures
            ? `${error.message || '自动领取失败'}，已暂停自动领取，请手动刷新`
            : (error.message || '自动领取失败，将稍后重试');
          if (autoClaimFailures >= maxAutoClaimFailures) clearTimeout(timer);
          return;
        }
      }
      autoClaimFailures = 0;
      rows.replaceChildren();
      if (!result.items?.length) {
        rows.innerHTML = '<tr><td colspan="6" class="muted">暂无待领取任务</td></tr>';
      } else {
        result.items.forEach((task) => {
          const row = document.createElement('tr');
          row.dataset.taskId = task.task_id;
          row.innerHTML = `<td>${esc(task.number)}</td><td>${esc(task.title)}</td><td>${esc(task.current_node)}</td><td>${esc(task.created_at)}</td><td>${esc(task.due_at)}</td><td><button type="button" class="button pending-claim" data-task-id="${esc(task.task_id)}">人工领取</button></td>`;
          rows.appendChild(row);
        });
      }
      message.textContent = `共 ${Number(result.total || 0)} 条待领取任务`;
    } catch (error) {
      message.textContent = error.message || '待领取任务加载失败';
      if (!automatic) rows.innerHTML = '<tr><td colspan="6" class="muted">加载失败，请稍后重试</td></tr>';
    } finally {
      inFlight = false;
      refresh.disabled = false;
    }
  };
  const claim = async (button) => {
    button.disabled = true;
    button.textContent = '领取中…';
    try {
      const result = await claimIds([button.dataset.taskId]);
      button.closest('tr')?.remove();
      if (!rows.querySelector('tr')) rows.innerHTML = '<tr><td colspan="6" class="muted">暂无待领取任务</td></tr>';
      message.textContent = result.message || '领取成功';
    } catch (error) {
      button.disabled = false;
      button.textContent = '人工领取';
      message.textContent = error.message || '领取失败';
    }
  };
  const schedule = () => {
    if (!autoClaim || document.hidden || autoClaimFailures >= maxAutoClaimFailures) return;
    clearTimeout(timer);
    const delay = autoClaimFailures ? Math.min(60000, pollInterval * (2 ** autoClaimFailures)) : pollInterval;
    timer = setTimeout(async () => { await load({automatic: true}); schedule(); }, delay);
  };
  refresh.addEventListener('click', () => { autoClaimFailures = 0; load(); schedule(); });
  rows.addEventListener('click', (event) => { const button = event.target.closest('.pending-claim'); if (button) claim(button); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(timer); timer = null; }
    else { autoClaimFailures = 0; load({automatic: autoClaim}); schedule(); }
  });
  window.addEventListener('beforeunload', () => clearTimeout(timer));
  load().finally(schedule);
})();
