# Fumiha Lighting 文羽照明

恒照度补偿（原 lux_balance）与定时夜灯组（原 auto_night_light）的合并集成。一个 domain、两种条目类型、两个文羽面板页面；页面读写走集成自有 REST，实体（light/number/button/sensor）只作为 HA 原生生态（自动化/仪表盘）的适配层。

## 从旧集成迁移（一次性）

1. HACS 添加并安装 `SagisawaTsubasa/FumihaLighting`，重启 HA
2. **删除**旧条目：设置 → 设备与服务 → lux_balance 条目、auto_night_light 条目 → 删除
3. **删除**旧集成本身（HACS 里移除 lux_balance / auto_night_light，或先留着不冲突）
4. 添加「Fumiha Lighting 文羽照明」→ 选择类型：
   - **恒照度区域**：填区域名、灯、照度传感器（同旧 lux_balance 配置）
   - **夜灯组**：按向导填时间锚点/灯/参数（同旧 auto_night_light 配置）
5. 卫生间区**重新校准一次**（曲线不迁移，15~25 分钟）

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
