# Fumiha Lighting 文羽照明

恒照度补偿（原 lux_balance）与定时夜灯组（原 auto_night_light）的合并集成。一个 domain、两种条目类型、两个文羽面板页面；页面读写走集成自有 REST，实体（light/number/button/sensor）只作为 HA 原生生态（自动化/仪表盘）的适配层。

## 从旧集成迁移（一次性）

1. HACS 添加并安装 `SagisawaTsubasa/FumihaLighting`，重启 HA
2. **删除**旧条目：设置 → 设备与服务 → lux_balance 条目、auto_night_light 条目 → 删除
3. **删除**旧集成本身（HACS 里移除 lux_balance / auto_night_light，或先留着不冲突）
4. 添加「Fumiha Lighting 文羽照明」→ 选择类型：
   - **恒照度区域**：填区域名、灯、照度传感器（同旧 lux_balance 配置）
   - **夜灯组**：按向导填时间锚点/灯/参数（同旧 auto_night_light 配置）
5. 卫生间区**重新校准一次**（曲线不迁移，约 15~40 分钟（传感器上报不畅时可能接近 1 小时））

已知破坏面：服务改名 `fumiha_lighting.calibrate` / `fumiha_lighting.trigger_now`；实体 platform 变更导致 entity_id 重建（引用旧实体 id 的自动化需改）。

## 架构

- 页面（文羽面板壳内 `luxbalance.main` / `anl.main` 两个页签；壳缺席时各自回落独立侧边栏面板）
- REST：`GET /api/fumiha_lighting/lux/overview`、`GET /api/fumiha_lighting/anl/snapshot`；写：`POST /api/fumiha_lighting/lux/target|calibrate|calibrate/cancel`、`POST /api/fumiha_lighting/anl/trigger`、`GET|POST /api/fumiha_lighting/anl/config`（夜灯设置读写，V1.4.0）
- 服务（自动化面）：`fumiha_lighting.calibrate`（目标未命中/互斥跳过时抛错）、`fumiha_lighting.trigger_now`
- `static/kit.js`、`static/tokens.css` 为 FumihaHub vendored 副本（锁步更新）

## 夜灯组配置（V1.4.0 起）

**原生向导只保留 3 步**：选择灯具 → 时间锚点（开始/结束方式 + 固定时间/偏移 + 太阳实体 + 过渡时长，一页）→ 全局参数（夜间/日间亮度色温、容差、延迟、行为开关）。

**逐灯覆盖与额外时段在「夜灯助手」页面编辑**（点条目卡「⚙ 设置」或灯卡「逐灯设置」）：

- 全局设置弹窗：时间锚点、全部参数、额外时段增删改（0–5 个），保存经 `POST /api/fumiha_lighting/anl/config` 写入条目 options，自动重载生效；
- 逐灯设置弹窗：「跟随全局」开关 + 夜间/日间/各额外时段参数覆盖，取消覆盖即恢复全局；
- 校验与向导共用同一份字段规格（`anl/config_schema.py`），越界/未知灯/超时段数都会被 400 拒绝。

HA 原生「配置」（options 流）与向导同字段同存储，同样不含逐灯覆盖/额外时段。语义注记：**重新配置**（reconfigure）保存时会把条目数据整体收敛进 `data` 并清空 `options`，GUI 写入的额外时段/逐灯覆盖会原样携带，不会丢失。

### 日间恒照度模式（V1.5.0 起）

日间段新增独立开关「日间恒照度模式」（`day_ci_enabled` + 虚拟灯 `day_ci_virtual_light`）：开启后日间段夜灯组**不再直控真实灯的亮度/色温**，改为驱动恒照度（lux）侧的虚拟恒照度灯实体——开虚拟灯 = lux 闭环接管（联动 `light.turn_on` 绝不带亮度，带亮度会落 lux passthrough 破坏闭环）。接管时机为**事件驱动**：

- 白天开真实灯（开灯监听）或触发落在日间 → 开虚拟灯交恒照度闭环，真实灯零下发、无 settle 延迟；
- 夜间开始锚点触发（或触发落在夜间/额外时段）→ 先关虚拟灯归还（lux off = 纯放手，对真实灯零下发），再按夜灯参数直控；
- CI 下日间参数与逐灯日间覆盖**不生效但保留存储**；过渡带含日间侧（from/target 为日间）的不生成步进，夜间↔额外时段过渡不受影响；
- 虚拟灯服务失败 → WARNING（每夜灯组节流一次）+ 回落日间参数直控，错误必响但保可用。

设置弹窗的显隐为条件式：勾选「日间恒照度模式」才显示虚拟灯选择，日间亮度/色温输入随之隐藏（向导仍为平铺展示，条件显隐由夜灯助手页面 GUI 承担）。

