(() => {
  class ApiError extends Error {
    constructor(message, {status = 0, code = 'request_failed', cause = undefined} = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.cause = cause;
    }
  }

  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const csrf = () => document.querySelector('meta[name=csrf-token]')?.content || '';
  const redirectToLogin = () => {
    if (window.location.pathname === '/login') return;
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`/login?next=${encodeURIComponent(next)}`);
  };

  const isBodyInit = (body) => {
    if (body == null || typeof body === 'string') return true;
    if (typeof Body !== 'undefined' && body instanceof Body) return true;
    if (typeof FormData !== 'undefined' && body instanceof FormData) return true;
    if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) return true;
    if (typeof Blob !== 'undefined' && body instanceof Blob) return true;
    if (typeof ArrayBuffer !== 'undefined' && (body instanceof ArrayBuffer || ArrayBuffer.isView(body))) return true;
    return false;
  };

  const request = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : 10000;
    const retries = Number.isFinite(options.retries) ? Math.max(0, options.retries) : 2;
    const retryable = options.retry === true || (options.retry !== false && ['GET', 'HEAD'].includes(method));
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined && !isBodyInit(options.body)
        && typeof options.body === 'object' && options.body !== null) {
      headers.set('Content-Type', 'application/json');
      options = {...options, body: JSON.stringify(options.body)};
    }
    if (!headers.has('X-CSRF-Token') && !['GET', 'HEAD'].includes(method)) headers.set('X-CSRF-Token', csrf());

    for (let attempt = 0; ; attempt += 1) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, {...options, method, headers, cache: options.cache || 'no-store', signal: controller.signal});
        const text = await response.text();
        let result = null;
        try { result = text ? JSON.parse(text) : null; } catch (_) { result = null; }
        if (response.status === 401) {
          redirectToLogin();
          throw new ApiError((result && result.message) || '登录已失效，请重新登录', {status: 401, code: (result && result.error) || 'unauthorized'});
        }
        if (!response.ok || (result && result.ok === false)) {
          throw new ApiError((result && result.message) || (text && text.slice(0, 160)) || '请求失败', {
            status: response.status,
            code: (result && result.error) || 'request_failed',
          });
        }
        if (result === null && text) throw new ApiError('服务器返回了无法解析的响应', {status: response.status, code: 'invalid_json'});
        return result;
      } catch (error) {
        const apiError = error instanceof ApiError
          ? error
          : new ApiError(error.name === 'AbortError' ? '请求超时，请稍后重试' : '网络请求失败', {code: error.name === 'AbortError' ? 'timeout' : 'network', cause: error});
        if (apiError.status === 401 || !retryable || attempt >= retries) throw apiError;
        await sleep(Math.min(4000, 250 * (2 ** attempt)));
      } finally {
        window.clearTimeout(timeout);
      }
    }
  };

  window.GridApi = {ApiError, request};
})();
