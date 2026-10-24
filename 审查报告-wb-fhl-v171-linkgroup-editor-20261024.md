# 审查报告-wb-fhl-v171-linkgroup-editor-20261024（施工汇总）

> FumihaLighting V1.7.1 真机反馈批：UI 微调 ×4（用户真机验收 V1.7.0 后反馈）+ 联动组面板内编辑（三键原藏原生 options 流第 2 页「校准与降级」）+ manifest 1.7.1。基线 25ee446（V1.7.0 收口）。计划书：`D:\project\plans\pending\FumihaLighting_V1.7.1_真机反馈UI微调与联动组面板编辑_计划书.md`。

## 1. 施工 commit 链（5 步全落）

| 步骤 | commit | 内容 |
|------|--------|------|
| 1 | 0be0235 | tokens.css 三处（cx-note 12.5px→10px + 删 accent 蓝竖条、backdrop 0.35→0.25 带感知校准注释）+ page-anl.js ciRow 换行（ciHint 从 dlgRow 第三参数位改行下块级 cx-note，`data-anl-ci-hint` 供 L2 钉）+ dev/index.html backdrop 副本同步（FXH-F-009） |
| 2 | a760d3d | web.py `LuxLinkGroupView`（GET/POST `/api/fumiha_lighting/lux/link_group`）+ page-lux.js 联动组 chip 可点 + 编辑弹窗 + tests/test_web.py 新钉×6 + dev 夹具 GET 路由 |
| 3 | 7309c6a | dev/test_panel.py 新钉×4（lux 弹窗预填/保存 POST 三键/chip 即时更新 + anl ciHint 行下块级结构钉）+ 夹具有状态化（`luxLinkState`） |
| 4 | ca2da42 | manifest.json 1.7.0→1.7.1（`?ver=` 缓存戳经 `_manifest_version` 自动失效）+ README（建组段补面板内编辑入口 + 版本历史 V1.7.1 行） |
| 5 | （本报告 commit） | 汇总报告落仓 + 台账收割（V1.7.1 节，ANL-F-148 起待审查发现续编） |

## 2. 语义摘要

### REST 契约（web.py LuxLinkGroupView，套 anl/config 先例）

- **GET `?entry_id=`** → `{ok, zone_name, link_group, cross_fade, max_step_pct, link_members}`：三键现值经 `merged_options(entry.options)` 回填默认（lux/const.py 单一来源）；`link_members`=完整组员名单（**含本区**、zone_name 排序稳定）——与 overview 快照的 `link_peers`（**排除自身**、chip「与 X 交叉扣除」语义）键名分工不同，前端弹窗给本区标「（本区）」；名单只含已加载区（disabled/SETUP_RETRY 对等区不在列——与 `_linked_runtimes`/overview 同边界，名单是展示面非控制面）。entry 不存在 404「未知区域」。
- **POST `{entry_id, link_group, cross_fade, max_step_pct}`**：`link_group` 需字符串（非 str 400）、strip 归一（lux/flow.py 写入侧同语义，ANL-F-126 家族；空串=停用）；`cross_fade` 0.1~1.0 / `max_step_pct` 5~50（**范围与 options flow selector 同源**，flow.py `async_step_lux_advanced`，不设第二套数字），非数字/NaN/Inf 越界 400；防线复用 `_json_object`（非对象 body 400）/`_entry_id_of`（非字符串 entry_id 400）/`@require_admin`；过校验**镜像写 entry.options 只替换三键**（其余 options 键原样保留——options 流是全镜像写，漏带会把扫描间隔/死区等静默打回默认）→ `async_update_entry` → 顶层 update listener（__init__.py）自动 reload 重建 runtime；POST 后对等区靠 `_linked_runtimes` 现查即时生效，未重载对等区旧组标签保持（与 options flow 同边界）。

### 前端（page-lux.js）

- zone 卡联动组 chip 改可点 button（`data-action="link-group"`，观感对齐 span.cx-tag）：已设组显示原「联动组『X』：与 A、B 交叉扣除/（暂无同组成员）」文案；未设组显示弱提示「联动组未设置（点击配置）」。
- 点击开编辑弹窗（结构套 page-anl `_openDialog` 先例，行样式与 anl dlgRow 同款）：先 GET 现值+组员名单再开（名单是超越原生对话框的价值面，不从 overview 缓存拼）；弹窗内=组员名单 cx-note（本区标注）+ 组标签 TextSelector 语义 + 交叉系数/缓变步幅 number 输入（客户端 min/max/step 同 selector 数值，服务端同范围二次校验）；空值显式报错（ANL-F-034 同哲学）；保存成功 ack「联动组已保存，正在应用」+ 关弹窗后 `_refresh()` 即时刷 chip（轮询 5s 兜底）——未触碰 `_render`/`_payloadJson` 渲染门（面板重载问题用户拍板先不修）。

