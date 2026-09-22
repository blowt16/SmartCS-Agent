<template>
  <div>
    <!-- 工具条:工单由客服会话升级产生,管理端只处理不新建 —— 本页【没有】「新增」按钮 -->
    <div class="flex items-center gap-3 mb-4">
      <input
        v-model="draft.keyword"
        type="text"
        placeholder="搜索工单号/摘要/用户原话"
        class="w-72 bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
        @keyup.enter="search"
      />
      <select
        v-model="draft.status"
        class="bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
      >
        <option value="">全部</option>
        <option value="待处理">待处理</option>
        <option value="已解决">已解决</option>
      </select>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="search"
      >查询</button>
    </div>

    <!-- 加载中:3 行骨架条,渲染在 <table> 之外(§6.4.8);仅首次加载显示,翻页时不闪白 -->
    <div v-if="loading && !items.length" class="bg-white rounded-xl border border-gray-100 p-4 space-y-3">
      <div v-for="i in 3" :key="i" class="h-8 rounded-lg bg-gray-100 animate-pulse"></div>
    </div>

    <!-- 加载失败:居中灰字 + 重试,不弹窗(§6.4.8) -->
    <div v-else-if="error" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <p class="text-sm text-gray-400">
        数据加载失败
        <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
      </p>
    </div>

    <!-- 表头/行样式见 §6.4.8「表格统一规格」,与订单页共用同一组 class -->
    <div v-else-if="items.length" class="overflow-x-auto bg-white rounded-xl border border-gray-100">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-gray-500 text-xs">
          <tr>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">工单号</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">摘要</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">类别</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">紧急度</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">用户原话</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">状态</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">创建时间</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in items"
            :key="row.id"
            class="border-t border-gray-100 hover:bg-gray-50 transition-colors"
          >
            <td class="px-4 py-3 whitespace-nowrap w-44">{{ row.ticket_no }}</td>
            <!-- 单行截断必须 truncate max-w-0 + 显式宽度:只写 truncate 会被内容撑开而失效 -->
            <td class="px-4 py-3 truncate max-w-0 w-52" :title="row.summary">{{ row.summary }}</td>
            <td class="px-4 py-3 whitespace-nowrap w-28">{{ row.category || '-' }}</td>
            <td class="px-4 py-3 whitespace-nowrap w-20">
              <StatusBadge :text="row.urgency || '-'" :color="URGENCY_COLOR[row.urgency] || 'gray'" />
            </td>
            <td class="px-4 py-3 truncate max-w-0 w-full" :title="row.user_query">{{ row.user_query }}</td>
            <td class="px-4 py-3 whitespace-nowrap w-24">
              <StatusBadge :text="row.status" :color="STATUS_COLOR[row.status] || 'gray'" />
            </td>
            <td class="px-4 py-3 whitespace-nowrap w-40">{{ fmtDateTime(row.created_at) }}</td>
            <td class="px-4 py-3 whitespace-nowrap w-20">
              <button
                type="button"
                class="text-primary hover:text-primary-dark transition-colors"
                @click="openModal(row)"
              >处理</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 空态:渲染在 <table> 之外,不塞占满列数的 <td>(列数会变,塞进去每次加列都要改 colspan) -->
    <div v-else class="bg-white rounded-xl border border-gray-100 py-16 text-center text-gray-400">
      <i class="fas fa-inbox text-3xl"></i>
      <p class="mt-3 text-sm">暂无数据</p>
      <p v-if="filters.keyword" class="mt-1 text-xs">没有匹配「{{ filters.keyword }}」的记录</p>
    </div>

    <!-- 分页条照常渲染(接口返回的是分页对象,不做"一页放得下就隐藏"的特例);total===0 时它自己隐藏 -->
    <Pagination
      v-if="!error"
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="changePage"
      @update:page-size="changePageSize"
    />

    <!-- 处理弹窗:字段与只读性见 §6.4.6;工单号 / 用户原话只读置灰 -->
    <AdminModal :visible="modalVisible" title="处理工单" width="640px" @close="closeModal">
      <p
        v-if="saveError"
        class="mb-4 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2"
      >{{ saveError }}</p>

      <div class="space-y-4">
        <div>
          <label class="block text-sm text-gray-600 mb-1">工单号</label>
          <input
            v-model="form.ticket_no"
            type="text"
            readonly
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-gray-50 text-gray-500 cursor-not-allowed focus:outline-none"
          />
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">用户原话</label>
          <textarea
            v-model="form.user_query"
            rows="2"
            readonly
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-gray-50 text-gray-500 cursor-not-allowed focus:outline-none"
          ></textarea>
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">摘要</label>
          <input
            v-model="form.summary"
            type="text"
            :maxlength="SUMMARY_MAX"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          />
          <div class="text-right text-xs text-gray-400 mt-1">{{ form.summary.length }} / {{ SUMMARY_MAX }}</div>
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">问题类别</label>
          <input
            v-model="form.category"
            type="text"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          />
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">紧急度</label>
          <select
            v-model="form.urgency"
            class="w-full bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          >
            <option v-for="u in URGENCIES" :key="u" :value="u">{{ u }}</option>
          </select>
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">状态</label>
          <select
            v-model="form.status"
            class="w-full bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          >
            <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
          </select>
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">问题详情</label>
          <textarea
            v-model="form.detail"
            rows="4"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          ></textarea>
        </div>

        <div>
          <label class="block text-sm text-gray-600 mb-1">处理建议</label>
          <textarea
            v-model="form.suggestion"
            rows="4"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-primary"
          ></textarea>
        </div>
      </div>

      <div class="flex justify-end gap-3 mt-5">
        <button
          type="button"
          class="border border-gray-200 text-gray-600 hover:bg-gray-50 rounded-lg px-4 py-2 text-sm transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          :disabled="saving"
          @click="closeModal"
        >取消</button>
        <button
          type="button"
          class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          :disabled="saving"
          @click="save"
        >
          <i v-if="saving" class="fas fa-spinner fa-spin mr-1.5"></i>保存
        </button>
      </div>
    </AdminModal>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import { listTickets, updateTicket } from '../api.js';
