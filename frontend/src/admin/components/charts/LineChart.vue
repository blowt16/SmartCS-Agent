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
import { useChart, isEmptyNumberArray } from './useChart.js';

// data 结构(后端 §5.4):{ days: ["09-16",…], orders: [...], conversations: [...] }
const props = defineProps({
  data: { type: Object, default: () => ({}) },
  title: { type: String, default: '' },
});

const { el, empty } = useChart(
  () => props.data,
  (d) => ({
    // 图例一律交给 ECharts 的 LegendComponent(与另外两张图统一实现方式,不混用 HTML 自绘图例)
    legend: { right: 0, top: 0, data: ['订单', '客服会话'], itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 12 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 8, right: 8, bottom: 8, top: 40, containLabel: true },
    xAxis: { type: 'category', data: d.days || [], boundaryGap: false, axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#9ca3af', fontSize: 11 } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#9ca3af', fontSize: 11 } },
    series: [
      { name: '订单', type: 'line', smooth: true, data: d.orders || [], itemStyle: { color: '#16a34a' }, lineStyle: { width: 2 } },
      { name: '客服会话', type: 'line', smooth: true, data: d.conversations || [], itemStyle: { color: '#3b82f6' }, lineStyle: { width: 2 } },
    ],
  }),
  (d) => isEmptyNumberArray(d?.orders) && isEmptyNumberArray(d?.conversations)
);
</script>
