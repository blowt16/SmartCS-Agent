<template>
  <div>
    <!-- 工具条 -->
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <input
        v-model="keywordInput"
        type="text"
        placeholder="搜索商品名/SKU"
        class="w-72 border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
        @keyup.enter="applyFilters"
      >
      <select
        v-model="categoryInput"
        class="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white outline-none transition-colors focus:border-primary"
      >
        <option value="">全部品类</option>
        <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
      </select>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="applyFilters"
      >查询</button>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="openCreate"
      >新增商品</button>
    </div>

    <!-- 加载失败:居中一行灰字 + 重试,不弹窗 -->
    <div v-if="error" class="py-16 text-center text-sm text-gray-400">
      数据加载失败
      <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
    </div>

    <!-- 首次加载中:3 行骨架条。已有数据时不显示,翻页期间旧数据留在屏上(不清空) -->
    <div v-else-if="loading && !items.length" class="space-y-3">
      <div v-for="n in 3" :key="n" class="bg-white rounded-xl border border-gray-100 p-4 shadow-sm">
        <div class="animate-pulse h-4 bg-gray-100 rounded w-1/3 mb-3"></div>
        <div class="animate-pulse h-3 bg-gray-100 rounded w-2/3"></div>
      </div>
    </div>

    <!-- 空态 -->
    <div v-else-if="!items.length" class="py-16 text-center text-gray-400">
      <i class="fas fa-inbox text-3xl"></i>
      <p class="mt-3 text-sm">暂无数据</p>
      <p v-if="keyword" class="mt-1 text-xs">没有匹配「{{ keyword }}」的记录</p>
    </div>

    <!-- 卡片网格 -->
    <div v-else class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
      <div
        v-for="item in items"
        :key="item.sku"
        class="bg-white rounded-xl border border-gray-100 p-4 shadow-sm"
      >
        <div class="flex gap-3">
          <!-- 图片兜底由 ProductThumb 内部处理(文件不在 → 灰块 + 箱子图标) -->
          <ProductThumb :src="item.image" :alt="item.product_name" :size="96" />

          <div class="min-w-0 flex-1">
            <div class="text-sm font-medium text-gray-800 line-clamp-2" :title="item.product_name">
              {{ item.product_name }}
            </div>
            <div class="mt-1.5">
              <!-- 库存派生的展示徽章,不是商品状态字段 -->
              <StatusBadge
                :text="item.stock_quantity > 0 ? '上架' : '无货'"
                :color="item.stock_quantity > 0 ? 'green' : 'red'"
              />
            </div>
            <div class="mt-1.5 flex items-baseline gap-2">
              <span class="text-red-500 font-bold">{{ fmtPrice(item.current_price) }}</span>
              <span class="text-xs text-gray-400">{{ fmtDate(item.updated_at) }}</span>
            </div>
            <div class="mt-1 text-xs text-gray-500 truncate">
              {{ item.category }} · 库存 {{ item.stock_quantity }}
            </div>
          </div>
        </div>

        <div class="flex justify-end gap-3 mt-3 text-sm">
          <button type="button" class="text-primary hover:text-primary-dark transition-colors" @click="openEdit(item)">编辑</button>
          <button type="button" class="text-red-500 hover:text-red-600 transition-colors" @click="onDelete(item)">删除</button>
        </div>
      </div>
    </div>

    <!-- total === 0 时组件自身隐藏 -->
    <Pagination
      v-if="!error"
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="onPageChange"
      @update:page-size="onPageSizeChange"
    />

    <!-- 新增/编辑弹窗:表单状态由本页持有(AdminModal 自身不持有状态、点遮罩不关闭) -->
    <AdminModal :visible="modalVisible" :title="mode === 'create' ? '新增商品' : '编辑商品'" @close="closeModal">
      <!-- novalidate:关掉浏览器原生的气泡校验(如 step 不匹配),校验一律走本页的红字提示 -->
      <form novalidate @submit.prevent="submit">
        <!-- 保存失败:红色错误条,弹窗不关、已填内容保留 -->
        <p v-if="formError" class="mb-4 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          {{ formError }}
        </p>

        <div class="mb-4">
          <label class="block text-sm text-gray-600 mb-1.5">商品编码</label>
          <input
            v-model.trim="form.sku"
            type="text"
            placeholder="JD-XXX-000"
            :readonly="mode === 'edit'"
            class="w-full border rounded-lg px-3 py-2 text-sm outline-none transition-colors"
            :class="mode === 'edit'
              ? 'bg-gray-100 text-gray-500 border-gray-200 cursor-not-allowed'
              : 'border-gray-200 focus:border-primary'"
          >
          <p v-if="errors.sku" class="mt-1 text-xs text-red-500">{{ errors.sku }}</p>
        </div>

        <div class="mb-4">
          <label class="block text-sm text-gray-600 mb-1.5">商品名称</label>
          <input
            v-model.trim="form.product_name"
            type="text"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
          >
          <p v-if="errors.product_name" class="mt-1 text-xs text-red-500">{{ errors.product_name }}</p>
        </div>

        <div class="mb-4">
          <label class="block text-sm text-gray-600 mb-1.5">品类</label>
          <input
            v-model.trim="form.category"
            list="product-categories"
            type="text"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
          >
          <!-- 既有品类提示,不限制成下拉:新品类应当能直接敲进来 -->
          <datalist id="product-categories">
            <option v-for="c in categories" :key="c" :value="c"></option>
          </datalist>
          <p v-if="errors.category" class="mt-1 text-xs text-red-500">{{ errors.category }}</p>
        </div>

        <div class="mb-4">
          <label class="block text-sm text-gray-600 mb-1.5">当前价格</label>
          <input
            v-model.number="form.current_price"
            type="number"
            step="0.01"
            min="0.01"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
          >
          <p v-if="errors.current_price" class="mt-1 text-xs text-red-500">{{ errors.current_price }}</p>
        </div>

        <div class="mb-4">
          <label class="block text-sm text-gray-600 mb-1.5">库存数量</label>
          <input
            v-model.number="form.stock_quantity"
            type="number"
            step="1"
            min="0"
            class="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none transition-colors focus:border-primary"
          >
          <p v-if="errors.stock_quantity" class="mt-1 text-xs text-red-500">{{ errors.stock_quantity }}</p>
        </div>

        <div class="flex justify-end gap-3 mt-6">
          <button
            type="button"
            class="border border-gray-200 text-gray-600 hover:bg-gray-50 rounded-lg px-4 py-2 text-sm transition-colors"
            @click="closeModal"
          >取消</button>
          <button
            type="submit"
            :disabled="saving"
            class="bg-primary hover:bg-primary-dark disabled:opacity-60 disabled:cursor-not-allowed text-white rounded-lg px-4 py-2 text-sm transition-colors"
          >
            <i v-if="saving" class="fas fa-spinner fa-spin mr-1.5"></i>保存
          </button>
        </div>
      </form>
    </AdminModal>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import { createProduct, deleteProduct, listCategories, listProducts, updateProduct } from '../api.js';
