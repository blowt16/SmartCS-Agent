<template>
  <main class="flex-1 flex flex-col min-w-0" :class="isDark ? 'bg-gray-800' : 'bg-page-bg'">
    <!-- 顶部 Header -->
    <header class="sticky top-0 z-30 px-6 py-4 border-b" :class="isDark ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-100'">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-4">
          <!-- 移动端菜单按钮 -->
          <button
            @click="emit('toggle-sidebar')"
            class="lg:hidden w-10 h-10 rounded-xl flex items-center justify-center transition-colors"
            :class="isDark ? 'bg-gray-700 text-gray-300 hover:bg-gray-600' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
          >
            <i class="fas fa-bars"></i>
          </button>

          <!-- 对话标题 -->
          <div>
            <div
              v-if="!editingTitle"
              @dblclick="startEditTitle"
              class="text-lg font-semibold cursor-pointer"
              :class="isDark ? 'text-white' : 'text-gray-800'"
            >
              {{ currentConversation?.name || '新对话' }}
            </div>
            <input
              v-else
              ref="titleInput"
              v-model="editTitleValue"
              @blur="saveTitle"
              @keyup.enter="saveTitle"
              @keyup.escape="cancelEditTitle"
              class="text-lg font-semibold bg-transparent border-b-2 border-primary focus:outline-none"
              :class="isDark ? 'text-white' : 'text-gray-800'"
            >
          </div>
        </div>

        <div class="flex items-center gap-3">
          <!-- 模型标签 -->
          <span class="hidden sm:inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-green-100 text-green-700">
            <i class="fas fa-robot"></i>
            DeepSeek
          </span>

          <!-- 文档管理按钮 -->
          <button
            @click="emit('toggle-docs')"
            class="w-10 h-10 rounded-xl flex items-center justify-center transition-all duration-200"
            :class="[
              docsPanelOpen
                ? 'bg-primary text-white'
                : isDark
                  ? 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                  : 'bg-gray-100 text-gray-600 hover:bg-primary hover:text-white'
            ]"
          >
            <i class="fas fa-folder"></i>
          </button>

          <!-- 暗色模式切换 -->
          <button
            @click="emit('toggle-dark')"
            class="w-10 h-10 rounded-xl flex items-center justify-center transition-all duration-200"
            :class="isDark ? 'bg-gray-700 text-yellow-400 hover:bg-gray-600' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
          >
            <i :class="isDark ? 'fas fa-sun' : 'fas fa-moon'"></i>
          </button>
        </div>
      </div>
    </header>

    <!-- 消息区域 / 欢迎页 -->
    <div class="flex-1 overflow-y-auto" ref="messagesContainer">
      <!-- 欢迎页 -->
      <div v-if="!currentConversation || messages.length === 0" class="h-full flex flex-col">
        <!-- 统计卡片区 -->
        <div class="p-6">
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div
              v-for="stat in stats"
              :key="stat.label"
              class="card-hover rounded-2xl p-5 shadow-sm"
              :class="isDark ? 'bg-gray-700' : 'bg-white'"
            >
              <div class="flex items-center justify-between mb-3">
                <div
                  class="w-10 h-10 rounded-xl flex items-center justify-center"
                  :class="stat.iconBg"
                >
                  <i :class="[stat.icon, stat.iconColor]"></i>
                </div>
                <span
                  class="text-xs font-medium px-2 py-1 rounded-full"
                  :class="stat.change > 0 ? 'bg-green-100 text-green-600' : 'bg-red-100 text-red-600'"
                >
                  <i :class="stat.change > 0 ? 'fas fa-arrow-up' : 'fas fa-arrow-down'"></i>
                  {{ Math.abs(stat.change) }}%
                </span>
              </div>
              <p class="text-2xl font-bold mb-1" :class="isDark ? 'text-white' : 'text-gray-800'">
                {{ stat.value }}
              </p>
              <p class="text-xs" :class="isDark ? 'text-gray-400' : 'text-gray-500'">
                {{ stat.label }}
              </p>
            </div>
          </div>
        </div>

        <!-- 欢迎内容 -->
        <div class="flex-1 flex flex-col items-center justify-center px-6 pb-20">
          <!-- 叶子 SVG 插图 -->
          <div class="w-32 h-32 mb-6 opacity-80">
            <svg viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M60 10C40 10 20 30 20 60C20 90 40 110 60 110C80 110 100 90 100 60C100 30 80 10 60 10Z" stroke="#16a34a" stroke-width="2" fill="none"/>
              <path d="M60 20V100" stroke="#16a34a" stroke-width="2"/>
              <path d="M60 35C45 45 35 60 35 80" stroke="#22c55e" stroke-width="2"/>
              <path d="M60 35C75 45 85 60 85 80" stroke="#22c55e" stroke-width="2"/>
              <path d="M60 50C50 55 45 65 45 75" stroke="#22c55e" stroke-width="1.5"/>
              <path d="M60 50C70 55 75 65 75 75" stroke="#22c55e" stroke-width="1.5"/>
              <circle cx="60" cy="60" r="5" fill="#16a34a"/>
            </svg>
          </div>
          <h2 class="text-2xl font-bold mb-3" :class="isDark ? 'text-white' : 'text-gray-800'">
            欢迎使用 SmartCS-Agent
          </h2>
          <p class="text-center max-w-md leading-relaxed" :class="isDark ? 'text-gray-400' : 'text-gray-500'">
            我是您的智能客服助手，可以回答问题、提供建议、帮助您解决各种问题。开始对话吧！
          </p>
        </div>
      </div>

      <!-- 消息列表 -->
      <div v-else class="p-6 space-y-4">
        <div
          v-for="(msg, index) in messages"
          :key="index"
          class="flex gap-3 animate-fade-in"
          :class="msg.role === 'user' ? 'flex-row-reverse' : ''"
        >
          <!-- 头像 -->
          <div
            class="w-9 h-9 rounded-full flex-shrink-0 flex items-center justify-center"
            :class="msg.role === 'user' ? 'bg-primary text-white' : 'bg-primary-bg text-primary'"
          >
            <i :class="msg.role === 'user' ? 'fas fa-user text-sm' : 'fas fa-leaf text-sm'"></i>
          </div>

          <!-- 消息内容 -->
          <div
            class="max-w-[70%] flex flex-col"
            :class="msg.role === 'user' ? 'items-end' : 'items-start'"
          >
            <!-- 消息气泡 -->
            <div
              class="px-4 py-3 rounded-2xl markdown-content"
              :class="[
                msg.role === 'user'
                  ? 'bg-primary text-white rounded-br-sm user-message'
                  : isDark
                    ? 'bg-gray-700 text-gray-100 rounded-bl-sm'
                    : 'bg-bot-bubble text-gray-800 rounded-bl-sm'
              ]"
              v-html="renderMarkdown(msg.content)"
            ></div>

            <!-- 图片附件 -->
            <img
              v-if="msg.image"
              :src="msg.image"
              class="mt-2 max-w-[200px] rounded-xl cursor-pointer hover:opacity-90 transition-opacity"
              @click="emit('preview-image', msg.image)"
            >

            <!-- 时间戳和来源 -->
            <div class="flex items-center gap-2 mt-1.5 px-1">
              <span class="text-xs" :class="isDark ? 'text-gray-500' : 'text-gray-400'">
                {{ formatMessageTime(msg.timestamp) }}
              </span>
              <span
                v-if="msg.role === 'assistant' && msg.source"
                class="text-xs px-2 py-0.5 rounded-full"
                :class="isDark ? 'bg-gray-700 text-gray-400' : 'bg-gray-100 text-gray-500'"
              >
                来源: {{ msg.source }}
              </span>
            </div>
          </div>
        </div>

        <!-- 正在输入指示器 -->
        <div v-if="isTyping" class="flex gap-3 animate-fade-in">
          <div class="w-9 h-9 rounded-full flex-shrink-0 flex items-center justify-center bg-primary-bg text-primary">
            <i class="fas fa-leaf text-sm"></i>
          </div>
          <div
            class="px-4 py-3 rounded-2xl rounded-bl-sm flex items-center gap-1.5"
            :class="isDark ? 'bg-gray-700' : 'bg-bot-bubble'"
          >
            <div class="typing-dot w-2 h-2 rounded-full bg-primary"></div>
            <div class="typing-dot w-2 h-2 rounded-full bg-primary"></div>
            <div class="typing-dot w-2 h-2 rounded-full bg-primary"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 底部输入区 -->
    <div class="p-4 border-t" :class="isDark ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-100'">
      <!-- 图片预览 -->
      <div v-if="selectedImages.length > 0" class="flex gap-2 mb-3 flex-wrap">
        <div
          v-for="(img, index) in selectedImages"
          :key="index"
          class="relative w-16 h-16 rounded-xl overflow-hidden"
        >
          <img :src="img.preview" class="w-full h-full object-cover">
          <button
            @click="removeImage(index)"
            class="absolute top-1 right-1 w-5 h-5 rounded-full bg-black/60 text-white flex items-center justify-center hover:bg-red-500 transition-colors"
          >
            <i class="fas fa-times text-xs"></i>
          </button>
        </div>
      </div>

      <!-- 输入框容器 -->
      <div
        class="flex items-end gap-3 p-3 rounded-2xl shadow-sm border-2 transition-all duration-200"
        :class="[
          inputFocused
            ? 'border-primary ring-4 ring-primary/10'
            : isDark
              ? 'border-gray-600 bg-gray-700'
              : 'border-gray-200 bg-white',
          isDark ? 'bg-gray-700' : 'bg-white'
        ]"
      >
        <!-- 附件按钮 -->
        <input
          type="file"
          ref="imageInput"
          accept="image/*"
          multiple
          class="hidden"
          @change="handleImageSelect"
        >
        <button
          @click="imageInput.click()"
          class="w-10 h-10 rounded-xl flex items-center justify-center transition-colors flex-shrink-0"
          :class="isDark ? 'bg-gray-600 text-gray-300 hover:bg-primary hover:text-white' : 'bg-gray-100 text-gray-500 hover:bg-primary hover:text-white'"
        >
          <i class="fas fa-image"></i>
        </button>

        <!-- 文档上传按钮 -->
        <button
          @click="emit('toggle-docs')"
          class="w-10 h-10 rounded-xl flex items-center justify-center transition-colors flex-shrink-0"
          :class="isDark ? 'bg-gray-600 text-gray-300 hover:bg-primary hover:text-white' : 'bg-gray-100 text-gray-500 hover:bg-primary hover:text-white'"
        >
          <i class="fas fa-file-alt"></i>
        </button>

        <!-- 文本输入框 -->
        <textarea
          ref="messageInput"
          v-model="inputMessage"
          @focus="inputFocused = true"
          @blur="inputFocused = false"
          @keydown="handleKeyDown"
          @input="autoResize"
          placeholder="输入你的问题..."
          rows="1"
          class="flex-1 bg-transparent border-none focus:outline-none text-base leading-relaxed auto-resize max-h-32"
          :class="isDark ? 'text-white placeholder-gray-400' : 'text-gray-800 placeholder-gray-400'"
        ></textarea>

        <!-- 发送按钮 -->
        <button
          @click="send"
          :disabled="!canSend"
          class="w-11 h-11 rounded-xl flex items-center justify-center transition-all duration-200 flex-shrink-0"
          :class="[
            canSend
              ? 'bg-primary hover:bg-primary-dark text-white hover:scale-105 hover:rotate-12'
              : isDark
                ? 'bg-gray-600 text-gray-400 cursor-not-allowed'
                : 'bg-gray-200 text-gray-400 cursor-not-allowed'
          ]"
        >
          <i class="fas fa-paper-plane"></i>
        </button>
      </div>
    </div>
  </main>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue';
