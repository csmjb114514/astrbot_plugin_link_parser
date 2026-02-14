# astrbot_plugin_link_parser
基于AstrBot平台的Python插件，可以进行绕过忍者链接进行解析，需要购买Lat bypass服务

链接解析插件文档

📝 插件信息

· 插件名称：link_parser
· 版本：1.1.0
· 作者：AstrBot开发者
· 描述：链接解析插件，支持解卡功能和任务排队系统

📦 安装方法

方法一：通过AstrBot WebUI安装（推荐）

1. 打开AstrBot管理面板
2. 进入"插件管理"
3. 搜索"link_parser"
4. 点击"安装"

方法二：手动安装

1. 进入AstrBot的 data/plugins 目录
2. 克隆插件仓库：
   ```bash
   git clone https://github.com/csmjb114514/astrbot_plugin_link_parser
   ```
3. 重启AstrBot或重载插件

⚙️ 配置说明

插件支持通过WebUI进行配置，配置文件为 _conf_schema.json。以下是所有可配置项：

配置项 类型 默认值 说明
api_key string User_LAT1234567890 API密钥
api_url string https://api.bypass.ceo/bypass/qq-bot/delta/q-bot API请求地址
debug_mode bool false 调试模式开关
max_retries int 3 最大重试次数
max_total_attempts int 10 最大总尝试次数
task_interval int 30 任务间隔（秒）
task_timeout int 1800 任务超时时间（秒）
max_queue_size int 10 最大队列长度
allowed_domains list 见下方 允许的域名列表

默认允许的域名

```json
[
    "https://auth.platoboost.com",
    "https://auth.platorelay.com",
    "https://auth.platoboost.net",
    "https://auth.platoboost.click",
    "https://auth.platoboost.app",
    "https://auth.platoboost.me",
    "https://deltaios-executor.com"
]
```

📋 依赖库

插件只需要一个依赖库：

requirements.txt:

```txt
aiohttp>=3.8.0
```

🎮 使用命令

1. 解卡命令

命令：/解卡 <链接>

功能：将链接加入解析队列，自动进行解析

示例：

```
/解卡 https://auth.platoboost.com/xxx/xxx
```

响应示例：

```
✅ 链接已加入解析队列
📊 当前排队位置：第1位
⏱️ 预计等待时间：约30秒
🔄 任务将自动重试3次，总尝试10次
⏰ 任务超时时间：30分钟
```

2. 查看队列状态

命令：/队列状态

功能：查看当前任务队列状态

响应示例：

```
📊 当前队列状态
等待任务数：2
正在处理：是
任务间隔：30秒
最大重试：3次
总尝试次数：10次
任务超时：30分钟
当前处理：https://auth.platoboost.com/xxx...
已尝试：2次
```

3. 取消任务

命令：/取消任务

功能：取消用户自己的待处理任务

响应示例：

```
✅ 已取消1个待处理任务
```

🔄 工作流程

1. 用户发送链接 → 验证域名合法性
2. 加入队列 → 显示排队位置和预计等待时间
3. 后台处理 → 按顺序处理每个任务
4. 自动重试 → 失败后自动重试（可配置次数）
5. 结果通知 → 解析成功或失败都会@用户并引用原消息

⏱️ 等待时间策略

根据不同的错误类型，插件会自动调整等待时间：

错误类型 等待时间
API服务暂时不可用 60秒
频繁请求限制 120秒
请求超时 45秒
网络连接失败 30秒
其他错误 任务间隔（默认30秒）

📊 响应消息类型

成功解析

```
✅ 解卡成功！
🔑 卡密：xxxx-xxxx-xxxx
⏱️ 耗时：2.5秒
🎮 祝你游玩愉快
```

失败重试

```
🔄 第2次尝试失败
❌ 原因：API服务暂时不可用
⏱️ 60秒后将进行第3次尝试
📊 已尝试2/10次
```

最终失败

```
❌ 经过10次尝试，解析失败
🔗 链接：https://auth.platoboost.com/xxx
📊 最后一次错误：链接已过期
📝 最近错误：
尝试8: 链接已过期
尝试9: 链接已过期
尝试10: 链接已过期

💡 建议：
1. 确认链接是否有效
2. 重新获取新链接再试
3. 如果问题持续，请联系管理员
```

超时失败

```
⏰ 任务已超时（超过30分钟）
❌ 链接解析失败
🔗 链接：https://auth.platoboost.com/xxx
📊 已尝试次数：8
💡 建议：请重新获取新链接后再试
```

⚠️ 注意事项

1. 域名限制：只支持配置文件中指定的域名
2. 队列限制：每个用户最多同时有2个任务在排队
3. 超时机制：任务超过30分钟（可配置）会自动放弃
4. 消息格式：最终结果会@用户并引用原消息
5. 重试机制：失败后会根据错误类型智能调整等待时间

🐛 常见问题

Q: 插件安装失败

A: 请检查是否安装了依赖：pip install aiohttp

Q: 提示"不是忍者链接"

A: 请确认链接域名是否在允许列表中，或联系管理员添加新域名

Q: 一直显示排队位置为1

A: 已修复此问题，请更新到最新版本

Q: 发送消息失败

A: 请检查AstrBot版本是否兼容，最低要求v4.14.0

🔧 开发调试

开启调试模式（在配置中设置 debug_mode: true）后，会在日志中输出详细信息：

· 用户输入URL
· API请求URL
· API响应状态码
· API响应内容
· 解析过程日志
· 错误详细信息

📝 更新日志

v1.1.0

· ✨ 添加任务排队系统
· ✨ 支持自动重试机制
· ✨ 添加@用户和引用消息功能
· ✨ 修复排队位置显示bug
· ✨ 添加智能等待时间策略
· ✨ 新增队列状态查询命令
· ✨ 新增取消任务命令

v1.0.0

· 🎉 初始版本发布
· ✨ 基础解卡功能
· ✨ 域名验证

📧 联系方式

如有问题或建议，请联系：

· 作者：传送门
· 仓库：[GitHub链接]

---

文档最后更新：2024-01-XX
