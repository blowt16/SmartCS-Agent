<template>
  <AdminModal :visible="true" :title="isEdit ? '编辑文档' : '新增文档'" @close="onCancel">
    <!-- 命中重复:黄色提示条。不阻止继续 —— 用户可能就是想给已有文档补描述 -->
    <div
      v-if="dupHint"
      class="mb-4 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2"
    >
      该文件已存在于知识库（{{ dupHint.chunk_count }} 个片段，创建于 {{ fmtTime(dupHint.created_at) }}）。继续保存只会更新它的文件描述，不会新增一条记录。
    </div>

    <!-- 保存失败:弹窗不关,顶部红色错误条(已填内容不清空) -->
    <div
      v-if="saveError"
      class="mb-4 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2"
    >
      {{ saveError }}
    </div>

    <!-- 上传区:仅新增模式。编辑模式不提供"替换文件"(换文件等于换一条记录,语义上是删了重建) -->
    <div v-if="!isEdit" class="border border-dashed border-gray-200 rounded-xl py-6 text-center">
      <input
        ref="fileEl"
        type="file"
        :accept="ACCEPT"
        class="hidden"
        @change="onFileChange"
      />
      <button
        type="button"
        class="border border-gray-200 rounded-lg px-4 py-2 text-sm text-gray-600 bg-white hover:bg-gray-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="state === 'staging'"
        @click="pickFile"
      >{{ uploadText }}</button>
      <p class="text-xs text-gray-400 mt-3">支持 PDF / Word / TXT / Markdown，单个不超过 30MB</p>
      <p v-if="uploadError" class="text-xs text-red-500 mt-2">{{ uploadError }}</p>
    </div>

    <p v-if="!isEdit && showInfo" class="mt-5 text-xs text-gray-400 text-center">
      以下为上传后自动解析的基本信息，不可编辑
    </p>

    <!-- 只读信息区:值是纯文本节点 + 浅灰底,【不用】disabled 的 input
         (disabled 输入框视觉上仍像"能填但被禁用",用户会反复点击试图编辑) -->
    <div
      v-if="showInfo"
      class="mt-2 grid grid-cols-[80px_1fr] gap-y-3 items-center"
    >
      <div class="text-xs text-gray-400">文件名</div>
      <div class="min-w-0 text-sm text-gray-700 bg-gray-50 rounded px-3 py-1.5 truncate" :title="info.filename">{{ info.filename }}</div>

      <div class="text-xs text-gray-400">文件类型</div>
      <div class="text-sm text-gray-700 bg-gray-50 rounded px-3 py-1.5">{{ info.fileType }}</div>

      <div class="text-xs text-gray-400">文件大小</div>
      <div class="text-sm text-gray-700 bg-gray-50 rounded px-3 py-1.5">{{ info.fileSize }}</div>

      <div class="text-xs text-gray-400">片段数</div>
      <div class="text-sm text-gray-700 bg-gray-50 rounded px-3 py-1.5">
        {{ info.chunkCount }}
        <span v-if="!isEdit" class="text-xs text-gray-400 ml-1">保存后生成</span>
      </div>

      <div class="text-xs text-gray-400">创建时间</div>
      <div class="text-sm text-gray-700 bg-gray-50 rounded px-3 py-1.5">
        {{ info.createdAt }}
        <span v-if="!isEdit" class="text-xs text-gray-400 ml-1">保存后生成</span>
      </div>

      <!-- 唯一可编辑项 -->
      <div class="text-xs text-gray-400 pt-2 self-start">文件描述</div>
      <textarea
        v-model="description"
        rows="3"
        class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
      ></textarea>

      <!-- 状态:仅编辑模式(新建一律 enabled) -->
      <template v-if="isEdit">
        <div class="text-xs text-gray-400">状态</div>
        <select
          v-model="status"
          class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        >
          <option value="enabled">启用</option>
          <option value="disabled">停用</option>
        </select>
      </template>
    </div>

    <div class="mt-6 flex items-center justify-end gap-3">
      <button
        type="button"
        class="border border-gray-200 rounded-lg px-4 py-2 text-sm text-gray-600 bg-white hover:bg-gray-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="state === 'staging'"
        @click="onCancel"
      >取消</button>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="!canSave || saving"
        @click="onSave"
      >
        <i v-if="saving" class="fas fa-spinner fa-spin mr-1.5"></i>保存
      </button>
    </div>
  </AdminModal>
</template>

<script setup>
import { computed, ref } from 'vue';
import { stageFile, unstageKnowledge, updateKnowledge } from '../api.js';
import { startCommit } from '../knowledgeJob.js';
import AdminModal from './AdminModal.vue';

// 暂存式表单(spec §6.4.5):先暂存文件(不索引),补完描述再提交索引。
// 三个状态 idle → staging → staged;【没有 committed 态】—— 点「保存」弹窗立即关闭,
// 索引在页面上由进度条呈现(§6.4.5.2)。
const props = defineProps({
  mode: { type: String, default: 'create' },      // create | edit
  record: { type: Object, default: null },        // edit 模式的列表行
  userId: { type: [String, Number], default: '' }, // stage 的 user_id(取自 getMe().id)
});
const emit = defineEmits(['close', 'saved']);

