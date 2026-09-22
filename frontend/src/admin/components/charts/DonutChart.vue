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

// data 结构(后端 §5.4):[{ name: "处理中", value: 6 }, …]
// colors 按【后端保证的固定顺序】取用 —— 这是后端 ORDER BY 顺序不能随意改的原因。
const props = defineProps({
  data: { type: Array, default: () => [] },
  title: { type: String, default: '' },
  colors: { type: Array, default: () => ['#f59e0b', '#3b82f6', '#10b981'] },
});

const { el, empty } = useChart(
  () => props.data,
  (d) => ({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 12 } },
    color: props.colors,
    series: [{
      type: 'pie',
      radius: ['48%', '68%'],
      center: ['50%', '45%'],
      avoidLabelOverlap: true,
      label: { show: false },
      labelLine: { show: false },
      data: d.map((x) => ({ name: x.name, value: x.value })),
    }],
  }),
  isEmptyPairs
);
</script>
