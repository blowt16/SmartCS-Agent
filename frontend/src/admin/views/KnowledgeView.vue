<template>
  <div>
    <!-- 工具条:进度块加 ml-auto 靠右 + v-if 不占位 —— 否则「新增文档」按钮会左右跳动(§6.4.5.2) -->
    <div class="flex items-center gap-3 mb-4">
      <input
        v-model="keywordInput"
        type="text"
        placeholder="搜索文档编号/文件名"
        class="w-72 border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @keyup.enter="onSearch"
      />
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="onSearch"
      >查询</button>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="knowledgeJob.active"
        @click="openCreate"
      >新增文档</button>

      <!-- 索引进度:一次只允许一个任务,处理中「新增文档」禁用 -->
      <Transition
        enter-active-class="transition-opacity duration-200"
        leave-active-class="transition-opacity duration-500"
        enter-from-class="opacity-0"
        leave-to-class="opacity-0"
      >
        <div v-if="jobVisible" class="ml-auto shrink-0 text-xs">
          <div class="flex items-center gap-2">
            <div class="w-40 h-1.5 bg-gray-100 rounded-full overflow-hidden">
              <div
                class="h-full rounded-full transition-all duration-300"
                :class="barClass"
                :style="{ width: barWidth }"
              ></div>
            </div>
            <!-- w-9 text-right 防数字位数变化(9% → 10%)时抖动 -->
            <span v-if="knowledgeJob.active" class="w-9 text-right text-gray-500">{{ knowledgeJob.percent }}%</span>
            <template v-else-if="knowledgeJob.error">
              <span class="text-red-600 whitespace-nowrap">处理失败</span>
              <button
                type="button"
                class="w-5 h-5 rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                aria-label="关闭"
                @click="dismissJob"
              ><i class="fas fa-xmark"></i></button>
            </template>
          </div>
          <div class="mt-1 max-w-[320px] truncate" :class="jobTextClass" :title="jobText">{{ jobText }}</div>
        </div>
      </Transition>
    </div>

    <!-- 加载失败:居中一行灰字 + 重试,不弹窗(弹窗会打断后续操作) -->
    <div v-if="error" class="bg-white rounded-xl border border-gray-100 py-16 text-center text-sm text-gray-400">
      数据加载失败
      <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
    </div>

    <!-- 骨架条:仅首次加载显示,不清空已有数据(翻页时不闪白) -->
    <div v-else-if="loading && !items.length" class="bg-white rounded-xl border border-gray-100 p-4 space-y-3">
      <div v-for="i in 3" :key="i" class="h-10 bg-gray-100 rounded-lg animate-pulse"></div>
    </div>

    <!-- 空态与表格平级,不塞占满列数的 td(列数会随需求变,每次改列都要同步改 colspan) -->
    <div v-else-if="!items.length" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <i class="fas fa-inbox text-3xl text-gray-300"></i>
      <p class="text-sm text-gray-400 mt-3">暂无数据</p>
      <p v-if="keyword" class="text-xs text-gray-400 mt-1">没有匹配「{{ keyword }}」的记录</p>
    </div>

    <div v-else class="overflow-x-auto bg-white rounded-xl border border-gray-100">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-gray-500 text-xs">
          <tr>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-24">文档编号</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-[24%]">文件名</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">文件描述</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-20">片段数</th>
            <th
              class="text-left font-medium px-4 py-3 whitespace-nowrap w-20"
              title="停用仅影响管理端展示，智能客服仍会检索到该文档（已知限制）"
            >状态</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-40">创建时间</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.md5" class="border-t border-gray-100 hover:bg-gray-50 transition-colors">
            <td class="px-4 py-3 whitespace-nowrap">{{ row.id }}</td>
            <!-- 单行截断:truncate max-w-0 + 显式宽度(只写 truncate 在 td 上不生效) -->
            <td class="px-4 py-3 truncate max-w-0 w-[24%]" :title="row.original_filename">{{ row.original_filename }}</td>
            <td class="px-4 py-3">{{ row.description || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.chunk_count }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <StatusBadge
                :text="row.status === 'enabled' ? '启用' : '停用'"
                :color="row.status === 'enabled' ? 'green' : 'gray'"
              />
            </td>
            <td class="px-4 py-3 whitespace-nowrap">{{ fmtTime(row.created_at) }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <button type="button" class="text-primary hover:text-primary-dark mr-3" @click="openEdit(row)">编辑</button>
              <button type="button" class="text-red-500 hover:text-red-600" @click="onDelete(row)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="onPageChange"
      @update:page-size="onPageSizeChange"
    />

    <!-- v-if 每次打开都是新实例:状态机从 idle 起,不会串上一次的暂存 -->
    <KnowledgeFormModal
      v-if="modalOpen"
      :mode="modalMode"
      :record="modalRecord"
      :user-id="meId"
      @saved="load"
      @close="modalOpen = false"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { deleteKnowledge, listKnowledge } from '../api.js';
import { getMe } from '../../api/auth.js';
import { dismissJob, knowledgeJob } from '../knowledgeJob.js';
import Pagination from '../components/Pagination.vue';
import StatusBadge from '../components/StatusBadge.vue';
import KnowledgeFormModal from '../components/KnowledgeFormModal.vue';

const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(12);
const keywordInput = ref('');   // 输入框里的
const keyword = ref('');        // 已生效的(用于请求与空态文案)
const loading = ref(false);
const error = ref(false);

const modalOpen = ref(false);
const modalMode = ref('create');
const modalRecord = ref(null);
const meId = ref('');           // stage 的 user_id

// 日期一律字符串切片,不经过 new Date()(§6.4.8)
function fmtTime(v) {
  return v ? `${v.slice(0, 10)} ${v.slice(11, 16)}` : '—';
}

async function load() {
  loading.value = true;
  error.value = false;
  try {
    const res = await listKnowledge({
      page: page.value,
      page_size: pageSize.value,
      keyword: keyword.value,
    });
    // 删到当前页越界(在第 3 页删光只剩 2 页)→ 回退到最后一页重拉(§6.4.7)
    const lastPage = Math.max(1, Math.ceil(res.total / pageSize.value));
    if (page.value > lastPage) {
      page.value = lastPage;
      return load();
    }
    items.value = res.items;
    total.value = res.total;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  keyword.value = keywordInput.value.trim();
  page.value = 1;              // 搜索重置到第 1 页(§6.4.7)
  load();
}

function onPageChange(p) {
  page.value = p;
  load();
}

function onPageSizeChange(s) {
  pageSize.value = s;
  page.value = 1;              // 切换每页条数重置到第 1 页(§6.4.7)
  load();
}

function openCreate() {
  modalMode.value = 'create';
  modalRecord.value = null;
  modalOpen.value = true;
}

function openEdit(row) {
  modalMode.value = 'edit';
  modalRecord.value = row;
  modalOpen.value = true;
}

async function onDelete(row) {
  const ok = window.confirm(
    `确定删除文档「${row.original_filename}」？将同时删除其 ${row.chunk_count} 个知识片段，智能客服不再检索到它。`
  );
  if (!ok) return;
  try {
    await deleteKnowledge(row.md5);
  } catch (err) {
    window.alert(err.message || '删除失败');
    return;
  }
  load();   // 保持当前页与筛选条件
}

// ---- 进度块三态 ----
const jobVisible = computed(() => knowledgeJob.active || knowledgeJob.done || !!knowledgeJob.error);

const barClass = computed(() => {
  if (knowledgeJob.error) return 'bg-red-500';
  if (knowledgeJob.done) return 'bg-primary';
  return 'bg-gradient-to-r from-primary to-primary-light';
});

// 失败时 percent 已被 store 重置为 0,固定满格才看得见这条红(否则红条宽度为 0 = 不可见)
const barWidth = computed(() => (knowledgeJob.error ? '100%' : `${knowledgeJob.percent}%`));

// 成功提示必须带文档名:用户可能正在第 3 页,新增的文档排在第一页,不带名字就要去列表里找
const jobText = computed(() => {
  if (knowledgeJob.error) return knowledgeJob.error;
  if (knowledgeJob.done) return `已完成 · ${knowledgeJob.fileName} · ${knowledgeJob.detail}`;
  return `${knowledgeJob.stage}${knowledgeJob.detail ? ' · ' + knowledgeJob.detail : ''}`;
});

const jobTextClass = computed(() => {
  if (knowledgeJob.error) return 'text-red-600';
  return knowledgeJob.done ? 'text-primary' : 'text-gray-400';
});

// 1) 完成后刷新列表(拉到当前页,保持 page / page_size / 筛选条件)
watch(() => knowledgeJob.doneId, (id) => { if (id) load(); });

// 2) 成功态 1.5s 后自动清掉(淡出由进度块的 transition-opacity 承担)
//    定时器里再确认一次 done:这 1.5s 内用户若又发起一次索引,新任务不该被上一次的清掉
watch(() => knowledgeJob.done, (v) => {
  if (v) setTimeout(() => { if (knowledgeJob.done) dismissJob(); }, 1500);
});

onMounted(() => {
  load();
  // 弹窗 stage 时要用 me.id 作 user_id。拉不到就留空:不影响列表,失败会由后端 400 呈现
  getMe().then((me) => { meId.value = String(me.id); }).catch(() => {});
});
</script>
