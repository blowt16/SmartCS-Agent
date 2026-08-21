# jd-aig CEPSUM 数据集申请邮件模板

**收件人**: lihaoran24@jd.com
**主题**: [CEPSUM/NLPCC 2022] Dataset Application - {你的单位名称}

> 申请流程（据 GitHub README 与申请表）：
> 1. 填写 `docs/jd_aig_application_terms.docx`（姓名、单位、签字、日期；需单位联系人签字 + 身份证明，如个人主页/学术 ID/实验室网站）
> 2. 用**单位邮箱**发送下方邮件，附件附上填好的申请表
> 3. 京东研究院审核后会把数据下载链接发回该邮箱
>
> 注意事项：
> - 申请表条款：数据**仅限研究用途**，禁止商用、禁止转发给任何第三方；离职/离开单位后访问权终止
> - README 要求注明"参与 NLPCC 2022 竞赛"（即使仅作研究用途也建议写上，便于审核通过）
> - 数据包含 3 个类目（箱包 / 家电 / 服饰），可申请完整数据，智能家具信息主要从「Home Appliances（家电）」分片筛选

---

## 邮件正文（中文，按需替换 `{}` 占位符）

尊敬的京东 AI 研究院老师：

您好！我是 {你的姓名}，目前在 {单位名称}（{部门/实验室}）工作，联系方式：{单位邮箱}。

我们正在搭建一个面向电商客服场景的 RAG 检索增强生成系统，需要真实的电商商品数据（商品描述 + 属性知识库）来测试索引构建与检索效果。现有测试语料为虚构数据，无法验证真实场景下的效果，故申请使用贵团队发布的 CEPSUM（NLPCC 2022 多模态商品摘要）数据集。

申请内容：完整数据集（或 Home Appliances 分片），仅用于**非商业研究**用途，数据仅限本团队内部使用，不会对外公开、转发或用于任何商业目的，使用完毕或离开单位后即停止访问。

身份证明：
- 个人主页 / 学术主页：{URL}
- Semantic Scholar / 其他学术账号：{ID}
- 实验室网站：{URL}

已随邮件附上填写并签字完毕的申请表格（Application Terms and Form），请查收。如有任何疑问，可随时通过此邮箱联系。

此致
敬礼

{你的姓名}
{单位名称}
{单位邮箱}
{日期}

---

## 英文版正文（如需）

Dear JD AI Research Team,

My name is {Name}, currently working at {Affiliation} ({Department/Lab}). I am writing to apply for access to the CEPSUM dataset (NLPCC 2022 Multimodal Product Summarization) for non-commercial research purposes.

We are building a RAG (retrieval-augmented generation) system for e-commerce customer service and need real e-commerce product data (product descriptions and attribute knowledge bases) to evaluate our index construction and retrieval pipeline. The dataset will be used solely for internal research and will not be shared, published, or used commercially.

Identity proof: homepage {URL} / Semantic Scholar {ID} / lab website {URL}.

The signed application form is attached. Please let me know if any further information is needed.

Best regards,
{Name}
{Affiliation}
{Email}
{Date}
