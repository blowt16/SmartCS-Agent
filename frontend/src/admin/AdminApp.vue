<template>
  <!-- checking:居中的加载指示 -->
  <div v-if="authState === 'checking'" class="min-h-screen flex items-center justify-center text-gray-400">
    <i class="fas fa-spinner fa-spin mr-2"></i> 加载中…
  </div>

  <!-- anonymous:管理端自己的登录页 -->
  <AdminLogin v-else-if="authState === 'anonymous'" @logged-in="checkAuth" />

  <!-- denied:登录成功但非管理员 -->
  <div v-else-if="authState === 'denied'" class="min-h-screen flex items-center justify-center p-6">
    <div class="bg-white rounded-2xl shadow-sm border border-gray-100 p-8 w-full text-center" style="max-width: 420px">
      <i class="fas fa-lock text-3xl text-gray-300"></i>
      <p class="mt-4 text-gray-700">
        当前账号（<span class="font-medium">{{ me?.email }}</span>）没有管理员权限
      </p>
      <div class="mt-6 flex items-center justify-center gap-4">
        <a href="/" class="text-sm text-primary hover:text-primary-dark">返回客服端</a>
        <button
          type="button"
          class="text-sm text-gray-500 hover:text-gray-700 border border-gray-200 rounded-lg px-4 py-2 transition-colors"
          @click="switchAccount"
        >切换账号</button>
      </div>
    </div>
  </div>

  <!-- ok:导航 + 当前页 -->
  <div v-else class="min-h-screen">
    <header class="bg-white border-b border-gray-100 sticky top-0 z-40">
      <div class="max-w-[1400px] mx-auto px-6 flex items-center h-16 gap-8">
        <div class="flex items-center gap-3 shrink-0">
          <div class="w-9 h-9 rounded-full bg-gradient-to-br from-primary to-emerald-600 flex items-center justify-center text-white">
            <i class="fas fa-headset text-sm"></i>
          </div>
          <div class="leading-tight">
            <div class="font-semibold text-gray-800">SmartCS-Agent</div>
            <div class="text-xs text-gray-400">管理端</div>
          </div>
        </div>

        <nav class="flex items-center gap-1 flex-1">
          <button
            v-for="item in NAV"
            :key="item.key"
            type="button"
            class="relative px-4 py-2 text-sm border-b-2 transition-colors"
            :class="currentPage === item.key
              ? 'text-primary border-primary'
              : 'text-gray-500 border-transparent hover:text-gray-700'"
            @click="go(item.key)"
          >
            <i class="fas mr-1.5" :class="item.icon"></i>{{ item.label }}
          </button>
        </nav>

        <div class="flex items-center gap-4 shrink-0">
          <button
            type="button"
            class="text-sm text-gray-500 hover:text-gray-700 transition-colors"
            @click="logout"
          >退出登录</button>
          <div class="flex items-center gap-2">
            <div class="w-8 h-8 rounded-full bg-primary text-white flex items-center justify-center text-sm font-medium">
              {{ avatarChar }}
            </div>
            <span class="text-sm text-gray-700">{{ me?.username }}</span>
          </div>
        </div>
      </div>
    </header>

    <main class="max-w-[1400px] mx-auto px-6 py-6">
      <ConsoleView v-if="currentPage === 'console'" />
      <ProductView v-else-if="currentPage === 'products'" />
      <OrderView v-else-if="currentPage === 'orders'" />
      <KnowledgeView v-else-if="currentPage === 'knowledge'" />
      <TicketView v-else-if="currentPage === 'tickets'" />
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { clearToken, getMe, getToken } from '../api/auth.js';
import AdminLogin from './components/AdminLogin.vue';
import ConsoleView from './views/ConsoleView.vue';
import ProductView from './views/ProductView.vue';
import OrderView from './views/OrderView.vue';
import KnowledgeView from './views/KnowledgeView.vue';
import TicketView from './views/TicketView.vue';

const NAV = [
  { key: 'console', label: '控制台', icon: 'fa-gauge-high' },
  { key: 'products', label: '商品管理', icon: 'fa-box' },
  { key: 'orders', label: '订单管理', icon: 'fa-clipboard-list' },
  { key: 'knowledge', label: '知识库', icon: 'fa-book' },
  { key: 'tickets', label: '工单', icon: 'fa-ticket' },
];
const PAGES = NAV.map((n) => n.key);

// 无 vue-router:ref + hash 同步(支持刷新后停在同一页)
function pageFromHash() {
  const h = window.location.hash.replace(/^#\/?/, '');
  return PAGES.includes(h) ? h : 'console';
}
const currentPage = ref(pageFromHash());

function go(page) {
  currentPage.value = page;
  window.location.hash = `#/${page}`;
}

function onHashChange() {
  currentPage.value = pageFromHash();
}

// ---- 登录态三段式:checking | anonymous | denied | ok ----
const authState = ref('checking');
const me = ref(null);

async function checkAuth() {
  if (!getToken()) { authState.value = 'anonymous'; return; }
  try {
    const user = await getMe();
    // ⚠️ me 必须在 role 判断【之前】赋值:denied 提示卡要显示 {email}
    me.value = user;
    authState.value = user.role === 'admin' ? 'ok' : 'denied';
  } catch {
    clearToken();
    me.value = null;
    authState.value = 'anonymous';
  }
}

const avatarChar = computed(() => (me.value?.username || '?').trim().charAt(0).toUpperCase());

function logout() {
  clearToken();
  me.value = null;
  authState.value = 'anonymous';
}

function switchAccount() {
  clearToken();
  me.value = null;
  authState.value = 'anonymous';
}

onMounted(() => {
  checkAuth();
  window.addEventListener('hashchange', onHashChange);
  // ⚠️ 必须监听:api/auth.js 的 handleUnauthorized 会 clearToken() 并派发此事件,
  // 全仓唯一监听者是客户端 App.vue。管理端不接的话 —— token 过期时点任意页面 →
  // 请求 401 → token 已被清、authState 仍是 'ok' → 用户卡在管理端界面看
  // 「数据加载失败」，而不是回登录页,必须手动刷新才能恢复。
  window.addEventListener('auth:unauthorized', () => {
    me.value = null;
    authState.value = 'anonymous';
  });
});
</script>