### UI 微调（用户四点反馈）

1. 逐灯弹窗「日间恒照度接管」三态说明从开关同级并排（V1.7.0 错用 dlgRow 第三参数位）改挂行下块级 cx-note（紧随 ciRow）；grep 证实 dlgRow help 位全仓仅 ciRow 一处传值，dlgRow 定义未动。
2. cx-note 说明文字 12.5px→10px（用户指定值）。
3. cx-note 删 `border-left: 3px solid var(--fx-accent)` 蓝竖条；**warn/err 语义色条保留**（用户只投诉 accent 蓝条；底规则删 border-left 后 warn/err 变体改为自带完整 `border-left: 3px solid` 声明，渲染不变且无死规则）。
4. `.fx-dialog::backdrop` rgba(0,0,0,0.35)→0.25 + 注释（感知校准：0.35 被感知约 50% 压暗；黑 scrim 为 HA 原生 dialog 惯例保持）；dev/index.html 内联副本同步（FXH-F-009 夹具/生产同观感）。

## 3. 出门门禁（步骤 4 实跑）

- pytest tests/ -q：**344 passed**（基线 338 只增不减，+6 端点钉；无 skip/xfail）
- dev/test_panel.py：**all green**（存量全过 + 新钉×4：lux 弹窗预填=组员名单「本区」标注+三键现值 / 保存 POST 三键 / chip 即时更新 / anl ciHint 行下块级结构钉 inlineWithToggle=False+belowToggle+block）
- ruff check custom_components tests dev：**零告警**（步骤 3 中途出 1 处 E501 新增，即修）
- 翻译对账：en.json / zh-Hans.json 各 **129 键，双向 delta=0**（本批零新增键，文案前端内联中文与 anl GUI 同惯例）
- node --check 双 JS OK；py_compile 改动 .py OK

## 4. 遗留疑点与审查透镜提示

1. **link_members 只含已加载区**：disabled/SETUP_RETRY 对等区不在名单——与 overview chip/`_linked_runtimes` 现查同边界；名单是展示面非控制面，不构成控制有效性风险。跨区即时性：POST 只 reload 本条目，对等区旧组标签保持到其自身重载（与 options flow 同边界）。
2. **语义双键分工**：overview `link_peers`（排除自身）vs 编辑器 `link_members`（含本区）——审查请勿误判其一为冗余或误改合一。
3. **面板保存后整页重载**：用户拍板先不修（两档方案登记在案，禁顺手修）；本批保存后 `_refresh()` 是既有轮询语义内的最小动作，未触碰 `_render`/`_dialogClosed`/`_payloadJson` 相关行为。
4. **遗留 P3 F-142~147**：本批未承接（计划书 known_findings=无、范围外零改动），仍挂「批 2 B3+C 系清理优先承接」；审查如扫到同面请按台账既记状态评审。
5. **POST 数值防线口径**：`float(True)` 可过（True→1.0 入 0.1~1.0 域）——与既有 LuxTargetView 同口径，未新增防线（非必要不增加）。
6. **warn/err 色条保留裁量**：见 §2.3；如审查认为应随蓝条一并删除，属产品语义裁定，回用户。

## 5. wb 首审记录（主会话提交后回填）

- 批次名 `fhl-v171-linkgroup-editor-20261024`；task_type=code-review；mode=首审；known_findings=无（新功能小批）。
- （待回填：job id / 发现清单 / 台账 ANL-F-148 起续编 / 复验循环记录。）

## 6. R1/R2 修复回写（ANL-F-159）

R1 修复批（commit a481272，ANL-F-148~154）已使本报告三处描述失效——原文保留在上文供对照，以本节为准；今后修复批把「报告与代码同 commit 对齐」列入出门项。

