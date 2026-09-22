<template>
  <img
    v-if="!failed"
    :src="src"
    :alt="alt"
    :style="{ width: size + 'px', height: size + 'px' }"
    class="rounded-xl object-cover bg-gray-100 shrink-0"
    @error="failed = true"
  >
  <!-- 后端不查文件系统,image 永远返回 /products/{sku}.svg;文件不在就由这里兜底。
       新增商品天然走这个分支,无需任何额外代码。 -->
  <div
    v-else
    class="rounded-xl bg-gray-100 text-gray-400 flex items-center justify-center shrink-0"
    :style="{ width: size + 'px', height: size + 'px' }"
    :title="alt"
  >
    <i class="fas fa-box text-2xl"></i>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue';

const props = defineProps({
  src: String,
  alt: String,
  size: { type: Number, default: 96 },
});

const failed = ref(false);
// 同一组件实例被复用到另一张图时重置兜底态
watch(() => props.src, () => { failed.value = false; });
</script>
