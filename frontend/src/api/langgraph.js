// /api/langgraph/query 调用封装（multipart form，返回原始 Response 供 SSE 读取）
import { authHeaders } from './auth.js';

export function postLanggraphQuery({ query, userId, conversationId, imageFile }) {
  const formData = new FormData();
  formData.append('query', query);
  formData.append('user_id', userId);
  if (conversationId) {
    formData.append('conversation_id', conversationId);
  }
  if (imageFile) {
    formData.append('image', imageFile);
  }
  return fetch('/api/langgraph/query', {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
  });
}
