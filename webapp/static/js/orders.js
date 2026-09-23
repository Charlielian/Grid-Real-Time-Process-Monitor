/*
 * 工单列表的定时刷新控制器。
 *
 * 直接按当前 URL 的筛选参数 GET /api/v1/orders，成功后只重建 #orders-rows 和
 * 总数，不整页刷新，从而保留当前筛选条件和页面滚动位置。auto_sync 关闭时只
 * 在页面加载时查询一次。
 */
(() => {
  const page = document.querySelector('.orders-page');
  const status = document.querySelector('#orders-refresh-status');
  const rows = document.querySelector('#orders-rows');
  const total = document.querySelector('#orders-total');
  if (!page || !status || !rows || !total) return;

  const interval = Math.max(5, Number.parseInt(page.dataset.pollInterval || '60', 10) || 60);
  const autoSync = page.dataset.autoSync === 'true';
  const {request} = window.GridApi;
  let remaining = interval;
  let timer = null;
  let refreshing = false;

  const clearTimers = () => {
    if (timer) window.clearInterval(timer);
    timer = null;
  };
  const setCountdown = () => {
    if (!autoSync) return;
    status.textContent = `下次刷新：${remaining} 秒`;
  };
  const refreshOrders = async () => {
    if (refreshing) return;
    refreshing = true;
    try {
      const query = new URLSearchParams(window.location.search);
      const result = await request(`/api/v1/orders${query.toString() ? `?${query.toString()}` : ''}`);
      rows.replaceChildren();
      for (const item of result.items || []) {
        const row = document.createElement('tr');
        for (const value of [item.number, item.title, item.current_node, item.status, item.assignee, item.created_at]) {
          const cell = document.createElement('td');
          cell.textContent = value || '';
          row.append(cell);
        }
        const actionCell = document.createElement('td');
        const link = document.createElement('a');
        link.href = `/orders/${encodeURIComponent(item.order_id)}`;
        link.textContent = '详情';
        actionCell.append(link);
        row.append(actionCell);
        rows.append(row);
      }
      if (!result.items?.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 7;
        cell.className = 'muted';
        cell.textContent = '暂无工单';
        row.append(cell);
        rows.append(row);
      }
      total.textContent = String(result.total ?? 0);
    } catch (error) {
      status.textContent = error.message || '工单列表刷新失败，请稍后重试';
    } finally {
      refreshing = false;
    }
  };
  const restartCountdown = () => {
    remaining = interval;
    clearTimers();
    setCountdown();
    if (!autoSync) return;
    timer = window.setInterval(() => {
      if (document.hidden) return;
      remaining -= 1;
      if (remaining <= 0) {
        clearTimers();
        refreshOrders().finally(restartCountdown);
      } else setCountdown();
    }, 1000);
  };

  window.addEventListener('pagehide', clearTimers);
  restartCountdown();
})();