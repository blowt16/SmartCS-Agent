// SSE 流式聊天核心（从 chat.html sendMessage/loadMessages 逐行迁移）
import { ref } from 'vue';
import { postLanggraphQuery } from '../api/langgraph.js';
import { getConversationMessages, saveConversationMessages } from '../api/conversations.js';

// user: Ref<{id,...}>；getCurrentConversation: () => currentConversation.value | null
// 会话创建由 App 层在调用 sendMessage 前确保（与原件 createNewConversation 行为一致）
export function useChat({ user, getCurrentConversation }) {
  const messages = ref([]);
  const isTyping = ref(false);
  const langgraphConversationId = ref(null);

  // 从后端加载历史消息
  async function loadMessages(conversationId) {
    try {
      const data = await getConversationMessages(conversationId, user.value.id);
      messages.value = data.map(msg => ({
        role: msg.sender,
        content: msg.content,
        timestamp: new Date(msg.created_at)
      }));
    } catch (e) {
      console.error('加载消息失败:', e);
      messages.value = [];
    }
  }

  // 发送消息（对接 LangGraph SSE 流）
  async function sendMessage(content, images) {
    if (isTyping.value) return;

    const img = images[0] || null;

    // 添加用户消息到界面
    messages.value.push({
      role: 'user',
      content: content,
      image: img && img.preview ? img.preview : null,
      timestamp: new Date()
    });

    // 显示正在输入（直到收到第一个内容才关闭）
    isTyping.value = true;

    // 调用 LangGraph 接口
    try {
      const response = await postLanggraphQuery({
        query: content,
        userId: user.value.id,
        conversationId: langgraphConversationId.value,
        imageFile: img && img.file ? img.file : null,
      });

      if (!response.ok) {
        throw new Error(`API 错误: ${response.status}`);
      }

      // 从响应头获取 LangGraph 会话 ID
      const newConvId = response.headers.get('X-Conversation-ID');
      if (newConvId) {
        langgraphConversationId.value = newConvId;
      }

      // AI 消息占位
      let aiContent = '';
      let firstChunk = true;
      const aiMsg = {
        role: 'assistant',
        content: '',
        timestamp: new Date(),
        source: 'LangGraph'
      };
      messages.value.push(aiMsg);
      const msgIndex = messages.value.length - 1;

      // 读取 SSE 流
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // 兼容 \r\n 和 \n 换行
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop();

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr) continue;

          try {
            const parsed = JSON.parse(jsonStr);
            if (parsed && parsed.interruption) {
              langgraphConversationId.value = parsed.conversation_id;
              aiContent += '\n\n*等待您的确认...*';
              messages.value[msgIndex] = { ...messages.value[msgIndex], content: aiContent };
              continue;
            }
            if (typeof parsed === 'string') {
              aiContent += parsed;
            } else if (Array.isArray(parsed)) {
              for (const part of parsed) {
                if (typeof part === 'string') aiContent += part;
                else if (part && part.type === 'text') aiContent += part.text;
              }
            }
          } catch (e) {
            aiContent += jsonStr;
          }

          if (firstChunk && aiContent) {
            firstChunk = false;
            isTyping.value = false;
          }
          // 替换整个对象以触发 Vue 响应式
          messages.value[msgIndex] = { ...messages.value[msgIndex], content: aiContent };
        }
      }

      isTyping.value = false;
      messages.value[msgIndex] = { ...messages.value[msgIndex], content: aiContent };

      // 流结束后，保存这轮对话到 PostgreSQL
      const conv = getCurrentConversation();
      if (conv && aiContent && content) {
        try {
          await saveConversationMessages(conv.id, content, aiContent);
        } catch (saveErr) {
          console.error('保存消息失败:', saveErr);
        }
      }

    } catch (e) {
      isTyping.value = false;
      messages.value.push({
        role: 'assistant',
        content: `抱歉，请求出错了：${e.message}`,
        timestamp: new Date()
      });
    }
  }

  return { messages, isTyping, langgraphConversationId, sendMessage, loadMessages };
}