import AdminModal from '../components/AdminModal.vue';
import Pagination from '../components/Pagination.vue';
import ProductThumb from '../components/ProductThumb.vue';
import StatusBadge from '../components/StatusBadge.vue';

// 与后端 ProductCreate 的 sku 约束一致:本地先挡,不合法不发请求(把 422 挡在网络往返之前)
const SKU_RE = /^JD-[A-Z]{3}-\d{3}$/;

const PAGE_SIZE_DEFAULT = 12;

// ---- 列表状态 ----
const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(PAGE_SIZE_DEFAULT);
const loading = ref(false);
const error = ref(false);

// 输入框里的条件 与 已生效的条件 分成两组:重试要重跑"上一次的请求",
// 不能拿用户刚敲到一半、还没点查询的字去请求
const keywordInput = ref('');
const categoryInput = ref('');
const keyword = ref('');
const category = ref('');

const categories = ref([]);

function fmtPrice(v) {
  // 后端 Decimal 序列化后尾零会被吃掉(9957.50 → 9957.5),展示一律补两位
  return `¥${Number(v).toFixed(2)}`;
}

function fmtDate(v) {
  // 只做字符串切片,不经过 new Date()(时区会把日期带偏一天)
  return v ? v.slice(0, 10) : '—';
}

async function load() {
  loading.value = true;
  error.value = false;
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
      keyword: keyword.value,
      category: category.value,
    };
    let data = await listProducts(params);

    // 删除后当前页可能越界(在第 3 页删光、只剩 2 页):回退到最后一页并重新拉取
    const lastPage = Math.max(1, Math.ceil(data.total / pageSize.value));
    if (page.value > lastPage) {
      page.value = lastPage;
      data = await listProducts({ ...params, page: lastPage });
    }

    items.value = data.items;
    total.value = data.total;
  } catch {
    // 失败只显示一行灰字 + 重试按钮,不弹窗
    error.value = true;
  } finally {
    loading.value = false;
  }
}

// 搜索 / 切换筛选条件一律重置到第 1 页(保持当前页会越界或跳过数据)
function applyFilters() {
  keyword.value = keywordInput.value.trim();
  category.value = categoryInput.value;
  page.value = 1;
  load();
}

