<template>
  <div class="bg-white rounded-xl border border-gray-100 p-4 shadow-sm">
    <h3 class="text-sm font-medium text-gray-700 mb-3">{{ title }}</h3>
    <div class="relative" style="height: 320px">
      <div ref="el" style="height: 320px"></div>
      <div v-if="empty" class="absolute inset-0 flex items-center justify-center text-gray-400 text-sm">暂无数据</div>
    </div>
  </div>
</template>

<script setup>
import { useChart, isEmptyPairs } from './useChart.js';

// data 结构(后端 §5.4):[{ name: "智能门锁", value: 12 }, …](已按数量降序)
const props = defineProps({
  data: { type: Array, default: () => [] },
  title: { type: String, default: '' },
});

const { el, empty } = useChart(
  () => props.data,
  (d) => ({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 8, bottom: 8, top: 16, containLabel: true },
    xAxis: {
      type: 'category',
      data: d.map((x) => x.name),
      axisLine: { lineStyle: { color: '#e5e7eb' } },
      axisLabel: { color: '#9ca3af', fontSize: 11, interval: 0, rotate: d.length > 6 ? 30 : 0 },
    },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#9ca3af', fontSize: 11 } },
    // 单色柱(参考图也是单色)
    series: [{ type: 'bar', data: d.map((x) => x.value), itemStyle: { color: '#16a34a', borderRadius: [4, 4, 0, 0] }, barMaxWidth: 32 }],
  }),
  isEmptyPairs
);
</script>
