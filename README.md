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
- REST：`GET /api/fumiha_lighting/lux/overview`、`GET /api/fumiha_lighting/anl/snapshot`；写：`POST /api/fumiha_lighting/lux/target|calibrate`、`POST /api/fumiha_lighting/anl/trigger`
- 服务（自动化面）：`fumiha_lighting.calibrate`（目标未命中/互斥跳过时抛错）、`fumiha_lighting.trigger_now`
- `static/kit.js`、`static/tokens.css` 为 FumihaHub vendored 副本（锁步更新）

## 开发

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests dev
pytest tests/ -q
python dev/serve.py   # 离线 harness（http://127.0.0.1:8772）
python dev/test_panel.py  # Playwright 冒烟
```

## 版本历史

- **V1.2.1（2026-10-14）**：校准可中止——页面校准进度下新增「中止校准」按钮（danger 确认），REST `POST /api/fumiha_lighting/lux/calibrate/cancel`；取消恢复灯原状态、不记错误；修同刻取消（task 未起步）时 mode 卡 calibrating 的边界
- **V1.2.0（2026-10-14）**：四页可操作性迭代——①照度数据可信窗（30 分钟）：心跳型 BLE 传感器长时间不上报时闭环暂停、页面明示「数据不可信」，开灯首帧同判据；②校准启动前预检传感器新鲜度并在确认框警告；③校准中止原因上页面（此前只进日志，观感与完成一致）；④夜灯「立即触发」加确认；⑤夜灯灯卡显示友好名；⑥目标照度输入保留草稿、支持回车；⑦时长文案校准至最坏情况
- **V1.1.1（2026-10-14）**：修复影子期以来恒照度半边在部署实例上的 `setup_error`——HA 按 domain 根解析平台模块，而 light/number/button/sensor 平台留在 `lux/` 子包内；补根级转发 shim，并给 CI 加真机 import 兜底 job（含四个平台模块）
- **V1.1.0（2026-10-14）**：影子预览期结束——页面标识修订回正式（页签 id `luxbalance.main`/`anl.main`，标题去「·影子」后缀，元素名还原）；与旧 lux_balance / auto_night_light 的退役切换同步完成
- **V1.0.0**：lux_balance + auto_night_light 合并发版（双类型条目、自有 REST、双页面）
