<template>
  <div>
    <!-- 工具条 -->
    <div class="flex items-center gap-3 mb-4">
      <input
        v-model="keyword"
        type="text"
        placeholder="搜索订单号/商品名/买家"
        class="w-72 border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @keyup.enter="applyFilter"
      />
      <!-- 状态下拉即时生效:若等「查询」才应用,下拉显示值会与列表内容不一致 -->
      <select
        v-model="status"
        class="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @change="applyFilter"
      >
        <option value="">全部</option>
        <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
      </select>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="applyFilter"
      >
        <i class="fas fa-magnifying-glass mr-1.5"></i>查询
      </button>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="openCreate"
      >
        <i class="fas fa-plus mr-1.5"></i>新增订单
      </button>
    </div>

    <!-- 加载失败但已有数据:保留列表,只在顶部提示(§6.4.8) -->
    <p v-if="error && items.length" class="text-sm text-gray-400 mb-3">
      数据加载失败
      <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
    </p>

    <!-- 加载失败且无数据:居中一行灰字 + 重试,不弹窗(§6.4.8) -->
    <div v-if="error && !items.length" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <p class="text-sm text-gray-400">
        数据加载失败
        <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
      </p>
    </div>

    <!-- 骨架条:仅首次加载显示,翻页时不清空已有数据(§6.4.8) -->
    <div v-else-if="loading && !items.length" class="bg-white rounded-xl border border-gray-100 p-4 space-y-3">
      <div v-for="n in 3" :key="n" class="h-10 bg-gray-100 rounded-lg animate-pulse"></div>
    </div>

    <!-- 空态:与表格平级,不塞占列数的 <td>(§6.4.8) -->
    <div v-else-if="!items.length" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <i class="fas fa-inbox text-3xl text-gray-300"></i>
      <p class="text-sm text-gray-400 mt-3">暂无数据</p>
      <p v-if="keyword.trim()" class="text-xs text-gray-400 mt-1">没有匹配「{{ keyword.trim() }}」的记录</p>
    </div>

    <!-- 表格 8 列,表头/行 class 与工单页共用同一组(§6.4.8 表格统一规格) -->
    <div v-else class="overflow-x-auto bg-white rounded-xl border border-gray-100">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-gray-500 text-xs">
          <tr>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-32">订单号</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-[26%]">商品</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">品类</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-32">买家</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-24">金额</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-24">状态</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">下单日期</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">签收日期</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in items"
            :key="row.id"
            class="border-t border-gray-100 hover:bg-gray-50 transition-colors"
          >
            <td class="px-4 py-3 whitespace-nowrap">{{ row.order_no }}</td>
            <!-- 单行截断:truncate 必须配 max-w-0,只写 truncate 在 td 上不生效 -->
            <td class="px-4 py-3 truncate max-w-0 w-[26%]" :title="row.product_name">{{ row.product_name }}</td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.category }}</td>
            <td class="px-4 py-3 whitespace-nowrap">{{ buyerText(row) }}</td>
            <td class="px-4 py-3 whitespace-nowrap text-red-600 font-semibold">¥{{ Number(row.amount).toFixed(2) }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <StatusBadge :text="row.status" :color="STATUS_COLOR[row.status] || 'gray'" />
            </td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.order_date }}</td>
            <!-- 只有已签收才有值,其余状态后端恒返回 null,此处不显示 -->
            <td class="px-4 py-3 whitespace-nowrap">{{ row.signed_date || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <button type="button" class="text-primary hover:text-primary-dark mr-3" @click="openEdit(row)">编辑</button>
              <button type="button" class="text-red-500 hover:text-red-600" @click="remove(row)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- total === 0 时分页条自身隐藏(§6.4.7) -->
    <Pagination
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="onPageChange"
      @update:page-size="onPageSizeChange"
    />

    <!-- 新增/编辑弹窗 -->
    <AdminModal :visible="modalVisible" :title="editing ? '编辑订单' : '新增订单'" @close="closeModal">
      <form @submit.prevent="save">
        <!-- 保存失败:弹窗不关,顶部红色错误条,已填内容保留(§6.4.8) -->
        <p
          v-if="saveError"
          class="mb-4 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2"
        >{{ saveError }}</p>

        <div class="space-y-4">
          <div>
            <label class="block text-sm text-gray-600 mb-1">商品</label>
            <!-- 编辑:只读展示,不给「换商品」入口(sku/商品名/品类后端不可改,§5.6) -->
            <div v-if="editing" class="bg-gray-50 rounded-lg px-3 py-2 text-sm text-gray-700">
              {{ editing.product_name }}<span class="text-gray-400 ml-1">· {{ editing.category }}</span>
            </div>
            <template v-else>
              <select v-model="form.product_sku" :class="INPUT_CLASS" @change="onProductChange">
                <option value="">请选择商品</option>
                <option v-for="p in products" :key="p.sku" :value="p.sku">{{ p.product_name }}</option>
              </select>
              <p v-if="productsError" class="text-xs text-red-500 mt-1">
                商品列表加载失败
                <button type="button" class="text-primary underline ml-1" @click="loadProducts">重试</button>
              </p>
              <p v-else-if="fieldErrors.product_sku" class="text-xs text-red-500 mt-1">{{ fieldErrors.product_sku }}</p>
            </template>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">买家姓名</label>
            <input v-model="form.buyer_name" type="text" :class="INPUT_CLASS" />
            <p v-if="fieldErrors.buyer_name" class="text-xs text-red-500 mt-1">{{ fieldErrors.buyer_name }}</p>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">买家编码</label>
            <input v-model="form.buyer_code" type="text" placeholder="P001" :class="INPUT_CLASS" />
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">金额</label>
            <input
              v-model="form.amount"
              type="number"
              step="0.01"
              min="0"
              :placeholder="editing ? '' : '留空则取商品当前价'"
              :class="INPUT_CLASS"
            />
            <p v-if="fieldErrors.amount" class="text-xs text-red-500 mt-1">{{ fieldErrors.amount }}</p>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">状态</label>
            <select v-model="form.status" :class="INPUT_CLASS">
              <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
            </select>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">下单日期</label>
            <input v-model="form.order_date" type="date" :class="INPUT_CLASS" />
            <p v-if="fieldErrors.order_date" class="text-xs text-red-500 mt-1">{{ fieldErrors.order_date }}</p>
          </div>

          <!-- 仅在「已签收」时渲染:未签收的订单不该有签收日期(后端也强制非已签收置 NULL) -->
          <div v-if="form.status === '已签收'">
            <label class="block text-sm text-gray-600 mb-1">签收日期</label>
            <input v-model="form.signed_date" type="date" :class="INPUT_CLASS" />
          </div>
        </div>

        <div class="flex items-center justify-end gap-3 mt-6">
          <button
            type="button"
            class="text-sm text-gray-500 hover:text-gray-700 border border-gray-200 rounded-lg px-4 py-2 transition-colors"
            @click="closeModal"
          >取消</button>
          <!-- 保存中置灰 + 转圈,禁止重复提交(§6.4.8) -->
          <button
            type="submit"
            :disabled="saving"
            class="bg-primary hover:bg-primary-dark disabled:opacity-60 disabled:cursor-not-allowed text-white rounded-lg px-4 py-2 text-sm transition-colors"
          >
            <i v-if="saving" class="fas fa-spinner fa-spin mr-1.5"></i>{{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </form>
    </AdminModal>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue';
import { createOrder, deleteOrder, listOrders, listProducts, updateOrder } from '../api.js';
import AdminModal from '../components/AdminModal.vue';
import Pagination from '../components/Pagination.vue';
import StatusBadge from '../components/StatusBadge.vue';

const STATUSES = ['处理中', '已发货', '已送达', '已签收'];
// 徽章配色在页面里决定(StatusBadge 不做「传 status 自动配色」,§6.2)
// 「已签收」用 teal 与「已送达」的 green 区分:两者并排出现时靠颜色也能分辨
const STATUS_COLOR = { 处理中: 'amber', 已发货: 'blue', 已送达: 'green', 已签收: 'teal' };
const INPUT_CLASS = 'w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary';

// 买家列:「姓名 编码」,编码为空只显示姓名。
// ⚠️ 不要在模板里写成 <template v-if>{{ x }}</template> —— Vue 的 whitespace 压缩会把
// 拼在标签内的前导空格吃掉,渲染成「沈七P001」,这里用表达式拼死。
const buyerText = (row) => (row.buyer_code ? `${row.buyer_name} ${row.buyer_code}` : row.buyer_name);

// ---- 列表 ----
const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(12);
const keyword = ref('');
const status = ref('');
const loading = ref(false);
const error = ref(false);

const params = () => ({
  page: page.value,
  page_size: pageSize.value,
  keyword: keyword.value.trim(),
  status: status.value,
});

async function load() {
  loading.value = true;
  error.value = false;
  try {
    let res = await listOrders(params());
    const maxPage = Math.max(1, Math.ceil((res.total || 0) / pageSize.value));
    // 删除后当前页越界(在第 3 页删光只剩 2 页)→ 回退最后一页重新拉取(§6.4.7)
    if (page.value > maxPage) {
      page.value = maxPage;
      res = await listOrders(params());
    }
    items.value = res.items || [];
    total.value = res.total || 0;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

// 搜索 / 切换筛选条件一律重置到第 1 页(§6.4.7)
function applyFilter() {
  page.value = 1;
  load();
}

function onPageChange(p) {
  // 切换每页条数时 Pagination 会连带 emit update:page=1,这里挡掉重复请求
  if (p === page.value) return;
  page.value = p;
  load();
}

function onPageSizeChange(size) {
  pageSize.value = size;
  page.value = 1;
  load();
}

async function remove(row) {
  if (!window.confirm(`确定删除订单「${row.order_no}」？删除后不可恢复。`)) return;
  try {
    await deleteOrder(row.id);
    await load();
  } catch (e) {
    window.alert(e.message || '删除失败');
  }
}

// ---- 商品下拉(仅新增时需要,编辑不给换商品) ----
const products = ref([]);
const productsError = ref(false);

async function loadProducts() {
  productsError.value = false;
  try {
    const res = await listProducts({ page_size: 100 });
    products.value = res.items || [];
  } catch {
    products.value = [];
    productsError.value = true;
  }
}

// ---- 新增 / 编辑弹窗 ----
const modalVisible = ref(false);
const editing = ref(null);
const saving = ref(false);
const saveError = ref('');
const fieldErrors = reactive({ product_sku: '', buyer_name: '', amount: '', order_date: '' });

const form = reactive({
  product_sku: '',
  buyer_name: '',
  buyer_code: '',
  amount: '',
  status: '处理中',
  order_date: '',
  signed_date: '',
});

// 本地日期:<input type="date"> 与用户日历一致,不走 toISOString()(那是 UTC,国内会差一天)
function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 用户一改字段就撤掉上一次的红字提示
watch(form, () => {
  Object.keys(fieldErrors).forEach((k) => { fieldErrors[k] = ''; });
});

// 签收日期与状态联动(表单里只在「已签收」时出现):
//   切到「已签收」→ 自动带出今天,用户仍可手改(补录历史订单)
//   切走        → 清空,与后端「非已签收恒置 NULL」的约定保持一致
watch(() => form.status, (next, prev) => {
  if (next === '已签收' && prev !== '已签收') form.signed_date = todayStr();
  else if (next !== '已签收') form.signed_date = '';
});

function openCreate() {
  editing.value = null;
  Object.assign(form, {
    product_sku: '',
    buyer_name: '',
    buyer_code: '',
    amount: '',
    status: '处理中',
    order_date: todayStr(),
    signed_date: '',
  });
  saveError.value = '';
  modalVisible.value = true;
  if (productsError.value) loadProducts();
}

function openEdit(row) {
  editing.value = row;
  Object.assign(form, {
    product_sku: row.sku,
    buyer_name: row.buyer_name || '',
    buyer_code: row.buyer_code || '',
    amount: row.amount,
    status: row.status,
    order_date: row.order_date,
    signed_date: row.signed_date || '',
  });
  saveError.value = '';
  modalVisible.value = true;
}

function closeModal() {
  if (saving.value) return;
  modalVisible.value = false;
}

// 选中商品后带出其当前价(仍可手改;清空则后端取现价,§5.6)
function onProductChange(e) {
  const p = products.value.find((x) => x.sku === e.target.value);
  if (p) form.amount = p.current_price;
}

// 前端预校验:把 422 挡在网络往返之前(§6.4.8)
function validate() {
  fieldErrors.product_sku = !editing.value && !form.product_sku ? '请选择商品' : '';
  fieldErrors.buyer_name = form.buyer_name.trim() ? '' : '请填写买家姓名';
  fieldErrors.order_date = form.order_date ? '' : '请选择下单日期';

  const amount = form.amount === '' ? null : Number(form.amount);
  if (editing.value && amount === null) fieldErrors.amount = '请填写金额';
  else if (amount !== null && !(amount > 0)) fieldErrors.amount = '金额需大于 0';
  else fieldErrors.amount = '';

  return !Object.values(fieldErrors).some(Boolean);
}

async function save() {
  saveError.value = '';
  if (!validate()) return;

  saving.value = true;
  try {
    const body = {
      buyer_name: form.buyer_name.trim(),
      buyer_code: form.buyer_code.trim(),
      amount: form.amount === '' ? '' : Number(form.amount),
      status: form.status,
      order_date: form.order_date,
      // 空串转 null:后端把「非已签收」一律置 NULL,已签收但留空则由后端补当天
      signed_date: form.signed_date || null,
    };
    if (editing.value) await updateOrder(editing.value.id, body);
    else await createOrder({ ...body, product_sku: form.product_sku });

    modalVisible.value = false;
    await load(); // 保持 page / page_size / 筛选条件刷新(§6.4.8)
  } catch (e) {
    saveError.value = e.message || '保存失败';
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  load();
  loadProducts();
});
</script>
