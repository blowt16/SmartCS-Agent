// JWT 登录鉴权封装：token 读写（localStorage，与旧前端同 key）+ 登录/注册/users/me

const TOKEN_KEY = 'token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// 统一 401 处理：清 token 并广播事件，由 App 层切回登录视图
export function handleUnauthorized(res) {
  if (res.status === 401) {
    clearToken();
    window.dispatchEvent(new Event('auth:unauthorized'));
    return true;
  }
  return false;
}

export async function login(email, password) {
  const res = await fetch('/api/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (res.status === 401) {
    throw new Error('邮箱或密码错误');
  }
  if (!res.ok) {
    throw new Error(`登录失败: ${res.status}`);
  }
  const data = await res.json();
  setToken(data.access_token);
  return data;
}

export async function register(username, email, password) {
  const res = await fetch('/api/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, email, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `注册失败: ${res.status}`);
  }
  return res.json();
}

export async function getMe() {
  const res = await fetch('/api/users/me', { headers: authHeaders() });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`获取用户信息失败: ${res.status}`);
  return res.json();
}
