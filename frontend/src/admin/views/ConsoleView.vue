<template>
  <div>
    <div class="flex items-start justify-between gap-6 mb-6">
      <div>
        <h1 class="text-2xl font-bold text-gray-800">管理控制台</h1>
        <p class="text-sm text-gray-400 mt-1">商品、订单、知识库与工单数据概览，供智能客服实时检索与升级处理。</p>
      </div>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm shrink-0 transition-colors"
        @click="openChat"
      >
        <i class="fas fa-comment-dots mr-1.5"></i>体验客服
      </button>
    </div>

    <p v-if="error" class="text-sm text-gray-400 mb-4">
      数据加载失败，请刷新重试
      <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
    </p>

    <!-- 6 张统计卡片 -->
    <div class="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-4">
      <div v-for="card in cards" :key="card.label" class="bg-white rounded-xl border border-gray-100 p-4 shadow-sm">
        <div class="w-9 h-9 rounded-lg flex items-center justify-center mb-3" :class="card.iconBg">
          <i class="fas text-sm" :class="card.icon"></i>
        </div>
        <div class="text-2xl font-bold text-gray-800">{{ card.value }}</div>
        <div class="text-sm text-gray-500 mt-0.5">{{ card.label }}</div>
        <div class="text-xs text-gray-400 mt-0.5 h-4">{{ card.sub }}</div>
      </div>
    </div>

    <!-- 四张图:2×2 -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <LineChart title="近 7 日趋势" :data="charts.trend" />
      <DonutChart title="订单状态分布" :data="charts.order_status" :colors="ORDER_COLORS" />
      <BarChart title="商品品类分布" :data="charts.product_category" />
      <DonutChart title="工单状态" :data="charts.ticket_status" :colors="TICKET_COLORS" />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { getCharts, getStats } from '../api.js';
import LineChart from '../components/charts/LineChart.vue';
import BarChart from '../components/charts/BarChart.vue';
import DonutChart from '../components/charts/DonutChart.vue';

// 颜色按后端保证的固定顺序绑定(spec §6.6)
const ORDER_COLORS = ['#f59e0b', '#3b82f6', '#10b981', '#14b8a6'];   // 处理中/已发货/已送达/已签收
const TICKET_COLORS = ['#ef4444', '#10b981'];             // 待处理/已解决

const stats = ref(null);
const charts = ref({ trend: {}, order_status: [], product_category: [], ticket_status: [] });
const error = ref(false);

const cards = computed(() => {
  const s = stats.value;
  if (!s) return [];
  return [
    { label: '商品', value: s.products.total, sub: `上架 ${s.products.in_stock}`, icon: 'fa-box', iconBg: 'bg-blue-100 text-blue-600' },
    { label: '订单总数', value: s.orders.total, sub: '', icon: 'fa-clipboard-list', iconBg: 'bg-sky-100 text-sky-600' },
    { label: '知识库文档', value: s.knowledge.total, sub: '', icon: 'fa-book', iconBg: 'bg-emerald-100 text-emerald-600' },
    { label: '工单', value: s.tickets.total, sub: `待办 ${s.tickets.pending}`, icon: 'fa-ticket', iconBg: 'bg-amber-100 text-amber-600' },
    { label: '活跃用户', value: s.users.total, sub: '', icon: 'fa-user', iconBg: 'bg-purple-100 text-purple-600' },
    { label: '客服会话', value: s.conversations.total, sub: `消息 ${s.conversations.messages}`, icon: 'fa-comment-dots', iconBg: 'bg-red-100 text-red-600' },
  ];
});

async function load() {
  error.value = false;
  try {
    const [s, c] = await Promise.all([getStats(), getCharts(7)]);
    stats.value = s;
    charts.value = c;
  } catch {
    // 失败只显示一行灰色提示,不弹窗(弹窗会打断后续操作)
    error.value = true;
  }
}

function openChat() {
  window.open('/', '_blank');
}

onMounted(load);
</script>
