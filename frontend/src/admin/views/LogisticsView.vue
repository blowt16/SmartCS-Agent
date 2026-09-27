<template>
  <div>
    <!-- 工具条 -->
    <div class="flex items-center gap-3 mb-4">
      <input
        v-model="keyword"
        type="text"
        placeholder="搜索订单号/运单号/商品名/买家/承运商"
        class="w-80 border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @keyup.enter="applyFilter"
      />
      <!-- 下拉即时生效:若等「查询」才应用,下拉显示值会与列表内容不一致 -->
      <select
        v-model="status"
        class="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @change="applyFilter"
      >
        <option value="">全部物流状态</option>
        <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
        <!-- 未录入排在最后:方便筛出待补录的 -->
        <option value="未录入">未录入</option>
      </select>
      <select
        v-model="orderStatus"
        class="border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary"
        @change="applyFilter"
      >
        <option value="">全部订单状态</option>
        <!-- 四个值全给:订单建过运单后可能被改回「处理中」,那种行只能靠它筛出来 -->
        <option v-for="s in ORDER_STATUSES" :key="s" :value="s">{{ s }}</option>
      </select>
      <button
        type="button"
        class="bg-primary hover:bg-primary-dark text-white rounded-lg px-4 py-2 text-sm transition-colors"
        @click="applyFilter"
      >
        <i class="fas fa-magnifying-glass mr-1.5"></i>查询
      </button>
    </div>

    <!-- 加载失败但已有数据:保留列表,只在顶部提示 -->
    <p v-if="error && items.length" class="text-sm text-gray-400 mb-3">
      数据加载失败
      <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
    </p>

    <!-- 加载失败且无数据:居中一行灰字 + 重试,不弹窗 -->
    <div v-if="error && !items.length" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <p class="text-sm text-gray-400">
        数据加载失败
        <button type="button" class="text-primary hover:text-primary-dark ml-2" @click="load">重试</button>
      </p>
    </div>

    <!-- 骨架条:仅首次加载显示,翻页时不清空已有数据 -->
    <div v-else-if="loading && !items.length" class="bg-white rounded-xl border border-gray-100 p-4 space-y-3">
      <div v-for="n in 3" :key="n" class="h-10 bg-gray-100 rounded-lg animate-pulse"></div>
    </div>

    <!-- 空态:与表格平级 -->
    <div v-else-if="!items.length" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
      <i class="fas fa-inbox text-3xl text-gray-300"></i>
      <p class="text-sm text-gray-400 mt-3">暂无数据</p>
      <p v-if="keyword.trim()" class="text-xs text-gray-400 mt-1">没有匹配「{{ keyword.trim() }}」的记录</p>
    </div>

    <!-- 表格 8 列,表头/行 class 与订单页共用同一组 -->
    <div v-else class="overflow-x-auto bg-white rounded-xl border border-gray-100">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-gray-500 text-xs">
          <tr>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-40">运单号</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-32">订单号</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-[24%]">商品</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">承运商</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">物流状态</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">发货时间</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">签收时间</th>
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-28">操作</th>
          </tr>
        </thead>
        <tbody>
          <!-- key 用 order_no 不是 id:未录入行的 id 是 null,v-for 的 key 会重复 -->
          <tr
            v-for="row in items"
            :key="row.order_no"
            class="border-t border-gray-100 hover:bg-gray-50 transition-colors"
          >
            <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">{{ row.tracking_no || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <!-- 未录入的行点开没内容可看,不做成链接 -->
              <button
                v-if="row.id !== null"
                type="button"
                class="text-primary hover:text-primary-dark"
                @click="openDetail(row)"
              >{{ row.order_no }}</button>
              <span v-else class="cursor-default text-gray-500" title="尚未录入物流">{{ row.order_no }}</span>
            </td>
            <!-- 单行截断:truncate 必须配 max-w-0,只写 truncate 在 td 上不生效 -->
            <td class="px-4 py-3 truncate max-w-0 w-[24%]" :title="row.product_name">{{ row.product_name }}</td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.carrier || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <StatusBadge
                v-if="row.status"
                :text="row.status"
                :color="STATUS_COLOR[row.status] || 'gray'"
              />
              <StatusBadge v-else text="未录入" color="gray" />
            </td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.shipped_at || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">{{ row.signed_at || '—' }}</td>
            <td class="px-4 py-3 whitespace-nowrap">
              <template v-if="row.id !== null">
                <button type="button" class="text-primary hover:text-primary-dark mr-3" @click="openEdit(row)">编辑</button>
                <button type="button" class="text-red-500 hover:text-red-600" @click="remove(row)">删除</button>
              </template>
              <button v-else type="button" class="text-primary hover:text-primary-dark" @click="openCreate(row)">补录</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- total === 0 时分页条自身隐藏 -->
    <Pagination
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="onPageChange"
      @update:page-size="onPageSizeChange"
    />

    <!-- 补录 / 编辑弹窗 -->
    <AdminModal
      :visible="modalVisible"
      :title="editing ? '编辑物流' : '补录物流'"
      width="640px"
      @close="closeModal"
    >
      <form @submit.prevent="save">
        <p
          v-if="saveError"
          class="mb-4 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2"
        >{{ saveError }}</p>

        <div class="space-y-4">
          <div>
            <label class="block text-sm text-gray-600 mb-1">订单</label>
            <!-- 纯文本,不是 select:订单由被点的那一行带入,不可换 -->
            <div class="bg-gray-50 rounded-lg px-3 py-2 text-sm text-gray-700">
              {{ currentOrderText }}
            </div>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">运单号</label>
            <input v-model="form.tracking_no" type="text" maxlength="50" :class="INPUT_CLASS" />
            <p v-if="fieldErrors.tracking_no" class="text-xs text-red-500 mt-1">{{ fieldErrors.tracking_no }}</p>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">承运商</label>
            <select v-model="form.carrier" :class="INPUT_CLASS">
              <option v-for="c in CARRIERS" :key="c" :value="c">{{ c }}</option>
            </select>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">物流状态</label>
            <select v-model="form.status" :class="INPUT_CLASS">
              <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
            </select>
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">发货时间</label>
            <input v-model="form.shipped_at" type="date" :class="INPUT_CLASS" />
          </div>

          <!-- 仅在「已签收」时渲染 -->
          <div v-if="form.status === '已签收'">
            <label class="block text-sm text-gray-600 mb-1">签收时间</label>
            <input v-model="form.signed_at" type="date" :class="INPUT_CLASS" />
          </div>

          <div>
            <label class="block text-sm text-gray-600 mb-1">轨迹</label>
            <textarea
              v-model="form.trace"
              rows="7"
              :maxlength="TRACE_MAX"
              class="w-full border rounded-lg px-3 py-2 text-sm bg-white focus:outline-none font-mono"
              :class="traceError ? 'border-red-300 focus:border-red-400' : 'border-gray-200 focus:border-primary'"
              :placeholder="TRACE_PLACEHOLDER"
            ></textarea>
            <p class="text-xs mt-1" :class="traceError ? 'text-red-500' : 'text-gray-400'">
              <template v-if="traceError">{{ traceError }}</template>
              <template v-else>每行一个节点，格式：<code>YYYY-MM-DD HH:MM | 地点 | 描述</code></template>
              <span class="ml-1">{{ (form.trace || '').length }} / {{ TRACE_MAX }}</span>
            </p>
          </div>
        </div>

        <div class="flex items-center justify-end gap-3 mt-6">
          <button
            type="button"
            class="text-sm text-gray-500 hover:text-gray-700 border border-gray-200 rounded-lg px-4 py-2 transition-colors"
            @click="closeModal"
          >取消</button>
          <button
            type="submit"
            :disabled="saving || !!traceError"
            class="bg-primary hover:bg-primary-dark disabled:opacity-60 disabled:cursor-not-allowed text-white rounded-lg px-4 py-2 text-sm transition-colors"
          >
            <i v-if="saving" class="fas fa-spinner fa-spin mr-1.5"></i>{{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </form>
    </AdminModal>

    <!-- 只读详情弹窗 -->
    <AdminModal :visible="detailVisible" title="物流详情" @close="detailVisible = false">
      <template v-if="detail">
        <p class="text-sm text-gray-700 mb-4">
          {{ detail.order_no }} · {{ detail.product_name }} · {{ detail.buyer_name }}
          <span class="ml-2 text-xs text-gray-400">订单状态：{{ detail.order_status }}</span>
        </p>
        <p class="text-sm text-gray-600 mb-4">
          运单 <span class="font-mono">{{ detail.tracking_no }}</span>
          · {{ detail.carrier }}
          <StatusBadge :text="detail.status" :color="STATUS_COLOR[detail.status] || 'gray'" class="ml-2" />
          <span class="ml-3 text-xs text-gray-400">发货 {{ detail.shipped_at || '—' }}　签收 {{ detail.signed_at || '—' }}</span>
        </p>

        <div v-if="traceRows.length" class="space-y-0">
          <div v-for="(t, i) in traceRows" :key="i" class="flex gap-3 text-sm">
            <div class="w-28 shrink-0 text-gray-400 text-xs pt-0.5 whitespace-nowrap">{{ t.date }} {{ t.time }}</div>
            <div class="w-4 shrink-0 flex flex-col items-center">
              <span class="w-2 h-2 rounded-full bg-primary mt-1.5"></span>
              <span v-if="i < traceRows.length - 1" class="w-px flex-1 bg-gray-200"></span>
            </div>
            <div class="pb-4">
              <span class="text-gray-500 mr-3">{{ t.loc }}</span>
              <span class="text-gray-700">{{ t.desc }}</span>
            </div>
          </div>
        </div>
        <p v-else class="text-sm text-gray-400">暂无轨迹</p>
      </template>
    </AdminModal>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import {
  createShipment, deleteShipment, listLogistics, updateShipment,
} from '../api.js';
import AdminModal from '../components/AdminModal.vue';
import Pagination from '../components/Pagination.vue';
import StatusBadge from '../components/StatusBadge.vue';

const STATUSES = ['待揽收', '已揽收', '运输中', '派送中', '已签收', '异常'];
const ORDER_STATUSES = ['处理中', '已发货', '已送达', '已签收'];
const CARRIERS = ['京东物流', '顺丰速运', '中通快递', '圆通速递',
                  '申通快递', '韵达快递', '邮政EMS', '德邦快递'];
// 徽章配色在页面里决定(StatusBadge 不做「传 status 自动配色」)
const STATUS_COLOR = {
  待揽收: 'gray', 已揽收: 'blue', 运输中: 'green',
  派送中: 'amber', 已签收: 'teal', 异常: 'red',
};
const TRACE_MAX = 2000;   // 与后端 schemas/admin.py 的 TRACE_MAX 保持一致
const TRACE_PLACEHOLDER =
  '一行一个节点，例如：\n' +
  '2026-09-20 14:32 | 广州市 | 已揽收\n' +
  '2026-09-21 08:05 | 广州转运中心 | 到达转运中心';
const INPUT_CLASS = 'w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:border-primary';

// ---- 列表 ----
const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(12);
const keyword = ref('');
const status = ref('');
const orderStatus = ref('');
const loading = ref(false);
const error = ref(false);

const params = () => ({
  page: page.value,
  page_size: pageSize.value,
  keyword: keyword.value.trim(),
  status: status.value,
  order_status: orderStatus.value,
});

async function load() {
  loading.value = true;
  error.value = false;
  try {
    let res = await listLogistics(params());
    const maxPage = Math.max(1, Math.ceil((res.total || 0) / pageSize.value));
    // 删除后当前页越界(在第 3 页删光只剩 2 页)→ 回退最后一页重新拉取
    if (page.value > maxPage) {
      page.value = maxPage;
      res = await listLogistics(params());
    }
    items.value = res.items || [];
    total.value = res.total || 0;
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
}

// 搜索 / 切换筛选条件一律重置到第 1 页
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
  if (!window.confirm(`确定删除运单「${row.tracking_no}」？该订单会回到「未录入」状态，可重新补录。`)) return;
  try {
    await deleteShipment(row.id);
    await load();
  } catch (e) {
    window.alert(e.message || '删除失败');
  }
}

// ---- 补录 / 编辑弹窗 ----
const modalVisible = ref(false);
const editing = ref(null);
const currentRow = ref(null);
const saving = ref(false);
const saveError = ref('');
const fieldErrors = reactive({ tracking_no: '' });
const traceError = ref('');

const form = reactive({
  tracking_no: '', carrier: CARRIERS[0], status: '待揽收',
  shipped_at: '', signed_at: '', trace: '',
});

const currentOrderText = computed(() => {
  const r = currentRow.value;
  return r ? `${r.order_no} · ${r.product_name} · ${r.buyer_name}` : '';
});

// 本地日期:<input type="date"> 与用户日历一致,不走 toISOString()(那是 UTC,国内会差一天)
function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 用户一改运单号就撤掉上一次的红字提示
watch(() => form.tracking_no, () => { fieldErrors.tracking_no = ''; });

// 轨迹逐行校验:规则与后端 §4.5 规则 C 一致,把错误挡在网络往返之前。
// 后端仍是权威判定(前端用字符串比较,看不出 2026-13-45 这种非法日期)。
watch(() => [form.trace, form.shipped_at, form.signed_at], () => {
  traceError.value = validateTrace(form.trace, form.shipped_at, form.signed_at);
});

// 返回第一条错误的中文描述;全部通过返回 ''
// ⚠️ 行号口径必须与后端 parse_trace 一致:按用户在 textarea 里看到的行数编号
//    (空行占位),不能先丢空行再编号 —— 否则空行之后的所有行号前移,红字指到空行上
function validateTrace(raw, shippedAt, signedAt) {
  const lines = (raw || '').replace(/\r\n?/g, '\n').split('\n');
  const nodes = [];
  for (let i = 0; i < lines.length; i++) {
    const n = i + 1;
    const line = lines[i].trim();
    if (!line) continue;                       // 空行忽略,不算错
    const parts = line.split('|');
    if (parts.length !== 3) return `第 ${n} 行：应为「时间 | 地点 | 描述」三段，实际 ${parts.length} 段`;
    const [ts, loc, desc] = parts.map((p) => p.trim());
    if (!loc) return `第 ${n} 行：地点不能为空`;
    if (!desc) return `第 ${n} 行：描述不能为空`;
    if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(ts)) return `第 ${n} 行：时间要写成 2026-09-20 14:32 这样`;
    nodes.push({ lineNo: n, ts, loc, desc, day: ts.slice(0, 10) });
  }
  if (!nodes.length) return '';
  for (let i = 1; i < nodes.length; i++) {
    if (nodes[i].ts <= nodes[i - 1].ts) return `第 ${nodes[i].lineNo} 行：时间要比上一行晚`;
  }
  if (shippedAt && nodes[0].day < shippedAt) {
    return `第 ${nodes[0].lineNo} 行：不能早于发货时间 ${shippedAt}`;
  }
  if (signedAt && nodes[nodes.length - 1].day > signedAt) {
    return `第 ${nodes[nodes.length - 1].lineNo} 行：不能晚于签收时间 ${signedAt}`;
  }
  // 长度按【规范化之后】算:规范化每行加 4 个字符(`|` -> ` | `),后端也是这么卡的。
  // 上面的正则已保证 ts 是规范形态,所以这里拼出来的就是后端会存下来的那个字符串。
  const normalized = nodes.map((x) => `${x.ts} | ${x.loc} | ${x.desc}`).join('\n');
  if (normalized.length > TRACE_MAX) {
    return `轨迹规范化后 ${normalized.length} 字，超过上限 ${TRACE_MAX} 字`;
  }
  return '';
}

// 签收时间与状态的联动。⚠️ 不能照抄 OrderView.vue:339-342 —— 那段 watcher
// 分不清「用户手动切换」与「回填」,回填时会把库里的签收日期覆盖成今天
// (详见 spec §9⑤)。这里用 filling 标记在回填期间短路它。
let filling = false;
watch(() => form.status, (next, prev) => {
  if (filling) return;
  if (next === '已签收' && prev !== '已签收') form.signed_at = todayStr();
  else if (next !== '已签收') form.signed_at = '';
});

function openCreate(row) {
  editing.value = null;
  currentRow.value = row;
  filling = true;
  Object.assign(form, {
    tracking_no: '', carrier: CARRIERS[0], status: '待揽收',
    shipped_at: todayStr(), signed_at: '', trace: '',
  });
  nextTick(() => { filling = false; });
  saveError.value = '';
  traceError.value = '';
  modalVisible.value = true;
}

function openEdit(row) {
  editing.value = row;
  currentRow.value = row;
  filling = true;
  Object.assign(form, {
    tracking_no: row.tracking_no,
    carrier: row.carrier,
    status: row.status,
    shipped_at: row.shipped_at || '',
    signed_at: row.signed_at || '',
    trace: row.trace || '',
  });
  nextTick(() => { filling = false; });
  saveError.value = '';
  traceError.value = '';
  modalVisible.value = true;
}

function closeModal() {
  if (saving.value) return;
  modalVisible.value = false;
}

function validate() {
  fieldErrors.tracking_no = form.tracking_no.trim() ? '' : '请填写运单号';
  traceError.value = validateTrace(form.trace, form.shipped_at, form.signed_at);
  return !fieldErrors.tracking_no && !traceError.value;
}

async function save() {
  saveError.value = '';
  if (!validate()) return;

  saving.value = true;
  try {
    const body = {
      tracking_no: form.tracking_no.trim(),
      carrier: form.carrier,
      status: form.status,
      // 空串要变 null:cleanBody 会把 '' 整个键摘掉,后端 exclude_unset 会判定
      // 「没传」= 保留旧值 —— 那样这两个日期和轨迹就永远清不掉了
      shipped_at: form.shipped_at || null,
      signed_at: form.signed_at || null,
      trace: form.trace || null,
    };
    if (editing.value) await updateShipment(editing.value.id, body);
    else await createShipment({ ...body, order_no: currentRow.value.order_no });

    modalVisible.value = false;
    await load(); // 保持 page / page_size / 筛选条件刷新
  } catch (e) {
    saveError.value = e.message || '保存失败';
  } finally {
    saving.value = false;
  }
}

// ---- 详情弹窗(只用行数据,不发额外请求) ----
const detailVisible = ref(false);
const detail = ref(null);

function openDetail(row) {
  detail.value = row;
  detailVisible.value = true;
}

// 写入时后端已规范化(三段式 / LF 分隔 / 无空行),所以这里可以放心 split。
// 仍然兜底:脏数据(比如直连库写进去的)别把弹窗炸成白屏。
const traceRows = computed(() => {
  const raw = detail.value?.trace || '';
  return raw.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const [ts = '', loc = '', desc = ''] = l.split('|').map((p) => p.trim());
    return { date: ts.slice(0, 10), time: ts.slice(11), loc, desc };
  });
});

onMounted(load);
</script>