// 与后端 settings.allowed_extensions / MAX_FILE_SIZE_MB 一致(见 llm_backend/app/core/config.py)
const ALLOWED_EXTS = ['txt', 'md', 'pdf', 'docx'];
const MAX_FILE_MB = 30;
const ACCEPT = '.pdf,.doc,.docx,.txt,.md';

const isEdit = computed(() => props.mode === 'edit');

const state = ref('idle');
const fileEl = ref(null);
const staged = ref(null);            // stage 的返回:{md5, original_filename, file_type, file_size, duplicate, existing}
const uploadError = ref('');         // 预检/上传失败的红字(停在上传区下方)
const saveError = ref('');           // 保存失败的红色错误条(编辑模式 PATCH)
const description = ref(props.record?.description || '');
const status = ref(props.record?.status || 'enabled');
const saving = ref(false);

const uploadText = computed(() => {
  if (state.value === 'staging') return '上传中…';
  return state.value === 'staged' ? '重新上传' : '上传文件';
});

const showInfo = computed(() => isEdit.value || state.value === 'staged');
const canSave = computed(() => isEdit.value || state.value === 'staged');

const dupHint = computed(() => (staged.value?.duplicate ? staged.value.existing : null));

const info = computed(() => {
  if (isEdit.value) {
    const r = props.record || {};
    return {
      filename: r.original_filename || '—',
      fileType: r.file_type || '—',
      fileSize: r.file_size == null ? '—' : toKb(r.file_size),
      chunkCount: r.chunk_count ?? '—',
      createdAt: fmtTime(r.created_at),
    };
  }
  const s = staged.value;
  if (!s) return null;
  return {
    filename: s.original_filename,
    fileType: s.file_type,
    fileSize: toKb(s.file_size),
    chunkCount: '—',
    createdAt: '—',
  };
});

// 文件大小一律 KB 保留 1 位(stage 返回的 file_size 是字节数)
function toKb(bytes) {
  return `${(Number(bytes) / 1024).toFixed(1)} KB`;
}

// 日期一律字符串切片,不经过 new Date()(§6.4.8)
function fmtTime(v) {
  return v ? `${v.slice(0, 10)} ${v.slice(11, 16)}` : '—';
}

function pickFile() {
  uploadError.value = '';
  fileEl.value?.click();
}

async function onFileChange(e) {
  const file = e.target.files?.[0];
  e.target.value = '';                 // 复位,否则同一文件再次选择不触发 change
  if (!file) return;
  uploadError.value = '';

  // 本地预检:把必然失败的请求挡在网络往返之前
  const ext = (file.name.split('.').pop() || '').toLowerCase();
  if (!ALLOWED_EXTS.includes(ext)) {
    uploadError.value = `不支持的文件格式: .${ext}`;
    return;                            // 停在 idle,不发请求
  }
  if (file.size > MAX_FILE_MB * 1024 * 1024) {
    uploadError.value = `文件大小超过限制(最大 ${MAX_FILE_MB}MB)`;
    return;
  }

  // 重新上传:先撤销上一次的暂存,否则 idle 态下磁盘会留下残留(只能等 24h TTL 的机会式清理)
  if (staged.value) {
    try { await unstageKnowledge(staged.value.md5); } catch { /* 删不掉不阻塞:下一次 stage 的机会式清理会兜底 */ }
    staged.value = null;
  }

  state.value = 'staging';
  try {
    const res = await stageFile({
      file,
      userId: props.userId,
      // stageFile 无条件调用 onProgress,必须传函数;本表单不用上传百分比(按钮文案固定「上传中…」)
      onProgress: () => {},
    });
    staged.value = res;
    // 命中重复:把已有描述带出来,让用户看到的正是"继续保存会写进去的那份"
    if (res.duplicate && res.existing?.description) description.value = res.existing.description;
    state.value = 'staged';
  } catch (err) {
    uploadError.value = err.message || '上传失败';
    state.value = 'idle';
  }
}

async function onSave() {
  if (!canSave.value || saving.value) return;

  if (isEdit.value) {
    saving.value = true;
    saveError.value = '';
    try {
      await updateKnowledge(props.record.md5, { description: description.value, status: status.value });
      emit('saved');
      emit('close');
    } catch (err) {
      saveError.value = err.message || '保存失败';   // 弹窗不关,保留已填内容
    } finally {
      saving.value = false;
    }
    return;
  }

  const s = staged.value;
  if (!s) return;
  // 立即关闭弹窗,不等索引(不阻塞用户——索引期间还能翻页/搜别的/切页面)
  const payload = { md5: s.md5, original_filename: s.original_filename, description: description.value };
  emit('close');
  startCommit(payload);
}

async function onCancel() {
  // staging:请求进行中,按钮已禁用,ESC/× 也一并忽略(避免删一个正在写的文件)
  if (state.value === 'staging') return;

  if (state.value === 'staged' && staged.value) {
    // 撤销暂存:数据库里从头到尾没有过痕迹,所以不弹确认
    try { await unstageKnowledge(staged.value.md5); } catch { /* 失败留给机会式清理,不阻塞关闭 */ }
    staged.value = null;
  }
  emit('close');
}
</script>