import AdminModal from '../components/AdminModal.vue';
import Pagination from '../components/Pagination.vue';
import StatusBadge from '../components/StatusBadge.vue';

// 颜色映射在【页面里】决定,不把 StatusBadge 做成"传 status 自动配色"的智能组件(§6.2)
const URGENCY_COLOR = { 低: 'green', 中: 'amber', 高: 'red' };
const STATUS_COLOR = { 待处理: 'red', 已解决: 'green' };
const URGENCIES = ['低', '中', '高'];
const STATUSES = ['待处理', '已解决'];
const SUMMARY_MAX = 200;   // 后端 summary 列为 String(200),超出会被 pydantic 挡成 422

// 工具条草稿与已应用的筛选条件分开:输入框里改字不触发查询,点「查询」才生效
const draft = ref({ keyword: '', status: '' });
const filters = ref({ keyword: '', status: '' });

const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(12);
const loading = ref(false);
const error = ref(false);

// 列表永远是分页对象 {total, page, page_size, items} —— 不是裸数组
function fetchPage() {
  return listTickets({
    page: page.value,
    page_size: pageSize.value,
    keyword: filters.value.keyword,
    status: filters.value.status,
  });
}

async function load() {
  loading.value = true;
  error.value = false;
  try {
    let res = await fetchPage();
    // 越界回退(§6.4.7):筛选/并发删单后当前页可能超过总页数,不处理就是一张空白页
    const lastPage = Math.max(1, Math.ceil(res.total / pageSize.value));
    if (page.value > lastPage) {
      page.value = lastPage;
      res = await fetchPage();
    }
    items.value = res.items;
    total.value = res.total;
  } catch {
    // 失败只显示一行灰字与「重试」,不弹窗(弹窗会打断后续操作)
    items.value = [];
    total.value = 0;
    error.value = true;
  } finally {
    loading.value = false;
  }
}

function search() {
  filters.value = { keyword: draft.value.keyword.trim(), status: draft.value.status };
  page.value = 1;   // 搜索/切筛选一律重置到第 1 页(§6.4.7)
  load();
}

// Pagination 切换每页条数时会依次 emit update:pageSize 与 update:page=1;
// 页码相同直接跳过,否则一次操作会打两次请求,响应乱序就翻错页
function changePage(p) {
  if (p === page.value) return;
  page.value = p;
  load();
}

function changePageSize(size) {
  pageSize.value = size;
  page.value = 1;
  load();
}

// 日期一律字符串切片,不经过 new Date()(§6.4.8):后端给的是朴素 UTC isoformat
function fmtDateTime(s) {
  if (!s) return '-';
  return `${s.slice(0, 10)} ${s.slice(11, 16)}`;
}

// ---- 处理弹窗 ----
const modalVisible = ref(false);
const saving = ref(false);
const saveError = ref('');
const editingId = ref(null);
const form = ref({
  ticket_no: '',
  user_query: '',
  summary: '',
  category: '',
  urgency: '中',
  status: '待处理',
  detail: '',
  suggestion: '',
});

function openModal(row) {
  editingId.value = row.id;
  // 列表 item 已是全字段,弹窗不再二次请求
  form.value = {
    ticket_no: row.ticket_no,
    user_query: row.user_query,
    summary: row.summary || '',
    category: row.category || '',
    urgency: row.urgency || '中',
    status: row.status,
    detail: row.detail || '',
    suggestion: row.suggestion || '',
  };
  saveError.value = '';
  modalVisible.value = true;
}

// 保存中不关:请求已发出,关掉会让用户以为取消了
function closeModal() {
  if (saving.value) return;
  modalVisible.value = false;
}

async function save() {
  saving.value = true;
  saveError.value = '';
  try {
    const f = form.value;
    // ticket_no / user_query 不可改,后端 schema 里也没有这两个字段
    await updateTicket(editingId.value, {
      summary: f.summary,
      category: f.category,
      urgency: f.urgency,
      status: f.status,
      detail: f.detail,
      suggestion: f.suggestion,
    });
    modalVisible.value = false;
    load();   // 刷新当前页,保持 page / page_size / 筛选条件不变(§6.4.8)
  } catch (e) {
    // 失败弹窗不关,顶部红条提示,已填内容原样保留
    saveError.value = e.message || '保存失败';
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>