## 开发

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests dev
pytest tests/ -q
python dev/serve.py   # 离线 harness（http://127.0.0.1:8773）
python dev/test_panel.py  # Playwright 冒烟
```

## 版本历史

- **V1.5.0（2026-10-19）**：夜灯日间恒照度模式 + 设置弹窗条件显隐——①夜灯组新增 `day_ci_enabled`/`day_ci_virtual_light`：日间段不直控真实灯，改为驱动恒照度虚拟灯（事件驱动：白天开灯/触发落日间 → 开虚拟灯交 lux 闭环；夜间开始锚点/触发落夜间 → 关虚拟灯归还后按夜灯参数直控；联动开灯绝不带亮度，防落 lux passthrough）；②CI 下日间参数与逐灯日间覆盖保留存储不生效，过渡带含日间侧的不生成步进；③虚拟灯服务失败 WARNING 节流 + 回落日间参数直控；④`POST anl/config` 校验虚拟灯存在性与 light domain；⑤设置弹窗条件显隐（勾 CI 才显虚拟灯选择、隐藏日间参数行，前端半批）；⑥向导参数步平铺补两字段（en/zh 翻译同步）；⑦首审修复批——额外时段起点定时器（无过渡带 `transition=0` 的时段亦按时生效，修复历史缺口）、CI 下跨入日间窗口的过渡步进跳过（transitions_active 按生效带计）、CI 停用/改指/删条目自动归还旧虚拟灯（web/config_flow/删除钩子三挂点）、虚拟灯身份校验改实体注册表（本集成 platform+light 域）、CI 开而虚拟灯为空在向导与 REST 双侧 400/表单拦截、灯组拒绝本集成虚拟灯；⑧R2 复验修复——CI 生效判据含日间模式（day_enabled 关时 CI 完全不生效且快照不谎报）、extra 起点与过渡带互让（带内计划步让位于 extra 时段，transition=0 重叠告警）、跨条目归还检查、虚拟灯可用性预检、重复 start 校验、并发与句柄治理
- **V1.4.0（2026-10-19）**：夜灯配置体验重构——①原生向导 8+ 步瘦身为 3 步（灯具→时间锚点→全局参数），额外时段与逐灯覆盖移出向导；②夜灯助手页面新增设置弹窗（全局设置 + 逐灯覆盖，额外时段增删改），走新 REST `GET|POST /api/fumiha_lighting/anl/config` 写入条目 options 自动重载生效，与向导共用字段校验规格（`anl/config_schema.py`）；③修复原生 options 流两处真机 bug（缺 `async_step_init` 入口打开即 UnknownStep；`LuxZoneOptionsFlow` 向无 `__init__` 的基类传参 TypeError——均被测试桩掩盖，V1.0.0 合并时引入）；④reconfigure 保存收敛 data 时原样携带 GUI 管理键（extras/overrides 防丢）；⑤conftest 桩修正 + 新增测试（配置端点/向导链/options 入口回归钉）+ Playwright 弹窗用例
- **V1.3.0（开关语义，2026-10-15 落码，随 V1.4.0 出版）**：映射实体=恒照度开关（WP1）——删外部开灯自愈接管（不再与夜灯模组抢灯）、删重启自愈、`async_turn_off_requested` 纯放手不下发关灯、校准 pending 收尾放手到 IDLE
- **V1.2.3（2026-10-15）**：版本线勘误——V1.2.2 发布时 manifest 未 bump（盒上面板缓存戳仍显示 1.2.1），本版仅版本号修正；功能与 V1.2.2 一致
- **V1.2.1（2026-10-14）**：校准可中止——页面校准进度下新增「中止校准」按钮（danger 确认），REST `POST /api/fumiha_lighting/lux/calibrate/cancel`；取消恢复灯原状态、不记错误；覆盖全部取消窗口（同刻未起步/已起步未采集/采集中/曲线已采纳）的闭环状态恢复——未触碰窗口取消后区语义回到校准前（ACTIVE 灯亮有曲线 / IDLE），采集中取消按捕获值恢复，挂起的其他区对称唤醒（含 resume 评估）；窗口①并发关灯意图被兑现
- **V1.2.0（2026-10-14）**：四页可操作性迭代——①照度数据可信窗（30 分钟）：心跳型 BLE 传感器长时间不上报时闭环暂停、页面明示「数据不可信」，开灯首帧同判据；②校准启动前预检传感器新鲜度并在确认框警告；③校准中止原因上页面（此前只进日志，观感与完成一致）；④夜灯「立即触发」加确认；⑤夜灯灯卡显示友好名；⑥目标照度输入保留草稿、支持回车；⑦时长文案校准至最坏情况
- **V1.1.1（2026-10-14）**：修复影子期以来恒照度半边在部署实例上的 `setup_error`——HA 按 domain 根解析平台模块，而 light/number/button/sensor 平台留在 `lux/` 子包内；补根级转发 shim，并给 CI 加真机 import 兜底 job（含四个平台模块）
- **V1.1.0（2026-10-14）**：影子预览期结束——页面标识修订回正式（页签 id `luxbalance.main`/`anl.main`，标题去「·影子」后缀，元素名还原）；与旧 lux_balance / auto_night_light 的退役切换同步完成
- **V1.0.0**：lux_balance + auto_night_light 合并发版（双类型条目、自有 REST、双页面）
