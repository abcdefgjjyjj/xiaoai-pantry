# 家庭库存 HTTP 接口

适用于 2026-11-01 的自托管版本。接口由 [app.py](../app.py) 的 `Handler` 实现，网页 [page.html](../page.html) 是同一套接口的客户端。默认本机基址为 `http://127.0.0.1:8768`。下文的商品、访问码占位符和批次号都是示例。

## 连接与认证

| 方法和路径 | 认证 | 用途 |
| --- | --- | --- |
| GET / | 公开 | 家庭清单 HTML 页面 |
| GET /health | 公开 | Web 进程响应时返回 `{"ok":true}` |
| POST /api/login | 登录请求 | 用家庭访问码换浏览器 Cookie |
| GET /api/state | 需要 | 查询全部在库批次、设置和设备状态 |
| GET /api/export.csv | 需要 | 下载全部在库批次，UTF-8 BOM、CSV 表头和按批次的数据，不受页面筛选影响。 |
| POST /api/add | 需要 | 按字段新建一个批次 |
| POST /api/catalog | 需要 | 按标准商品名新建或覆盖常买商品资料 |
| POST /api/command | 需要 | 提交中文库存指令或接上一次追问 |
| POST /api/batch | 需要 | 对一个批次扣减、改日期或改位置 |
| POST /api/settings | 需要 | 保存整套提醒设置 |

所有 POST 都接受 UTF-8 JSON 对象，要求 `Content-Type: application/json`，正文长度为 1–8192 字节。浏览器带有 Origin 时，其主机和端口须与请求 Host 一致。家庭共用一个访问码，目前没有只读账户。脚本可直接用 `Authorization: Bearer <家庭访问码>`，不用先登录。浏览器登录传 `{"token":"<家庭访问码>"}`，成功返回 `{"ok":true}` 并设置 `pantry` Cookie（HttpOnly、SameSite=Strict、一年）。页面支持以访问码登录或打开带片段的家庭登录链接，页面读完片段就从地址栏移除。局域网地址使用 HTTP。