function onPageChange(p) {
  // 每页条数下拉会连续 emit update:pageSize 与 update:page=1,
  // 页码没变就不再拉一次,否则切条数会发两个请求
  if (p === page.value) return;
  page.value = p;
  load();
}

function onPageSizeChange(size) {
  pageSize.value = size;
  page.value = 1;
  load();
}

// ---- 弹窗与表单 ----
const modalVisible = ref(false);
const mode = ref('create');          // create | edit
const editingSku = ref('');          // 编辑时的原 sku(路径参数,不随表单变)
const originalName = ref('');        // 用于判断名称是否被改动
const form = ref({ sku: '', product_name: '', category: '', current_price: '', stock_quantity: '' });
const errors = ref({});
const formError = ref('');
const saving = ref(false);

function openCreate() {
  mode.value = 'create';
  editingSku.value = '';
  originalName.value = '';
  form.value = { sku: '', product_name: '', category: '', current_price: '', stock_quantity: '' };
  errors.value = {};
  formError.value = '';
  modalVisible.value = true;
}

function openEdit(item) {
  mode.value = 'edit';
  editingSku.value = item.sku;       // sku 不可改(§5.5):它只是定位用的路径参数
  originalName.value = item.product_name;
  form.value = {
    sku: item.sku,
    product_name: item.product_name,
    category: item.category,
    current_price: item.current_price,
    stock_quantity: item.stock_quantity,
  };
  errors.value = {};
  formError.value = '';
  modalVisible.value = true;
}

function closeModal() {
  modalVisible.value = false;
}

function validate() {
  const e = {};

  if (mode.value === 'create') {
    const sku = form.value.sku.trim();
    if (!sku) e.sku = '请输入商品编码';
    else if (!SKU_RE.test(sku)) e.sku = '商品编码格式不正确，形如 JD-XXX-000';
  }

  if (!form.value.product_name.trim()) e.product_name = '请输入商品名称';
  if (!form.value.category.trim()) e.category = '请输入品类';

  const price = form.value.current_price;
  if (price === '' || price === null || Number.isNaN(Number(price))) e.current_price = '请输入当前价格';
  else if (!(Number(price) > 0)) e.current_price = '价格必须大于 0';

  const stock = form.value.stock_quantity;
  if (stock === '' || stock === null || Number.isNaN(Number(stock))) e.stock_quantity = '请输入库存数量';
  else if (!Number.isInteger(Number(stock)) || Number(stock) < 0) e.stock_quantity = '库存必须是不小于 0 的整数';

  errors.value = e;
  return Object.keys(e).length === 0;
}

async function submit() {
  if (saving.value) return;          // 禁止重复提交
  formError.value = '';
  if (!validate()) return;           // 预校验不过不发请求

  // 改名会影响智能客服的检索命中,先确认(校验通过后再问,避免"确认了却因别处不合法没提交")
  if (mode.value === 'edit' && form.value.product_name.trim() !== originalName.value) {
    if (!confirm('修改商品名称会影响智能客服对该商品的检索命中（知识库中的旧名称对不上），确认修改？')) return;
  }

  saving.value = true;
  try {
    if (mode.value === 'create') {
      await createProduct({
        sku: form.value.sku.trim(),
        product_name: form.value.product_name.trim(),
        category: form.value.category.trim(),
        current_price: Number(form.value.current_price),
        stock_quantity: Number(form.value.stock_quantity),
      });
    } else {
      await updateProduct(editingSku.value, {
        product_name: form.value.product_name.trim(),
        category: form.value.category.trim(),
        current_price: Number(form.value.current_price),
        stock_quantity: Number(form.value.stock_quantity),
      });
    }
    closeModal();
    // 刷新当前页:page / page_size / 筛选条件都不动
    await load();
  } catch (e) {
    // 保存失败弹窗不关,把后端 detail 显示在顶部错误条,已填内容保留
    formError.value = e.message || '保存失败，请重试';
  } finally {
    saving.value = false;
  }
}

async function onDelete(item) {
  if (!confirm(`确定删除商品 ${item.sku}？删除后智能客服将查不到该商品的价格与库存（已入库的静态知识仍会返回）。`)) return;
  try {
    await deleteProduct(item.sku);
    // load() 内含"当前页越界则回退到最后一页"的处理
    await load();
  } catch (e) {
    alert(e.message || '删除失败，请重试');
  }
}

onMounted(() => {
  load();
  // 品类下拉拉不到不阻断页面:退化成只有「全部品类」一个选项
  listCategories()
    .then((list) => { categories.value = list; })
    .catch(() => {});
});
</script>