import { renderMarkdown } from '../utils/markdown.js';
import { formatMessageTime } from '../utils/format.js';

const props = defineProps({
  messages: { type: Array, default: () => [] },
  isTyping: { type: Boolean, default: false },
  currentConversation: { type: Object, default: null },
  isDark: { type: Boolean, default: false },
  docsPanelOpen: { type: Boolean, default: true },
  stats: { type: Array, default: () => [] },
});

const emit = defineEmits([
  'send', 'save-title', 'preview-image',
  'toggle-sidebar', 'toggle-docs', 'toggle-dark',
]);

// 输入区局部状态
const inputMessage = ref('');
const selectedImages = ref([]);
const inputFocused = ref(false);
const editingTitle = ref(false);
const editTitleValue = ref('');

const messagesContainer = ref(null);
const messageInput = ref(null);
const imageInput = ref(null);
const titleInput = ref(null);

const canSend = computed(() => {
  return inputMessage.value.trim() || selectedImages.value.length > 0;
});

// 滚动策略：深度监听消息内容变化 → 滚底（替代原件 6 处显式 scrollToBottom，行为等价）
watch(
  () => props.messages,
  () => {
    nextTick(() => {
      if (messagesContainer.value) {
        messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
      }
    });
  },
  { deep: true }
);

