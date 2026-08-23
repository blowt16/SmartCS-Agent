// 文档上传（XHR 带进度）与列表/删除
import { authHeaders, handleUnauthorized } from './auth.js';

// 当前用户的知识库文档列表（登录/刷新后恢复显示）
export async function listDocuments(userId) {
  const res = await fetch(`/api/documents?user_id=${encodeURIComponent(userId)}`, {
    headers: authHeaders(),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`加载文档列表失败: ${res.status}`);
  return res.json();
}

// 删除文档（按 md5；后端同时清理 chunks）
export async function deleteDocumentApi(md5, userId) {
  const res = await fetch(`/api/documents/${encodeURIComponent(md5)}?user_id=${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`删除文档失败: ${res.status}`);
  return res.json();
}

export function uploadFileWithProgress({ file, userId, onProgress }) {
  return new Promise((resolve, reject) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('user_id', userId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    // XHR 无法直接带自定义头拦截 401，仅附带 token
    const token = localStorage.getItem('token');
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else if (xhr.status === 401) {
        window.dispatchEvent(new Event('auth:unauthorized'));
        reject(new Error('未授权'));
      } else {
        reject(new Error(`上传失败: ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.send(formData);
  });
}
