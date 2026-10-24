"""一次性适配 test_anl_v15_fixes.py 与 test_anl_v15_r2_fixes.py 到 V1.7.0。"""

from pathlib import Path

# ---- test_anl_v15_fixes.py ------------------------------------------------
P = Path(__file__).parent / "test_anl_v15_fixes.py"
src = P.read_text(encoding="utf-8")

# ① 关 CI 归还：补种子（V1.7.0 归还判据按 zone 集合推导）
src = src.replace(
    '''    hass = _recording_hass()
    # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_lights"] == []''',
    '''    hass = _recording_hass()
    # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    # V1.7.0 反查推导：待归还虚拟灯由生效清单经反查表推导
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", VIRT)
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_lights"] == []''',
)

# ② 改指 X→Y → 改清单 light.a→light.b：旧 zone 归还、新 zone 不动
# （虚拟灯改指语义随键废除消失；zone 改指=清单改灯）
src = src.replace(
    '''def test_web_post_repoint_releases_old_only():
    """③改指 X→Y → turn_off 旧灯 X；新灯走注册表校验后落盘。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(  # F-083：助手 mode 守卫，见①
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    _anl_bucket(hass, _config_manager(_ci_entry()))
    _register_virtual("light.new")
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_virtual_light": "light.new"}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_virtual_light"] == "light.new"''',
    '''def test_web_post_repoint_releases_old_only():
    """③zone 改指（清单 light.a→light.b，V1.7.0 语义：虚拟灯无配置键，
    改指=改接管清单）→ turn_off 旧 zone 虚拟灯；新 zone 虚拟灯不动。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(  # F-083：助手 mode 守卫，见①
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid in (VIRT, VIRT2)
        else None
    )
    stubs.seed_light_zone(hass, "lux-a", "light.a", "卫生间", VIRT)
    stubs.seed_light_zone(hass, "lux-b", "light.b", "干区", VIRT2)
    entry = _ci_entry()
    entry.data["lights"] = ["light.a", "light.b"]  # 灯集须含新接管灯
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.b"]}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_lights"] == ["light.b"]''',
)

# VIRT2 常量
src = src.replace('VIRT = "light.virtX"', 'VIRT = "light.virtX"\nVIRT2 = "light.virt2"')

# ③ 存量值放行 → 新形态：清单原样重存（zone 有效）零归还
src = src.replace(
    '''def test_web_post_stored_virtual_resubmit_skips_registry_check():
    """F-058 核心：存量值原样重存（注册表缺失）→ 放行 200、零归还。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    # 刻意不注册 light.virtX——老 states 校验必 400 的场景
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.a"],
                                   "day_ci_virtual_light": VIRT}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []''',
    '''def test_web_post_stored_ci_lights_resubmit_zero_release():
    """存量值放行（F-058 新形态）：清单原样重存（zone 有效）→ 200、零归还
    （reload 等价场景不动灯；虚拟灯注册表校验随键废除消失）。"""
    hass = _recording_hass()
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", VIRT)
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.a"]}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []''',
)

# ④ 改指未注册实体 400 → 清单含 ∉反查表的组内灯 400（逐灯报名）
src = src.replace(
    '''def test_web_post_new_virtual_missing_from_registry_400():
    """F-058/F-064：改指为注册表不存在的实体 → 400、零落盘。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_virtual_light": "light.ghost"}}})
        )
    )
    assert resp.status == 400 and "light.ghost" in resp.payload["error"]
    assert recorded == []''',
    '''def test_web_post_ci_light_outside_any_zone_400():
    """zone 归属校验（V1.7.0 A1/A2 同源）：组内灯 ∉ 反查表 → 400 逐灯报名、
    零落盘（校验失败绝不动灯）。"""
    hass = _recording_hass()
    entry = _ci_entry()
    entry.data["lights"] = ["light.a", "light.b"]  # light.b 在组内、无区域
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.b"]}}})
        )
    )
    assert resp.status == 400
    assert "灯 light.b 不属于任何恒照度区域" in resp.payload["error"]
    assert recorded == []''',
)
P.write_text(src, encoding="utf-8", newline="\n")

# ---- test_anl_v15_r2_fixes.py ---------------------------------------------
P2 = Path(__file__).parent / "test_anl_v15_r2_fixes.py"
src2 = P2.read_text(encoding="utf-8")

# ⑤ day_disabled 400 用例：补种子（zone 校验先行，须让清单灯过校验）
old = '''    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: False,
              CONF_DAY_CI_LIGHTS: [LIGHT], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT}
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": [LIGHT],
                                   "day_enabled": False}}})
        )
    )
    assert resp.status == 400 and "day_enabled" in resp.payload["error"]'''
new = '''    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: False,
              CONF_DAY_CI_LIGHTS: [LIGHT], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT}
    )
    # V1.7.0：zone 归属校验先于跨字段 400——种子让清单灯过校验
    stubs.seed_light_zone(hass, "lux-x", LIGHT, "卫生间", VIRT)
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": [LIGHT],
                                   "day_enabled": False}}})
        )
    )
    assert resp.status == 400 and "day_enabled" in resp.payload["error"]'''
assert old in src2, "r2 day_disabled pattern missing"
src2 = src2.replace(old, new)

P2.write_text(src2, encoding="utf-8", newline="\n")
print("ok")
