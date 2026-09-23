<template>
  <div>
    <!-- 未登录：登录/注册页 -->
    <LoginView v-if="!isAuthenticated" @logged-in="handleLoggedIn" />

    <!-- 主界面 -->
    <template v-else>
      <!-- 移动端侧边栏遮罩 -->
      <div
        class="sidebar-overlay"
        :class="{ active: sidebarOpen && isMobile }"
        @click="sidebarOpen = false"
      ></div>

      <div class="flex h-screen">
        <Sidebar
          :conversations="conversations"
          :current-conversation="currentConversation"
          :user="user"
          :is-dark="isDark"
          :sidebar-open="sidebarOpen"
          @create="createNewConversation"
          @select="selectConversation"
          @delete="deleteConversation"
          @logout="logout"
        />

        <ChatArea
          :messages="messages"
          :is-typing="isTyping"
          :current-conversation="currentConversation"
          :is-dark="isDark"
          :stats="stats"
          @send="handleSend"
          @save-title="handleSaveTitle"
          @preview-image="previewImage = $event"
          @toggle-sidebar="sidebarOpen = !sidebarOpen"
          @toggle-dark="toggleDarkMode"
        />
      </div>

      <!-- 图片预览弹窗 -->
      <div
        v-if="previewImage"
        @click="previewImage = null"
        class="fixed inset-0 z-[100] bg-black/80 flex items-center justify-center p-4"
      >
        <img :src="previewImage" class="max-w-full max-h-full rounded-lg">
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import LoginView from './components/LoginView.vue';
import Sidebar from './components/Sidebar.vue';
import ChatArea from './components/ChatArea.vue';
import { useChat } from './composables/useChat.js';
import { getToken, clearToken } from './api/auth.js';
import { getMe } from './api/auth.js';
import {
  createConversation,
  listUserConversations,
  deleteConversation as apiDeleteConversation,
  renameConversation,
} from './api/conversations.js';

// ========== 登录态 ==========
const isAuthenticated = ref(!!getToken());
const user = ref({ id: null, name: '', email: '' });

// ========== UI 状态 ==========
const isDark = ref(false);
const sidebarOpen = ref(false);
const previewImage = ref(null);

// ========== 会话状态 ==========
const conversations = ref([]);
const currentConversation = ref(null);

// ========== 聊天（SSE 流式） ==========
const chat = useChat({
  user,
  getCurrentConversation: () => currentConversation.value,
});
const { messages, isTyping, sendMessage, loadMessages } = chat;

// ========== 计算属性 ==========
const stats = computed(() => [
  {
    label: '总对话数',
    value: conversations.value.length,
    change: 0,
    icon: 'fas fa-comments',
    iconBg: 'bg-green-100',
    iconColor: 'text-green-600'
  },
  {
    label: '今日提问',
    value: messages.value.filter(m => m.role === 'user').length,
    change: 0,
    icon: 'fas fa-question-circle',
    iconBg: 'bg-blue-100',
    iconColor: 'text-blue-600'
  }
]);

const isMobile = computed(() => {
  return window.innerWidth < 1024;
});

// ========== 登录相关 ==========
async function handleLoggedIn() {
  try {
    const me = await getMe();
    user.value = { id: me.id, name: me.username, email: me.email };
    isAuthenticated.value = true;
    await loadConversations();
  } catch (e) {
    console.error('获取用户信息失败:', e);
    clearToken();
    isAuthenticated.value = false;
  }
}

async function logout() {
  if (!confirm('确定要退出登录吗？')) return;
  clearToken();
  currentConversation.value = null;
  messages.value = [];
  conversations.value = [];
  isAuthenticated.value = false;
}

// ========== 会话管理 ==========
async function loadConversations() {
  try {
    const data = await listUserConversations(user.value.id);
    conversations.value = data.map(conv => ({
      id: conv.id,
      name: conv.title,
      // updated_at 为最近活跃时刻（后端已按它倒序）；缺失时回退 created_at
      updatedAt: new Date(conv.updated_at || conv.created_at),
      dialogue_type: conv.dialogue_type
    }));
  } catch (e) {
    console.error('加载会话列表失败:', e);
  }
}

async function createNewConversation() {
  try {
    const data = await createConversation(user.value.id);
    const newConv = {
      id: data.conversation_id,
      name: '新对话',
      updatedAt: new Date()
    };
    conversations.value.unshift(newConv);
    currentConversation.value = newConv;
    messages.value = [];
    // LangGraph 线程 id 直接复用 DB 会话 id：thread_id 由后端按 conversation_id
    // 原样取用（main.py langgraph_query），这样刷新/切换会话后线程不变，历史上下文
    // 得以接续。此前用响应头里的随机 UUID 且只存内存，刷新即丢 → 续聊必然开新线程。
    chat.langgraphConversationId.value = String(newConv.id);
    sidebarOpen.value = false;
  } catch (e) {
    console.error('创建对话失败:', e);
  }
}

async function selectConversation(conv) {
  currentConversation.value = conv;
  // 同上：线程 id = 会话 id，切回历史会话即可续上原来的上下文
  chat.langgraphConversationId.value = String(conv.id);
  await loadMessages(conv.id);
  sidebarOpen.value = false;
}

async function deleteConversation(id) {
  if (!confirm('确定要删除这个对话吗？')) return;
  try {
    await apiDeleteConversation(id);
    conversations.value = conversations.value.filter(c => c.id !== id);
    if (currentConversation.value?.id === id) {
      currentConversation.value = null;
      messages.value = [];
      chat.langgraphConversationId.value = null;
    }
  } catch (e) {
    console.error('删除对话失败:', e);
  }
}

async function handleSaveTitle(name) {
  if (!currentConversation.value) return;
  try {
    await renameConversation(currentConversation.value.id, name);
    currentConversation.value.name = name;
  } catch (e) {
    console.error('重命名失败:', e);
  }
}

// ========== 发送消息 ==========
async function handleSend({ content, images }) {
  if (isTyping.value) return;
  // 确保有对话（与原件 sendMessage 行为一致）
  if (!currentConversation.value) {
    await createNewConversation();
  }
  if (!currentConversation.value) return;
  await sendMessage(content, images);
  // 消息落库后回拉列表：首条消息的标题、最近活跃时间都由后端生成，
  // 本地那条是占位名「新对话」，不刷新则要等整页重载才更新
  await loadConversations();
}

// ========== 暗色模式 ==========
function toggleDarkMode() {
  isDark.value = !isDark.value;
  document.documentElement.classList.toggle('dark', isDark.value);
}

// ========== 初始化 ==========
onMounted(() => {
  // 检查系统暗色模式偏好
  if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    isDark.value = true;
    document.documentElement.classList.add('dark');
  }

  // token 失效（401）时切回登录视图
  window.addEventListener('auth:unauthorized', () => {
    isAuthenticated.value = false;
    currentConversation.value = null;
    messages.value = [];
  });

  // 已有 token 则直接恢复登录态
  if (getToken()) {
    handleLoggedIn();
  }
});
</script>
