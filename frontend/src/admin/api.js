// 管理端接口封装:统一带令牌、统一 401/403 处理、统一错误文案。
//
// 两个长连接动作【不走 request()】:
//   - stage 上传 → stageFile()(要 XHR 上传进度,且 src/api/upload.js 的 URL 硬编码成 /api/upload)
//   - commit 索引 → src/admin/knowledgeJob.js 的 startCommit()(返回 SSE 流,要逐帧读)
import { authHeaders, handleUnauthorized } from '../api/auth.js';

// 浏览器 <input type="number"> / <input type="date"> 留空时给的是 ''(不是 undefined),
// JSON.stringify 后就是 ""。而 pydantic 对 Optional[Decimal]/Optional[int]/Optional[date]
// 收到 '' 一律 ValidationError → 422(只有 Optional[str] 接受 '')。
// 所以发请求前必须把 "" 从 body 里摘掉,等价于"未传"。
export function cleanBody(body) {
  return Object.fromEntries(
    Object.entries(body).filter(([, v]) => v !== '' && v !== undefined)
  );
}

// 过滤 undefined / null / '' 的键后拼查询串
function qs(obj = {}) {
  const p = new URLSearchParams();
  Object.entries(obj).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') p.append(k, v);
  });
  const s = p.toString();
  return s;
}

async function request(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...authHeaders(),
      ...options.headers,
    },
  });
  if (handleUnauthorized(res)) throw new Error('登录已失效');
  if (res.status === 403) throw new Error('需要管理员权限');
  if (!res.ok) {
    // 管理端端点用标准 4xx 语义,响应体里的 detail 是给人看的
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `请求失败: ${res.status}`);
  }
  return res.json();
}

// ---- 控制台 ----
export const getStats = () => request('/api/admin/console/stats');
export const getCharts = (days = 7) => request(`/api/admin/console/charts?days=${days}`);

// ---- 商品 ----
export const listProducts = (p) => request(`/api/admin/products?${qs(p)}`);
export const createProduct = (b) => request('/api/admin/products', { method: 'POST', body: JSON.stringify(b) });
export const updateProduct = (sku, b) => request(`/api/admin/products/${encodeURIComponent(sku)}`, { method: 'PUT', body: JSON.stringify(cleanBody(b)) });
export const deleteProduct = (sku) => request(`/api/admin/products/${encodeURIComponent(sku)}`, { method: 'DELETE' });
export const listCategories = () => request('/api/admin/products/categories');

// ---- 订单 ----
export const listOrders = (p) => request(`/api/admin/orders?${qs(p)}`);
export const createOrder = (b) => request('/api/admin/orders', { method: 'POST', body: JSON.stringify(cleanBody(b)) });
export const updateOrder = (id, b) => request(`/api/admin/orders/${id}`, { method: 'PUT', body: JSON.stringify(cleanBody(b)) });
export const deleteOrder = (id) => request(`/api/admin/orders/${id}`, { method: 'DELETE' });

// ---- 知识库 ----
export const listKnowledge = (p) => request(`/api/admin/knowledge?${qs(p)}`);
export const unstageKnowledge = (md5) => request(`/api/admin/knowledge/stage/${encodeURIComponent(md5)}`, { method: 'DELETE' });
export const updateKnowledge = (md5, b) => request(`/api/admin/knowledge/${encodeURIComponent(md5)}`, { method: 'PATCH', body: JSON.stringify(cleanBody(b)) });
export const deleteKnowledge = (md5) => request(`/api/admin/knowledge/${encodeURIComponent(md5)}`, { method: 'DELETE' });

// ---- 工单 ----
export const listTickets = (p) => request(`/api/admin/tickets?${qs(p)}`);
export const updateTicket = (id, b) => request(`/api/admin/tickets/${id}`, { method: 'PUT', body: JSON.stringify(cleanBody(b)) });

// 管理端专用的带进度上传。与 src/api/upload.js 的 uploadFileWithProgress 结构相同,
// 两点差异:(1) URL 指向管理端 stage 端点;(2) 【非 2xx 时解析响应体】把 detail 带出来 ——
// 后者是管理端端点用标准 4xx 语义换来的好处,客户端那个版本的 400 是把响应体丢掉的。
//
// ⚠️ 不要把这个函数改成通用上传器去服务客户端:两者契约不同(/api/upload 把处理类失败
// 包进 200 的 status=failed),各留各的。
export function stageFile({ file, userId, onProgress }) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('user_id', userId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/admin/knowledge/stage');
    // XHR 无法直接带自定义头拦截 401,仅附带 token
    const token = localStorage.getItem('token');
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };

    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* 非 JSON,下面按状态码兜底 */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      if (xhr.status === 401) {
        window.dispatchEvent(new Event('auth:unauthorized'));
        return reject(new Error('登录已失效'));
      }
      reject(new Error(body?.detail || `上传失败: ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.send(fd);
  });
}
