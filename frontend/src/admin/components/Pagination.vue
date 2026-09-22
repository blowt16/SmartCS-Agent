<template>
  <!-- total === 0 时整条分页栏隐藏(由各页面渲染「暂无数据」空态代替) -->
  <div v-if="total > 0" class="flex items-center justify-end gap-4 mt-4 text-sm text-gray-600">
    <span>共 {{ total }} 条</span>

    <select
      :value="pageSize"
      class="border border-gray-200 rounded-lg px-2 py-1 text-sm bg-white focus:outline-none focus:border-primary"
      @change="onPageSizeChange"
    >
      <option v-for="s in PAGE_SIZES" :key="s" :value="s">{{ s }} 条/页</option>
    </select>

    <div class="flex items-center gap-1">
      <button
        type="button"
        class="w-7 h-7 rounded-lg border border-gray-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50"
        :disabled="page <= 1"
        @click="emit('update:page', page - 1)"
      >
        <i class="fas fa-chevron-left text-xs"></i>
      </button>

      <button
        v-for="p in pageNumbers"
        :key="p"
        type="button"
        class="w-7 h-7 rounded-lg border text-sm transition-colors"
        :class="p === page
          ? 'bg-primary text-white border-primary'
          : 'border-gray-200 hover:bg-gray-50'"
        @click="emit('update:page', p)"
      >{{ p }}</button>

      <button
        type="button"
        class="w-7 h-7 rounded-lg border border-gray-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50"
        :disabled="page >= totalPages"
        @click="emit('update:page', page + 1)"
      >
        <i class="fas fa-chevron-right text-xs"></i>
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue';

const props = defineProps({
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 12 },
});
const emit = defineEmits(['update:page', 'update:pageSize']);

const PAGE_SIZES = [12, 24, 48];

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)));

// 页码窗口:最多显示 9 个,当前页居中
const pageNumbers = computed(() => {
  const n = totalPages.value;
  if (n <= 9) return Array.from({ length: n }, (_, i) => i + 1);
  let start = Math.max(1, props.page - 4);
  const end = Math.min(n, start + 8);
  start = Math.max(1, end - 8);
  return Array.from({ length: end - start + 1 }, (_, i) => start + i);
});

// 切换每页条数必须重置到第 1 页 —— 保持当前页会导致越界或跳过数据
function onPageSizeChange(e) {
  emit('update:pageSize', Number(e.target.value));
  emit('update:page', 1);
}
</script>
