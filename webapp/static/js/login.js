(() => {
  const form = document.querySelector('#login-form');
  const image = document.querySelector('#captcha-image');
  const message = document.querySelector('#message');
  const csrf = () => form.querySelector('[name=csrf_token]').value;
  const setMessage = (text, error = false) => { message.textContent = text; message.className = error ? 'flash error' : 'muted'; };
  const postJson = async (url, body = undefined) => {
    const options = {method: 'POST', headers: {'X-CSRF-Token': csrf()}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || '请求失败');
    return result;
  };
  document.querySelectorAll('.restore-account').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      setMessage('正在恢复保存的登录会话…');
      try {
        const result = await postJson('/auth/restore', {login_id: button.dataset.loginId});
        window.location.href = result.redirect;
      } catch (error) {
        setMessage(error.message || '保存的登录会话已失效，请重新登录', true);
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll('.heartbeat-account').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await postJson(`/auth/heartbeat/${encodeURIComponent(button.dataset.loginId)}`);
        const row = button.closest('.saved-account');
        row.querySelector('.heartbeat-status').textContent = `状态：${result.account.heartbeat_status}`;
        setMessage(result.account.heartbeat_status === 'healthy' ? '会话仍然有效' : '该会话已失效，请重新登录', result.account.heartbeat_status !== 'healthy');
      } catch (error) {
        setMessage(error.message || '会话检查失败', true);
      } finally {
        button.disabled = false;
      }
    });
  });
  const refresh = () => { image.src = `/auth/captcha?ts=${Date.now()}`; };
  document.querySelector('#refresh-captcha').addEventListener('click', refresh);
  document.querySelector('#verify-captcha').addEventListener('click', async () => {
    const body = Object.fromEntries(new FormData(form));
    const result = await postJson('/auth/captcha/verify', body);
    setMessage(result.message, !result.ok);
    document.querySelector('#send-sms').disabled = !result.ok;
  });
  document.querySelector('#send-sms').addEventListener('click', async (event) => {
    const body = Object.fromEntries(new FormData(form));
    const button = event.currentTarget; button.disabled = true;
    const result = await postJson('/auth/sms', body); setMessage(result.message, !result.ok);
    if (!result.ok) { button.disabled = false; return; }
    let left = 60; button.textContent = `${left}s 后重发`;
    const timer = setInterval(() => { left -= 1; button.textContent = `${left}s 后重发`; if (left <= 0) { clearInterval(timer); button.textContent = '发送短信验证码'; button.disabled = false; } }, 1000);
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    try {
      const result = await postJson('/auth/login', body);
      window.location.href = result.redirect;
    } catch (error) {
      setMessage(error.message || '登录失败', true); refresh();
    }
  });
  refresh();
})();
