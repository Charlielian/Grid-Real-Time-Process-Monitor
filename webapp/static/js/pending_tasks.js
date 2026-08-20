(() => {
  const page = document.querySelector('.pending-page');
  const rows = document.querySelector('#pending-rows');
  const message = document.querySelector('#pending-message');
  const refresh = document.querySelector('#pending-refresh');
  if (!page || !rows || !message || !refresh) return;
  const csrf = document.querySelector('meta[name=csrf-token]')?.content || '';
  const autoClaim = page.dataset.autoClaim === 'true';
  const pollInterval = Math.max(5, Number(page.dataset.pollInterval || 60)) * 1000;
  let timer = null;
  let inFlight = false;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const claimIds = async (taskIds) => {
    const response = await fetch('/api/v1/pending-tasks/claim', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({task_ids: taskIds}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || '领取失败');
    return result;
  };
  const load = async ({automatic = false} = {}) => {
    if (inFlight || document.hidden) return;
    inFlight = true;
    refresh.disabled = true;
    if (!automatic) message.textContent = '正在加载…';
    try {
      const response = await fetch(`/api/v1/pending-tasks?page_size=${encodeURIComponent(page.dataset.pageSize || '50')}`, {cache: 'no-store'});
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || '待领取任务加载失败');
      if (autoClaim && result.items?.length) {
        message.textContent = `自动领取 ${result.items.length} 条任务…`;
        await claimIds(result.items.map((task) => task.task_id));
        message.textContent = '自动领取成功，正在刷新…';
        inFlight = false;
        return await load({automatic: true});
      }
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
    if (!autoClaim || document.hidden) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      await load({automatic: true});
      schedule();
    }, pollInterval);
  };
  refresh.addEventListener('click', () => load());
  rows.addEventListener('click', (event) => {
    const button = event.target.closest('.pending-claim');
    if (button) claim(button);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else {
      load({automatic: autoClaim});
      schedule();
    }
  });
  window.addEventListener('beforeunload', () => clearTimeout(timer));
  load().finally(schedule);
})();