// 处理键盘事件
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

// 自动调整输入框高度
function autoResize() {
  const el = messageInput.value;
  if (el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 128) + 'px';
  }
}

// 图片处理
function handleImageSelect(e) {
  const files = Array.from(e.target.files);
  files.forEach(file => {
    const reader = new FileReader();
    reader.onload = (ev) => {
      selectedImages.value.push({
        file: file,
        preview: ev.target.result
      });
    };
    reader.readAsDataURL(file);
  });
  e.target.value = '';
}

function removeImage(index) {
  selectedImages.value.splice(index, 1);
}

// 编辑标题
function startEditTitle() {
  if (!props.currentConversation) return;
  editingTitle.value = true;
  editTitleValue.value = props.currentConversation.name;
  nextTick(() => {
    titleInput.value?.focus();
  });
}

function saveTitle() {
  if (props.currentConversation && editTitleValue.value.trim()) {
    emit('save-title', editTitleValue.value.trim());
  }
  editingTitle.value = false;
}

function cancelEditTitle() {
  editingTitle.value = false;
}

// 发送消息
function send() {
  if (!canSend.value) return;
  const content = inputMessage.value.trim();
  const images = [...selectedImages.value];
  inputMessage.value = '';
  selectedImages.value = [];
  nextTick(() => autoResize());
  emit('send', { content, images });
}
</script>
