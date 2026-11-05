# AvatarWeave 融资商业计划书 V2

V2 以“未来的个人 AI 入口”为长期定位，以中国成年用户的卡通陪伴体验为切入点。项目处于全职独立开发、尚未商业化阶段；当前工程能力是执行依据，未来服务连接与跨设备身份是发展规划。

## 交付文件

- [可编辑 PowerPoint](AvatarWeave-Investor-Deck-V2.pptx)：25 页，正文文字、图形、界面和预算条形均可编辑；22 页主讲，3 页附录。包含每页演讲者备注。
- [路演 PDF](AvatarWeave-Investor-Deck-V2.pdf)：25 页，内嵌中文字体，页脚直接置于页面中。
- [路演讲稿](speaker-notes.md)：逐页讲述重点与来源。
- [事实与来源台账](sources.md)：数据时期、出处、口径和边界。
- [融资用途表](funding-budget.csv)：人民币万元，全部是融资建议预算，非历史支出。
- [整套缩略图](previews/contact-sheet.png)。

## 内容与数据边界

外部数据采用 CNNIC、Sensor Tower、Stanford HAI、USV 和官方产品资料；同类融资用途补充 Portola 创始人文章及 GeekWire 报道。没有使用 V1 的虚拟用户、收入、CAC、留存或毛利数字。用户场景和界面均标注为设计示意，不是客户证言或生产截图。

创始人履历按本人提供内容呈现：路伟、硕士、全职独立开发、8 年算法研发经验、好未来与爱学习经历，涉及时间序列预测、量化交易、情感陪伴数字人。没有添加学校、职位、业务成绩或雇主背书。

300 万元 / 15 个月是应创始人要求提出的融资预算建议，不是已融资或历史支出。核心团队预算按平均 4 个全职等效岗位（包含创始人）× 每岗位月全口径预算 2.5 万元 × 15 个月 = 150 万元测算；岗位成本是预算约束，不是调研工资或供应商报价。其余用途 120 万元、准备金 30 万元，总计 300 万元。执行前按实际招聘、报价和公司税费条件复核，不承诺估值、稀释比例或收入。

公司法律主体、股权结构、历史资金投入与联系方式尚未提供，因此没有编造；正式发送时请补充联系方式，进入尽调前准备主体与权利材料。豆包链接仍未获得可读取原文，未引用。此版本可用于早期项目介绍与融资沟通，不包含尚不存在的商业业绩。

## 视觉与文件验证

封面愿景图和界面角色图使用内置 image_gen 生成，提示词在 assets 内。它们是概念视觉，不代表完成 Live2D 动画或外部服务接入。

旧版 PDF 页脚使用 Chromium 独立模板，无法继承正文 CJK 字体；已替换为兼容的英文页脚，重新生成原文件。V2 改为页内页脚。已检查 PDF 实际渲染、文本、页数及 HTML 对应布局，检查 PPTX XML 结构、文字可编辑性与演讲者备注。尚未在桌面 PowerPoint 实机逐页播放；PPT 字体设为 Microsoft YaHei，跨设备投递建议同时附 PDF。

## 重建

依赖安装在独立工具目录，不修改业务依赖。构建脚本默认读取 /tmp/avatarweave-bp-v2-tools/node_modules，可用 BP_NODE_MODULES 指定；所需版本见 package.json。HTML/PDF 排版读取 /tmp/avatarweave-NotoSansCJKsc-Regular.otf。

从仓库根运行：

```bash
node docs/business-plan/v2/build-deck.cjs
node docs/business-plan/render-pdf.mjs --v2
node docs/business-plan/v2/inspect-pdf.mjs
```

业务运行代码、现有角色及服务未更改。