1. **「保存后 `_refresh()`」已删除**（§2 前端 / §4.3，ANL-F-150）：保存后**不再**立即 `_refresh()`——POST 只落 options、entry reload 是随后调度的异步任务，立即 GET 会撞旧 runtime（载荷不变→渲染门不重绘=旧 chip）或 unload 窗口（区出桶=空态闪最长 5s）。现行契约：保存响应直接乐观更新 chip（`_linkChipText` 单一来源），5s 轮询是唯一真相；reload 出桶窗口由 grace 兜住（ANL-F-155/162/164/166，`page-lux.js` `_pendingSaves` Map + 15s 窗，收敛判据=条目在且 link_group/cross_fade/max_step_pct 三键全等，未收敛不渲染空态；判据演进与钉位详见 §7）。
2. **POST 响应契约补记**（偏离①，R1 裁定可接受）：`POST /api/fumiha_lighting/lux/link_group` 响应为 `{ok, link_group, cross_fade, max_step_pct, link_peers}`——`link_peers` 按**新 group** 现查桶内其他区（排除自身，与 overview 同算法），供前端乐观更新不把有组员的组闪成「暂无同组成员」；单测 `test_link_group_post_returns_new_group_peers`（`tests/test_web.py`）。
3. **「`float(True)` 可过、未新增防线」已失效**（§4.5，ANL-F-153）：`LuxLinkGroupView.post` 现显式拒 bool/非 int-float、`OverflowError` 归入 400，`tests/test_web.py` 有 `True`/`10**400` 钉；R2 起同族口径统一走 `_finite_number`（`anl/config_schema.py`，LuxTargetView / `_finite_or_none` / `_clean_num` 三处消费，ANL-F-156）。

R2 复验新增发现 ANL-F-155~160（F-150 reload 窗口残留 / 数值同族 / 钉矩阵 / 缺键语义 / 本条 / 原生流警示）随第二轮修复 commit 收口，台账续编。

## 7. R3/R4 修复回写（ANL-F-165）

R3 修复批（commit 9e6136a，ANL-F-161~163）未随批回写本报告——违反 §6 自立的「报告与代码同 commit」出门项（R4 复验发现[2]追认）；本节随 R4 修复批（代码+钉+报告同 commit）补记，后续修复批继续执行该出门项。

1. **ANL-F-161（R3）：乐观 chip 更新写游离节点**。修法=文案单一来源 `_linkChipText`（卡片渲染与保存后乐观更新共用，防两处漂移）+ 保存后按 entry_id 重查当前在 DOM 的 chip 再写（`[data-zone="…"] [data-action="link-group"]`，带 `_body` 空守）；查不到（区出桶/卡片重建中）跳过乐观更新等 5s 轮询收敛，不写不可见 DOM。钉：`dev/test_panel.py` `__bumpLuxPayload()` 触发弹窗期间重渲染 + `?reloadgap=1` 压住服务端真相（7s 空窗 > 4s 断言窗），可见 chip 新文案只能来自重查写入。
2. **ANL-F-162（R3）→ ANL-F-164（R4 终态）：grace 收敛判据**。R3 落地=「条目在且 link_group 等于保存值」才清 grace、15s 超时放行；**已知残余**（R4 发现[1]）=组标签只是代理——只调 cross_fade/max_step_pct 的保存组标签不变，保存前已发出、保存后才落地的旧 overview 快照仍会提前清 grace，真出桶窗照旧闪空态。**现行口径（R4 修复）**：`web._serialize_lux_zone` 下发 `cross_fade`/`max_step_pct` 两数（`runtime.opts` merged_options 读穿），保存侧记三键，收敛判据=**条目在且三键全等**；grace 槽为 `Map<entry_id, {at, group, fade, step}>`（ANL-F-166，多区互不顶掉）。钉：超时侧（`?reloadgaplong`）/ `_boot` 重置 / `?reloadgapstale` 换标签旧快照（R3×3）+ R4 同标签只改交叉系数钉（旧码必红，红→绿实测）+ R4 双区 15s 内先后保存钉。
3. **ANL-F-163（R3）：FakeRequest `body or {}` 吞 falsy body**。修法=模块级哨兵 `_UNSET`——未传 → `{}`，显式 falsy（`[]`/`None`/`0`）原值直达；缺键/非对象两防线分别断精确文案（lux+anl 两处），非对象分支获真覆盖（旧桩把 falsy 换成 `{}` 走缺键分支=假绿）。R4 复验确认 fixed。
