<template>
  <aside
    class="docs-panel hidden lg:flex flex-col border-l"
    :class="[
      docsPanelOpen ? 'w-80' : 'collapsed',
      isDark ? 'bg-gray-800 border-gray-700' : 'bg-white border-gray-100'
    ]"
  >
    <!-- 标题栏 -->
    <div class="p-5 border-b flex items-center justify-between" :class="isDark ? 'border-gray-700' : 'border-gray-100'">
      <div class="flex items-center gap-2">
        <i class="fas fa-book text-primary"></i>
        <span class="font-semibold" :class="isDark ? 'text-white' : 'text-gray-800'">知识库</span>
      </div>
      <button
        @click="emit('close')"
        class="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
        :class="isDark ? 'bg-gray-700 text-gray-300 hover:bg-gray-600' : 'bg-gray-100 text-gray-500 hover:bg-gray-200'"
      >
        <i class="fas fa-times"></i>
      </button>
    </div>

    <!-- 上传区域 -->
    <div class="p-5">
      <div
        @click="docInput.click()"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="handleDocDrop"
        class="border-2 border-dashed rounded-2xl p-6 text-center cursor-pointer transition-all duration-200"
        :class="[
          dragOver
            ? 'border-primary bg-primary-bg'
            : isDark
              ? 'border-gray-600 hover:border-primary hover:bg-gray-700'
              : 'border-gray-200 hover:border-primary hover:bg-primary-bg/50'
        ]"
      >
        <input
          type="file"
          ref="docInput"
          accept=".pdf,.doc,.docx,.txt"
          multiple
          class="hidden"
          @change="handleDocSelect"
        >
        <i class="fas fa-cloud-upload-alt text-3xl text-primary mb-3"></i>
        <p class="text-sm font-medium mb-1" :class="isDark ? 'text-white' : 'text-gray-700'">
          拖拽文件到此处或点击上传
        </p>
        <p class="text-xs" :class="isDark ? 'text-gray-400' : 'text-gray-500'">
          支持 PDF / Word / TXT
        </p>
      </div>
    </div>

    <!-- 上传进度 -->
    <div v-if="uploadProgress > 0 && uploadProgress < 100" class="px-5 pb-5">
      <div class="rounded-full overflow-hidden" :class="isDark ? 'bg-gray-700' : 'bg-gray-100'">
        <div
          class="h-1.5 bg-gradient-to-r from-primary to-primary-light transition-all duration-300"
          :style="{ width: uploadProgress + '%' }"
        ></div>
      </div>
      <p class="text-xs text-center mt-2" :class="isDark ? 'text-gray-400' : 'text-gray-500'">
        上传中... {{ uploadProgress }}%
      </p>
    </div>

    <!-- 文档列表 -->
    <div class="flex-1 overflow-y-auto px-5 pb-5">
      <div
        v-for="doc in documents"
        :key="doc.id"
        class="group flex items-center gap-3 p-3 rounded-xl mb-2 transition-colors"
        :class="isDark ? 'bg-gray-700 hover:bg-gray-600' : 'bg-gray-50 hover:bg-gray-100'"
      >
        <!-- 文件图标 -->
        <div
          class="w-10 h-10 rounded-xl flex items-center justify-center"
          :class="getDocIconBg(doc.type)"
        >
          <i :class="[getDocIcon(doc.type), 'text-lg']"></i>
        </div>

        <!-- 文件信息 -->
        <div class="flex-1 min-w-0">
          <p class="text-sm font-medium truncate" :class="isDark ? 'text-white' : 'text-gray-800'">
            {{ doc.name }}
          </p>
          <div class="flex items-center gap-2">
            <span class="text-xs" :class="isDark ? 'text-gray-400' : 'text-gray-500'">
              {{ formatFileSize(doc.size) }}
            </span>
            <span v-if="doc.processing" class="text-xs text-amber-500 flex items-center gap-1">
              <i class="fas fa-spinner animate-spin"></i>
              处理中...
            </span>
          </div>
        </div>

        <!-- 删除按钮 -->
        <button
          @click="emit('delete-document', doc.id)"
          class="opacity-0 group-hover:opacity-100 w-8 h-8 rounded-lg flex items-center justify-center transition-all"
          :class="isDark ? 'bg-gray-600 text-gray-300 hover:bg-red-500 hover:text-white' : 'bg-gray-200 text-gray-500 hover:bg-red-500 hover:text-white'"
        >
          <i class="fas fa-trash text-xs"></i>
        </button>
      </div>

      <!-- 空状态 -->
      <div v-if="documents.length === 0" class="text-center py-8">
        <i class="fas fa-file-alt text-3xl mb-3" :class="isDark ? 'text-gray-600' : 'text-gray-300'"></i>
        <p class="text-sm" :class="isDark ? 'text-gray-500' : 'text-gray-400'">暂无文档</p>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { ref } from 'vue';
import { getDocIcon, getDocIconBg, formatFileSize } from '../utils/format.js';

const props = defineProps({
  isDark: { type: Boolean, default: false },
  docsPanelOpen: { type: Boolean, default: true },
  documents: { type: Array, default: () => [] },
  uploadProgress: { type: Number, default: 0 },
});

const emit = defineEmits(['close', 'files-selected', 'drop-files', 'delete-document']);

const dragOver = ref(false);
const docInput = ref(null);

function handleDocSelect(e) {
  emit('files-selected', Array.from(e.target.files));
  e.target.value = '';
}

function handleDocDrop(e) {
  dragOver.value = false;
  emit('drop-files', Array.from(e.dataTransfer.files));
}
</script>