下例在 **PowerShell 7** 执行，仅查询状态。访问码在终端隐藏输入。开发和写入示例请先使用[维护手册中的本地测试实例](HANDOVER.md#本地开发)。

```powershell
$base = 'http://127.0.0.1:8768'
$secret = Read-Host '家庭访问码' -AsSecureString
$state = Invoke-RestMethod "$base/api/state" -Authentication Bearer -Token $secret -AllowUnencryptedAuthentication
$state.rows | Select-Object id,name,qty,unit,location,due,kind,status
```

## 读取库存

`GET /api/state` 的示例响应如下。筛选、搜索和计数由网页在本地完成，接口没有分页或查询参数。先取这里的 `rows[].id`，再对指定批次操作。同名、同日期的商品仍可能是不同批次。

```json
{
  "rows": [{"id":17,"name":"饼干","qty":2,"unit":"包","due":"2026-11-03","kind":"expiry","location":"橱柜","status":"soon"}],
  "today":"2026-11-01",
  "settings":{"enabled":"1","start":"09:00","end":"20:00","lead_days":"2"},
  "devices":{"voice":"云端最近发言 11-01 10:00 · 查询 10:01","presence":"厨房有人 · 上报 11-01 10:00","output":"已受理 10:01"},
  "last_voice":"2026-11-01T10:00:00+08:00",
  "can_undo":true,
  "recent_changes":[{"action":"add","created_at":1793491200,"undone":false,"items":[{"batch":17,"name":"饼干","unit":"包","delta":2,"qty_after":2,"location":"橱柜","due":"2026-11-03"}]}],
  "notice":{"day":"2026-11-01","state":"音箱服务已受理"}
}
```

- `rows` 仅含余量大于零的批次，按日期、批次 ID 排序，未设日期的排最后。空库存返回 `[]`。`location:""` 表示未指定。
- `status` 可为 `expired` 已过期、`today` 今天到期、`soon` 在提前提醒窗口内、`review` 到日待查看、`later` 日期尚早、`undated` 未设日期。按上海当地日期计算。
- `last_voice` 尚无记录时为空字符串。`notice` 尚无主动提醒时为 null。`devices` 是给人看的诊断文字，进程重启后重新采集。`notice.state` 为「准备播报」「音箱服务已受理」或「结果未确认」。
- `can_undo` 表示家庭存在未撤销变更。它不是当前用户独占的撤销凭据。

## 写入与重试

`/api/add`、`/api/command`、`/api/batch`、`/api/catalog` 都必须传 `id`，建议使用 UUID（接收长度 8–160）。同一笔操作超时后，**保留同一个 ID 和原请求体重试**。服务按 ID 返回首次提交的答复。四个接口共用 Web ID 空间，跨端点、换商品或回答下一轮追问都应使用新 ID。服务不比较重复 ID 的请求内容。示例 ID 仅供本地练习。

写入统一返回 `{"reply":"…"}`，没有单独的 `created_id`。HTTP 200 也可能表示系统在追问、未找到商品或数量超出库存。需要确定结果时读 `reply` 并刷新 `/api/state`，不要假设最后一行是自己新增的批次。

### POST /api/add

```json
{"id":"demo-add-0001","name":"饼干","qty":2,"unit":"包","kind":"expiry","due":"2026-11-03","location":"橱柜"}
```

`name` 为 1–60 个字符，也可用已登记的别名，或用字符串 `barcode` 代替 name。`qty` 为 1–999 的 JSON 整数，`unit` 最长 8 字，可省略（取主档单位，否则为「份」）。直接填日期时用 `expiry` 或 `check` 加 `due: "YYYY-MM-DD"`；只记数量用 `"kind":"stock","due":""`。位置最长 40 字，省略则优先采用主档位置。每次写入都是新批次。

按生产日计算可提交：

```json
{"id":"demo-shelf-0001","barcode":"00123456","qty":2,"produced_on":"2026-10-20","shelf_value":30,"shelf_unit":"day"}
```

三项生产信息须一起传，不能同时指定非空 due；kind 仅可为 expiry 或省略。保质期是正整数，上限为 36500 天、1200 月或 100 年；生产日不能晚于上海当天。上例到期日为 11 月 19 日。月/年按日历相加，目标月没有同号日则取月末。批次返回原始三字段，直接填写日期时分别为 `""`、`0`、`""`。手动改期会清除计算依据，撤销改期可恢复。CSV 末尾追加生产日期、保质期数值和单位。

### POST /api/catalog

```json
{"id":"demo-catalog-001","name":"早餐纯牛奶","unit":"盒","location":"橱柜","aliases":["小白奶","早餐奶"],"barcodes":["00123456"]}
```

先登记映射再按条码入库。按标准名保存整份资料：修改时连同要保留的别名、条码全部提交，空数组清除旧映射。各数组最多 32 项。条码是 8、12、13 或 14 位数字字符串，保留前导零；只作家庭内匹配，不验证商品真伪，也不查外部商品库。标准名、别名和条码共用唯一键，冲突返回 400，不覆盖其他商品。更新只影响未来录入。查询 `GET /api/state` 的 `catalog` 数组即可按名称、别名或条码查找。

### 今天为什么没有提醒

状态接口的 `reminder_today` 给出上海当地的 day、time、due_count、in_window、notice_state、headline、reasons、last_window_check。reasons 是当前阻碍的「稳定键 → 显示文案」对象：disabled、paused、no_items、quiet、unconfigured、waiting、sensor_error、unknown、stale、vacant、short_stay。可同时出现多项。已预留或已尝试时优先显示 notice_state；「已受理」是服务回执，不代表现场听到。last_window_check 是今日播报预留前、允许时段内最近的检查（day/time/headline/reasons 文案数组），没有记录则为 null。查询不会触发播报或写入检查历史。

### POST /api/command

```json
{"id":"demo-cmd-0001","channel":"kitchen-tablet","text":"买了一管牙膏，放在卫生间"}
```

可能收到「牙膏哪天到期，还是哪天提醒查看？」。同一会话继续提交：

```json
{"id":"demo-cmd-0002","channel":"kitchen-tablet","text":"只记库存"}
```

`text` 为 1–240 个字符。`channel` 可选，默认 `browser`，服务取前 80 个字符。不同客户端应使用不同且稳定的 channel，避免互相接走追问。网页使用标签页会话标识。追问约 3 分钟有效。查询、分批选择和撤销都走这个接口，例如「橱柜里有什么」「第二批」「撤销」。网页发指令会得到文本答复，只有音箱入口会把答复播出。

### POST /api/batch

以下是独立操作，`batch` 均为从状态接口取得的实际批次 ID：

| op | 请求体示例 | 结果 |
| --- | --- | --- |
| consume | `{"id":"demo-use-0001","batch":17,"op":"consume","value":1}` | 扣 1 个该批次的计量单位。**省略 value 或传 null 会扣完整批**。余量不足会返回文字提示。 |
| date | `{"id":"demo-day-0001","batch":17,"op":"date","value":"2026-11-08"}` | 修改日期。查看日仍是查看日，只记库存的批次会变为到期日。 |
| location | `{"id":"demo-loc-0001","batch":17,"op":"location","value":"书房抽屉"}` | 移动整批，传空字符串可清除位置。 |

另外支持下列操作：

| op | 字段 | 业务规则 |
| --- | --- | --- |
| discard | batch 和正整数 value 必填 | 扣除该数量，将原因记为丢弃。余量不足时不修改。 |
| review_tomorrow | batch 必填 | 仅当查看日为今天或已过时，可延至上海时间明天；不会修改到期日。 |

状态接口的 recent_changes 以最近优先返回最多 20 次变更，每次有 action、Unix 秒数 created_at、undone 和受影响的 items。每项 delta 是数量增减，qty_after 是当时变更后的余量。旧记录显示 legacy，时间为空。

批次不存在、已用完或已撤销时，查看 `reply`。要撤销时，给 `/api/command` 一个新请求 ID 和 `"text":"撤销"`。Web 撤销针对**全家最近一笔**未撤销的库存变更，不能按批次 ID 指定要撤销的历史动作。

### POST /api/settings

```json
{"enabled":"1","start":"09:00","end":"20:00","lead_days":"2"}
```

四项一起传。启用开关用 `"1"` 或 `"0"`，时间为 24 小时制 `HH:MM`，要求开始早于结束，不支持跨午夜。窗口含开始、不含结束。`lead_days` 范围 0–90。设置接口不接收增量补丁，也不需要请求 ID。成功返回 `{"reply":"提醒设置已保存。"}`。

## 失败怎么处理

| HTTP | 含义 | 调用方动作 |
| --- | --- | --- |
| 400 | JSON、请求长度、必填字段或数值不合法 | 检查请求正文与字段约束。 |
| 401 | 无有效访问码或 Cookie | 重新登录，脚本检查 Bearer 凭据来源。 |
| 403 | 浏览器来源与服务地址不一致 | 从清单所在地址提交。 |
| 404 | 路由不存在 | 按上表核对方法与路径。 |
| 500 / 连接中断 | 客户端未得到可靠结果 | 库存操作先查状态，需要重试时复用原 ID。 |

错误响应为 `{"error":"说明"}`。物理播报没有对外 Web API。若接入另一种提醒设备，从代码的 `say()` 边界扩展，保留现有每日预留和失败去重规则。
