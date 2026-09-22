<template>
  <div
    v-if="visible"
    class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-6"
    style="background: rgba(15, 23, 42, 0.45)"
  >
    <!-- 点遮罩【不关闭】:表单填了一半误点会丢数据,只有右上角 × 与「取消」按钮触发 close -->
    <div
      class="bg-white rounded-2xl shadow-lg border border-gray-100 w-full my-auto"
      :style="{ maxWidth: width }"
    >
      <div class="flex items-center justify-between px-6 py-4 border-b border-gray-100">
        <h3 class="text-base font-semibold text-gray-800">{{ title }}</h3>
        <button
          type="button"
          class="text-gray-400 hover:text-gray-600 transition-colors w-7 h-7 rounded-lg hover:bg-gray-100"
          aria-label="关闭"
          @click="$emit('close')"
        >
          <i class="fas fa-xmark"></i>
        </button>
      </div>

      <div class="px-6 py-5">
        <slot />
      </div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, watch } from 'vue';

// 行为约定(见 spec §6.2 / §6.4.5):
//   - 点遮罩不关闭;ESC 关闭
//   - 打开时 body 加 overflow:hidden,关闭时移除
//   - 表单内容全部由父组件通过默认插槽传入,弹窗自身不持有表单状态
//   - z-index 50(高于导航栏的 40)
const props = defineProps({
  visible: Boolean,
  title: String,
  width: { type: String, default: '560px' },
});
const emit = defineEmits(['close']);

function onKeydown(e) {
  if (e.key === 'Escape') emit('close');
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      document.body.style.overflow = 'hidden';
      window.addEventListener('keydown', onKeydown);
    } else {
      document.body.style.overflow = '';
      window.removeEventListener('keydown', onKeydown);
    }
  },
  { immediate: true }
);

onBeforeUnmount(() => {
  document.body.style.overflow = '';
  window.removeEventListener('keydown', onKeydown);
});
</script>
