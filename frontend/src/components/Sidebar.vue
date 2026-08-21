<template>
  <aside
    class="fixed lg:relative z-50 h-full w-[280px] flex flex-col transition-transform duration-300 lg:translate-x-0"
    :class="[
      sidebarOpen ? 'translate-x-0' : '-translate-x-full',
      isDark ? 'bg-gray-900' : 'bg-sidebar-bg'
    ]"
  >
    <!-- 品牌区 -->
    <div class="p-6 border-b border-white/10">
      <div class="flex items-center gap-3 mb-2">
        <!-- 叶子 Logo -->
        <div class="w-10 h-10 rounded-full bg-white/10 flex items-center justify-center">
          <svg class="w-6 h-6 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 3c-4.5 0-9 4.5-9 9 0 0 4.5 9 9 9s9-9 9-9c0-4.5-4.5-9-9-9z" />
            <path d="M12 3v18" />
            <path d="M12 8c-2 2-3 4-3 7" />
            <path d="M12 8c2 2 3 4 3 7" />
          </svg>
        </div>
        <div>
          <h1 class="text-white text-lg font-semibold">SmartCS-Agent</h1>
          <p class="text-green-300 text-xs">智能客服助手</p>
        </div>
      </div>
    </div>

    <!-- 搜索框 -->
    <div class="px-4 py-3">
      <div class="relative">
        <i class="fas fa-search absolute left-3 top-1/2 -translate-y-1/2 text-white/40 text-sm"></i>
        <input
          type="text"
          v-model="searchQuery"
          placeholder="搜索对话..."
          class="w-full bg-white/10 text-white placeholder-white/40 rounded-xl py-2.5 pl-10 pr-4 text-sm focus:outline-none focus:ring-2 focus:ring-green-400/50"
        >
      </div>
    </div>

    <!-- 新建对话按钮 -->
    <div class="px-4 pb-3">
      <button
        @click="emit('create')"
        class="w-full bg-primary hover:bg-primary-light text-white rounded-xl py-3 px-4 font-medium text-sm flex items-center justify-center gap-2 transition-all duration-200 hover:-translate-y-0.5 shadow-lg shadow-green-900/30"
      >
        <i class="fas fa-plus"></i>
        新建对话
      </button>
    </div>

    <!-- 对话历史列表 -->
    <div class="flex-1 overflow-y-auto px-3 py-2">
      <div
        v-for="conv in filteredConversations"
        :key="conv.id"
        @click="emit('select', conv)"
        class="nav-item group relative rounded-xl px-4 py-3 mb-2 cursor-pointer"
        :class="[
          currentConversation?.id === conv.id
            ? 'bg-sidebar-active active'
            : 'hover:bg-sidebar-hover'
        ]"
      >
        <div class="flex items-start justify-between">
          <div class="flex-1 min-w-0 pr-8">
            <p class="text-white text-sm font-medium truncate">{{ conv.name }}</p>
            <p class="text-green-300/60 text-xs mt-1">{{ formatTime(conv.updatedAt) }}</p>
          </div>
          <!-- 删除按钮 -->
          <button
            @click.stop="emit('delete', conv.id)"
            class="absolute right-3 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 w-7 h-7 rounded-lg bg-white/10 hover:bg-red-500 text-white/60 hover:text-white flex items-center justify-center transition-all duration-200"
          >
            <i class="fas fa-trash text-xs"></i>
          </button>
        </div>
      </div>

      <!-- 空状态 -->
      <div v-if="filteredConversations.length === 0" class="text-center py-8">
        <i class="fas fa-comments text-3xl text-white/20 mb-3"></i>
        <p class="text-white/40 text-sm">暂无对话记录</p>
      </div>
    </div>

    <!-- 底部用户信息 -->
    <div class="p-4 border-t border-white/10">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-full bg-primary flex items-center justify-center text-white font-semibold">
          {{ (user.name || '访客').charAt(0) }}
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-white text-sm font-medium truncate">{{ user.name }}</p>
          <div class="flex items-center gap-1.5">
            <span class="w-2 h-2 rounded-full bg-green-400"></span>
            <span class="text-green-300/60 text-xs">在线</span>
          </div>
        </div>
        <button
          @click="emit('logout')"
          class="w-9 h-9 rounded-lg bg-white/10 hover:bg-red-500 text-white/60 hover:text-white flex items-center justify-center transition-all duration-200"
        >
          <i class="fas fa-sign-out-alt"></i>
        </button>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed } from 'vue';
import { formatTime } from '../utils/format.js';

const props = defineProps({
  conversations: { type: Array, default: () => [] },
  currentConversation: { type: Object, default: null },
  user: { type: Object, required: true },
  isDark: { type: Boolean, default: false },
  sidebarOpen: { type: Boolean, default: false },
});

const emit = defineEmits(['create', 'select', 'delete', 'logout']);

const searchQuery = ref('');

const filteredConversations = computed(() => {
  if (!searchQuery.value) return props.conversations;
  return props.conversations.filter(c =>
    c.name.toLowerCase().includes(searchQuery.value.toLowerCase())
  );
});
</script>
