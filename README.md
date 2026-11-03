# 中央气象台预警播报

把[中央气象台](https://www.nmc.cn/)发布的**全国气象灾害预警信号**，以及指定城市的**天气预报**，
自动推送到 AstrBot 的 QQ 群、QQ 私聊等会话。

数据全部来自中国大陆官方源，界面与推送文案均为简体中文。

## 功能

- **预警推送**：覆盖中央气象台发布的全部 16 类气象灾害预警（暴雨、大风、大雾、雷电、暴雪、道路结冰、寒潮低温、高温、台风、沙尘、冰雹、干旱、霾、地质灾害、山洪渍涝、其他），支持按城市、类型、颜色等级、动作（发布 / 变更 / 解除）筛选。
- **天气播报**：为每个订阅城市定时推送实况 + 未来三天预报（含体感温度、湿度、风力、空气质量、当前生效预警）。
- **群聊与私聊**：同一个插件同时支持群聊和私聊会话，指令在哪个会话里发送，就登记哪个会话。
- **管理页面**：提供省市联动的可视化配置页，可直接预览推送效果、测试数据源连通性、发送测试消息。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/cma_register 城市1,城市2` | 把当前会话登记为播报目标（管理员） |
| `/cma_unregister` | 取消当前会话的登记（管理员） |
| `/cma_weather [城市]` | 查询城市天气，省略城市时使用当前会话订阅的城市 |
| `/cma_status` | 查看当前会话的订阅状态与最近一次检查结果 |
| `/cma_test` | 立即向当前会话发送一条测试消息（管理员） |
| `/cma_help` | 显示指令帮助 |

预警类型、颜色等级、天气播报时间等细项在插件管理页调整。

## 数据来源

| 用途 | 接口 |
| --- | --- |
| 全国预警列表 | `https://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=500` |
| 预警详情 | `https://www.nmc.cn/publish/alarm/<alertid>.html` |
| 城市天气 | `https://www.nmc.cn/rest/weather?stationid=<code>` |

均为中央气象台官网公开接口，无需申请密钥。

## 行政区划覆盖

内置 **31 个省级行政区、380 个地级市 / 直辖市 / 省直辖县级市**的对照表（`regions.py`），
每个城市都绑定一个中央气象台的观测站编号。城市名支持省略「市」后缀，
例如 `/cma_register 广州` 与 `/cma_register 广州市` 等价。

## 配置项

### 插件配置（AstrBot 配置面板）

插件在 AstrBot 的「配置」中提供四个网络参数，修改后需重载插件生效：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `timeout` | `20` | 单次请求超时（秒），范围 5 ~ 120 |
| `retries` | `2` | 请求失败重试次数，范围 0 ~ 5 |
| `proxy` | 空 | HTTP 代理，例如 `http://127.0.0.1:7890` |
| `user_agent` | 空 | 自定义 User-Agent，留空使用内置 Chrome UA |

### 运行设置（管理页面 / KV 存储）

订阅与播报参数通过管理页面写入 AstrBot 的 KV 存储，主要字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `poll_interval_seconds` | `180` | 预警轮询间隔，60 ~ 1800 秒 |
| `content_limit` | `600` | 单条预警正文的字符上限 |
| `subscriptions[].cities` | `[]` | 订阅城市列表 |
| `subscriptions[].alert_types` | 全部 | 订阅的预警类型 |
| `subscriptions[].levels` | 全部 | 订阅的颜色等级 |
| `subscriptions[].actions` | 全部 | 订阅的预警动作 |
| `subscriptions[].forecast_enabled` | `true` | 是否开启定时天气播报 |
| `subscriptions[].forecast_times` | `07:30`、`19:00` | 天气播报时间，最多 4 个 |
| `subscriptions[].include_detail` | `true` | 是否抓取预警详情页正文 |

## 平台说明

- **aiocqhttp / qq_official / qq_official_webhook** 均可主动推送。
- QQ 官方机器人的群聊主动推送要求该群先与机器人产生过对话（发送任意指令即可满足），
  插件会在每次发送前自动补登记会话场景，因此重启后无需重新触发。
- 官方机器人受腾讯侧主动消息频率策略约束，建议把 `poll_interval_seconds` 保持在 300 秒以上。

## 数据持久化

所有状态（去重指纹、投递记录、天气播报槽位、订阅配置）只写入 AstrBot 的 KV 存储，
不落任何本地文件，也不写入用户主目录或工作目录。

## 许可

本项目基于 [Vlxnz/astrbot_plugin_cwa_alert](https://github.com/Vlxnz/astrbot_plugin_cwa_alert)（AGPL-3.0-or-later）改写，
数据源由台湾中央气象署替换为中央气象台，界面与文案全面简体化。遵循 AGPL-3.0-or-later 发布。
