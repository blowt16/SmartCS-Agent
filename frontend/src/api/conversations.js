// 会话 CRUD 封装
import { authHeaders, handleUnauthorized } from './auth.js';

export async function createConversation(userId) {
  const res = await fetch('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ user_id: userId }),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`创建对话失败: ${res.status}`);
  return res.json();
}

export async function listUserConversations(userId) {
  const res = await fetch(`/api/conversations/user/${userId}`, { headers: authHeaders() });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`加载会话列表失败: ${res.status}`);
  return res.json();
}

export async function getConversationMessages(conversationId, userId) {
  const res = await fetch(`/api/conversations/${conversationId}/messages?user_id=${userId}`, {
    headers: authHeaders(),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`加载消息失败: ${res.status}`);
  return res.json();
}

export async function deleteConversation(conversationId) {
  const res = await fetch(`/api/conversations/${conversationId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`删除对话失败: ${res.status}`);
  return res.json();
}

export async function renameConversation(conversationId, name) {
  const res = await fetch(`/api/conversations/${conversationId}/name`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`重命名失败: ${res.status}`);
  return res.json();
}

export async function saveConversationMessages(conversationId, userMessage, assistantMessage) {
  const res = await fetch('/api/conversations/save-messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      conversation_id: conversationId,
      user_message: userMessage,
      assistant_message: assistantMessage,
    }),
  });
  handleUnauthorized(res);
  if (!res.ok) throw new Error(`保存消息失败: ${res.status}`);
  return res.json();
}
