// ECharts 按需引入(减小 admin chunk 体积;客户端 bundle 不受影响,只被 src/admin 引用)。
//
// ⚠️ echarts.use([...]) 必须写在模块顶层,绝不能写在 onMounted 里 —— 那晚于 echarts.init,
// 会报 "Component series.line not exists. Load it first."。这是最容易犯的错,
// 因为"初始化前先注册"的顺序直觉上很像该放在挂载钩子里。
import * as echarts from 'echarts/core';
import { LineChart, BarChart, PieChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart, BarChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent,
  CanvasRenderer,
]);

export default echarts;
