// 三个图表组件共用的生命周期与空态判定(LineChart / BarChart / DonutChart)。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import echarts from './echarts-setup.js';

/**
 * @param {() => any} getData 返回 props.data 的 getter
 * @param {(data: any) => object} buildOption 把数据转成 ECharts option
 * @param {(data: any) => boolean} isEmpty 判定"不该调 setOption"的空/全零态
 */
export function useChart(getData, buildOption, isEmpty) {
  const el = ref(null);
  const empty = ref(true);
  let chart = null;

  const onResize = () => chart && chart.resize();

  function render() {
    const data = getData();
    if (isEmpty(data)) {
      // ECharts 在数据全 0 时一个扇区都不画(环图只剩图例、柱状图只剩坐标轴),
      // 看起来像加载失败 —— 所以这种态直接不调 setOption,渲染居中灰字。
      empty.value = true;
      if (chart) chart.clear();
      return;
    }
    empty.value = false;
    // setOption 第二参 true = 不合并,避免图例/系列残留
    chart.setOption(buildOption(data), true);
  }

  onMounted(() => {
    chart = echarts.init(el.value);
    render();
    window.addEventListener('resize', onResize);
  });

  onBeforeUnmount(() => {
    // 必须 dispose,否则切页面会累积 canvas 与 resize 监听器,久了页面卡顿
    window.removeEventListener('resize', onResize);
    if (chart) {
      chart.dispose();
      chart = null;
    }
  });

  watch(getData, render, { deep: true });

  return { el, empty };
}

/** 空数据 / 全零态判定(单值数组形态:如 trend 的 orders/conversations) */
export function isEmptyNumberArray(data) {
  return !data || !data.length || data.every((v) => !v);
}

/** 空数据 / 全零态判定({name, value}[] 形态:如 order_status / product_category) */
export function isEmptyPairs(data) {
  return !data || !data.length || data.every((d) => !d.value);
}
