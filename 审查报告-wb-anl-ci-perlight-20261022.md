# 审查汇总报告：FumihaLighting V1.6.0 日间恒照度单灯化（anl-ci-perlight 批）

> 循环：首审（job-bcb4a7bf5b）→ R1 修复 → R2 复验（job-e8215c57f5）→ R3 修复 → R3 复验（job-7fff12e0cb，零 P0–P2）→ ANL-F-104 用户终裁（2026-10-23）→ R4 修复 → R4 复验（job-5d43866c5d，零 P0–P2，**收口**）。
> 台账：`D:\project\台账\HA\审查台账.md` FumihaLighting 节 ANL-F-102~115。批次产物：`D:\project\temp\wb-review\anl-ci-perlight-20261022{,-r2,-r3,-r4b}\`（R1 报告在 anl-v15-firstrev-20261021 之后的施工批次目录）。
> 本文为收口补录（2026-10-23，主对话），施工与各轮详情见上列台账与批次原件。

## 发现总账（14 条编号 + 复核终态）

| ID | 级别 | 一句话 | 终态 |
|---|---|---|---|
| ANL-F-102 | P1 | GET 未镜像读入迁移+POST 死键 pop → 存量保存一次静默清零 CI 并归还虚拟灯 | fixed（R2 复核通过） |
| ANL-F-103 | P1 | reconfigure 归还判据 new 不含 CI 键（向导已剥）→ 保留 CI 的 reconfigure 也归还虚拟灯 | fixed（R2 复核通过） |
| ANL-F-104 | P2 | 混合组任一灯勾 CI 即隐藏全局日间参数行，非 CI 灯失去编辑入口 | **用户终裁第三案**：全局日间参数行与 CI 完全解耦恒显（只随 day_enabled），CI 开关归逐灯——fixed（R4 复核通过） |
| ANL-F-105 | P3 | trigger_now 描述仍是组级 CI 措辞，与逐灯语义相反 | fixed（R2 复核通过） |
| ANL-F-106 | P2 | _carry_gui_keys 携带不看 day_enabled，写入 web 自身 400 拒绝的组合 | fixed（R3 复核通过；取审查员 A 案门控） |
| ANL-F-107 | P3 | 迁移回填条件缺存量虚拟灯前提，可落非法组合 | fixed（R3 复核通过） |
| ANL-F-108 | P3 | 改签名残留破损日志文案，唯一诊断入口读不通 | fixed（R3 复核 3/4，残余=F-111） |
| ANL-F-109 | P3 | 跨四层复用的迁移单一来源仍私有名（违 ANL-F-003 家规） | fixed（R3 复核通过） |
| ANL-F-110 | P3 | manager.stop 登记晚于 start()，start 抛错留幽灵运行时 | open（随批清偿） |
| ANL-F-111 | P3 | 不可用节流分支「开启失败」与异常分支「开启仍失败」不一致 | open（随批清偿） |
| ANL-F-112 | P3 | WARNING 声称回落直控但 turn_on_listen=False 路径零直控 | open（随批清偿） |
| ANL-F-113 | P3 | F-104 终裁后残留反向陈旧注释（与实现矛盾） | open（随批清偿） |
| ANL-F-114 | P3 | 保存载荷断言未覆盖日间两键，「显隐↔全量提交」无回归钉 | open（随批清偿） |
| ANL-F-115 | P3 | 全 CI 组日间参数行恒显但零生效对象，无提示（静默无操作面） | open（随批清偿） |

## 关键决策记录

- **CI 配置形态（用户拍板 2026-10-22）**：纯逐灯——`day_ci_lights` 接管清单键 + 全局 `day_ci_enabled` 废除 + 虚拟灯保持组级一盏；存量读入迁移零回写。
- **F-104 终裁（用户拍板 2026-10-23）**：全局日间参数（日间亮度/色温）与有没有 CI 灯无关、恒显；亮度恒定是单灯逐个设置的特殊单独开关。R4 复验实证该判据与 `params_for(MODE_DAY)` 消费端一致，无新谎报面。
- R3 的 F-106 取审查员 A 案（web 跨字段校验门控：仅 patch 触碰判据输入或解析组合≠存量组合才执行），B 案（flow 侧门控）有数据丢失风险不取。

## 量具终态

- pytest **287 绿**（V1.6.0 净增：281 基线+6 钉；施工批自 211 起步）
- Playwright **all green**（F-104 钉翻转：vis5 断言恒显+computed flex）
- ruff：唯一豁免 `lux/runtime.py` F821（SIM105 已于本批修除，I001×4 施工批遗留已清零）
- 翻译三件套 delta=0；manifest **1.6.0**；README 单灯化重写

## 部署与遗留

- 已 push main（fc4e914→263b1ea）+ GitHub Release **V1.6.0**（HACS 可取）。
- 盒上更新尝试（WS refresh → update.install）：**2026-10-23 盒上 HA 不可达（REST/WS 超时），未执行**——网络恢复后在 HA 内手动刷新 HACS 即见 V1.6.0；验证清单：①接管清单逐灯勾选保存生效；②混合组全局日间参数行可见可编辑；③存量条目打开面板配置不丢 CI（迁移面）；④逐灯弹窗接管开关。
- 遗留：F-110~115 六条 P3 登记台账随批清偿（下一批同面顺带）；联动组批（stash@{0}）仍受阻待续。
