/*
 * 页面端统一 API 客户端。
 *
 * 所有页面脚本都通过这里访问 Flask API，以便统一处理 CSRF、JSON 编码、
 * 超时、AbortController 取消、登录失效跳转以及仅对安全请求进行有限重试。
 * 这里不直接决定业务提示文案；业务页面只需要处理结构化的 ApiError。
 */
(() => {
  /** 表示 HTTP、网络、超时或响应格式错误，并保留可用于恢复的上下文。 */
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

  /**
   * 发起一个带统一错误处理和有限退避重试的 API 请求。
   *
   * GET/HEAD 默认允许重试；修改数据的请求默认不重试，避免网络超时后
   * 无法判断服务端是否已经成功执行时重复提交。调用方可通过 signal 取消请求。
   */
  const request = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Math.max(1, options.timeoutMs) : 10000;
    const retries = Number.isFinite(options.retries) ? Math.max(0, options.retries) : 2;
    const retryableMethod = options.retry !== false && (options.retry === true || ['GET', 'HEAD'].includes(method));
    const externalSignal = options.signal;
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined && !isBodyInit(options.body)
        && typeof options.body === 'object' && options.body !== null) {
      headers.set('Content-Type', 'application/json');
      options = {...options, body: JSON.stringify(options.body)};
    }
    if (!headers.has('X-CSRF-Token') && !['GET', 'HEAD'].includes(method)) headers.set('X-CSRF-Token', csrf());

    const retryAfterMs = (response) => {
      const value = response.headers.get('Retry-After');
      if (!value) return 0;
      const seconds = Number(value);
      if (Number.isFinite(seconds)) return Math.min(30000, Math.max(0, seconds * 1000));
      const timestamp = Date.parse(value);
      return Number.isNaN(timestamp) ? 0 : Math.min(30000, Math.max(0, timestamp - Date.now()));
    };
    const canRetryResponse = (status) => [408, 425, 429, 500, 502, 503, 504].includes(status);

    for (let attempt = 0; ; attempt += 1) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      let timeout = window.setTimeout(abort, timeoutMs);
      if (externalSignal) {
        if (externalSignal.aborted) controller.abort();
        else externalSignal.addEventListener('abort', abort, {once: true});
      }
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
          const error = new ApiError((result && result.message) || (text && text.slice(0, 160)) || '请求失败', {
            status: response.status,
            code: (result && result.error) || 'request_failed',
          });
          if (retryableMethod && canRetryResponse(response.status) && attempt < retries) {
            await sleep(Math.max(Math.min(4000, 250 * (2 ** attempt)), retryAfterMs(response)));
            continue;
          }
          throw error;
        }
        if (result === null && text) throw new ApiError('服务器返回了无法解析的响应', {status: response.status, code: 'invalid_json'});
        return result;
      } catch (error) {
        const apiError = error instanceof ApiError
          ? error
          : new ApiError(error.name === 'AbortError' ? '请求超时，请稍后重试' : '网络请求失败', {code: error.name === 'AbortError' ? 'timeout' : 'network', cause: error});
        const abortedExternally = externalSignal?.aborted;
        if (abortedExternally || !retryableMethod || attempt >= retries || apiError.status === 401) throw apiError;
        await sleep(Math.min(4000, 250 * (2 ** attempt)));
      } finally {
        window.clearTimeout(timeout);
        if (externalSignal) externalSignal.removeEventListener('abort', abort);
      }
    }
  };

  window.GridApi = {ApiError, request};
})();
