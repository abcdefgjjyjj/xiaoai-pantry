# 相近的 GitHub 项目与复用选择

核对日期为 2026-11-01。下面按功能重合度列出项目，依据为仓库及作者文档。没有在这台音箱上安装这些替代项目。整套需求包括全屋多位置库存、不同批次的数量和日期、原厂小爱收中文自然语句，以及厨房持续有人时择机播报。在这次检查的仓库中，没有找到文档明确支持这四项开箱即用的项目。

| 项目 | 已有能力 | 对本项目最有价值的地方 | 接入时还要做什么 |
| --- | --- | --- | --- |
| [Grocy](https://github.com/grocy/grocy) | 自托管家庭库存，管理商品、位置、批次、数量单位、条码、消耗和到期。提供 REST API。 | 要做扫码、商品主档或购物清单时，可直接评估其库存后端。 | 小爱的发言筛选、多轮追问、厨房人在信号和每日一次播报，仍需在设备侧或适配层接好。[库存教程](https://github.com/grocy/grocy-docs/blob/master/tutorials/food.md) · [API 规范](https://github.com/grocy/grocy/blob/master/grocy.openapi.json) |
| [鲜知贴 fridge_tracker](https://github.com/xueyouluo/fridge_tracker) | 中文 H5，可自托管到 NAS。按数量、存放地点、分类和有效期管理食品、药品、日用品，支持墨水屏和可选 MCP。 | 与“家里任何地方的东西都能记”最接近。可参考手机录入、到期视图或只读展示。 | 作者展示的硬件交互是墨水屏。要复用到这户家庭，需另接原厂 OH2P 的语音输入和人在提醒。[作者说明](https://github.com/xueyouluo/fridge_tracker/blob/main/README.md) |
| [Grocy Voice Agent](https://github.com/jameshansen/Grocy-ElevenLabs-ElevenAgent-UI) | 厨房平板上的语音界面通过 ElevenLabs Agent 操作 Grocy，可语音加库存和登记消耗。 | 证明“语音前端 + 现成库存后端”已有实现，可参考语音意图如何转成库存操作。 | 作者使用平板按键开始说话。沿用家里的小爱唤醒方式，需要替换输入和回执通路。[作者演示说明](https://jameshansen.ai/projects/grocy-voice-agent) |
| [XiaoMusic](https://github.com/hanxi/xiaomusic) / [xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge) | 前者提供小爱音箱服务，后者把小爱语音桥接到 Home Assistant。 | 当前部署已复用 XiaoMusic 的设备清单和登录会话。桥接项目也记录了轮询对话时原生小爱可能先回应的现象。 | 两者均需另接库存业务。当前家里没有 HA。XiaoMusic 上游仓库已归档，替换时须复测设备标识、对话和 TTS。[XiaoMusic 仓库状态](https://github.com/hanxi/xiaomusic) · [桥接项目说明](https://github.com/chenshuhe/xiaoai-ha-bridge) |

## 如果将来接 Grocy

先用测试实例建立一个商品、一种单位和两个位置，然后把一批记到橱柜，另一批记到冰箱，设置不同到期日。通过目标版本的 API 读回两笔库存，再测试指定批次消耗。这样能提前发现哪些 Grocy 操作按商品选批，哪些能精确选中用户说的那一批。

| 当前数据或行为 | 迁移时要确定的映射 |
| --- | --- |
| 自由输入的 name、unit、location | 与 Grocy 商品、数量单位、位置的稳定 ID 对照。遇到两个同名商品时需要消歧。 |
| batches.id 与每批 due | 保存本地与远端批次标识的对应关系，验证移动和改期后仍能找到同一批。 |
| check 查看日、stock 无日期 | 保持“提醒查看”和“到期”的不同意思。先验证目标版本的数据模型，必要时保留一张本地提醒表。 |
| turns 查重、changes 撤销 | 本地超时可能发生在远端已成功写入之后。先设计查询与对账，再允许自动重试。撤销也要能定位远端对应操作。 |
| 厨房人在与 notices | 可继续由群晖执行。在切换日只启用一套主动播报，避免同一批商品被两套系统各报一次。 |

对目前已跑通的小爱流程，继续使用现有 SQLite 的改动最小。若下一阶段明确需要 Grocy 的扫码、主档和购物清单，优先做以上小范围验证，再决定是否迁移。此次调研未更换线上库存后端。
