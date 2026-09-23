<template>
  <span
    class="inline-block text-xs px-2 py-0.5 rounded-full whitespace-nowrap"
    :class="colorClass"
  >{{ text }}</span>
</template>

<script setup>
import { computed } from 'vue';

// 只做"文字 + 底色"的映射,不做成"传 status 自动配色"的智能组件 ——
// 三种状态集(订单 / 工单 / 紧急度)取值完全不同,硬合并要传枚举类型。
// 各处颜色映射在页面里决定。
const props = defineProps({
  text: String,
  color: { type: String, default: 'gray' },   // gray|green|blue|amber|red|teal
});

const MAP = {
  gray: 'bg-gray-100 text-gray-600',
  green: 'bg-green-100 text-green-600',
  blue: 'bg-blue-100 text-blue-600',
  amber: 'bg-amber-100 text-amber-600',
  red: 'bg-red-100 text-red-600',
  // 订单「已签收」用,与「已送达」的 green 区分
  teal: 'bg-teal-100 text-teal-600',
};

const colorClass = computed(() => MAP[props.color] || MAP.gray);
</script>
