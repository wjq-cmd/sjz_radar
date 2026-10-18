"""One-time V5.1.4 UI update; run only on the isolated 5.1.3 copy."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
web=ROOT/'Parser/ErCore/web'
page=ROOT/'Parser/ErCore/src/core/er_radar.py'
t=page.read_text(encoding='utf-8')
assert 's-walltrans' not in t
t=t.replace('model3d:"tactical", v3d:0', 'walltrans:78, floortrans:0, model3d:"tactical", v3d:0')
t=t.replace('<div class="sec">地图元素</div>', '''<div class="sec">地图元素</div>
    <div class="sld"><span>墙体透明度</span><input id="s-walltrans" type="range" min="0" max="100" step="1" value="78"><span class="v tnum" id="v-walltrans">78%</span></div>
    <div class="sld"><span>地面 / 屋顶透明度</span><input id="s-floortrans" type="range" min="0" max="100" step="1" value="0"><span class="v tnum" id="v-floortrans">0%</span></div>''')
t=t.replace('>第一视角平滑</span>', '>第一视角位置平滑</span>')
t=t.replace('el.addEventListener("input", apply);', '''el.addEventListener("input", () => { apply(); if (R3) R3.setPrefs(PREF); });''')
t=t.replace('bindSlider("s-fov", "fov", v => v + "°");', '''bindSlider("s-fov", "fov", v => v + "°");
bindSlider("s-walltrans", "walltrans", v => v + "%");
bindSlider("s-floortrans", "floortrans", v => v + "%");''')
# Replace stale r3d implementation commentary with the active adapter contract.
start=t.index('// 第一视角平滑时间常数')
end=t.index('bindSlider("s-fpvtau"',start)
t=t[:start]+'// Korr adapter receives input changes immediately. Position smoothing uses a time constant.\n'+t[end:]
t=t.replace('// 第一视角眼高（米）与视野 FOV（度）：r3d 每帧读，改完立刻生效','// 眼高/FOV 通过 setPrefs 即时传递，启动时也从本地偏好恢复。')
t=t.replace('shubao.css?v=5.1.3', 'shubao.css?v=5.1.4')
anchor='<link rel="stylesheet" href="/ui/shubao.css?v=5.1.4">'
assert anchor in t
t=t.replace(anchor,anchor+'\n<script src="/ui/dock.js?v=5.1.4" defer></script>')
page.write_text(t,encoding='utf-8')

renderer=web/'m3d/korr-renderer.js';t=renderer.read_text(encoding='utf-8')
t=t.replace('let indoorClarityEnabled = false;', '''let gatewaySurfacePrefs = {walltrans:78, floortrans:0};
function applyGatewaySurfaceOpacity() {
    if (!mapMesh?.userData.gatewayTransparentWalls) return;
    const materials = _getRadarMapMaterials();
    [gatewaySurfacePrefs.floortrans, gatewaySurfacePrefs.walltrans].forEach((value, index) => {
        const material = materials[index]; if (!material) return;
        const opacity = 1 - value / 100, transparent = opacity < 1;
        if (material.transparent !== transparent) { material.transparent = transparent; material.needsUpdate = true; }
        material.opacity = opacity; material.visible = opacity > 0;
        material.depthWrite = !transparent;
    });
}
let indoorClarityEnabled = false;''')
t=t.replace('wallMaterial.transparent = true; wallMaterial.opacity = 0.22; wallMaterial.depthWrite = false;', 'wallMaterial.transparent = true; wallMaterial.opacity = 1 - gatewaySurfacePrefs.walltrans / 100; wallMaterial.depthWrite = false;')
t=t.replace('mapMesh.userData.gatewayTransparentWalls = true;', 'mapMesh.userData.gatewayTransparentWalls = true;\n    applyGatewaySurfaceOpacity();')
t=t.replace('preferences(p){\n        if(camera', '''preferences(p){
        for (const key of ['walltrans','floortrans']) {
            if (Number.isFinite(p[key])) gatewaySurfacePrefs[key] = THREE.MathUtils.clamp(p[key], 0, 100);
        }
        applyGatewaySurfaceOpacity();
        if(camera''')
t=t.replace('mode:cameraMode,position:camera?.position.toArray()', 'mode:cameraMode,fov:camera?.fov,position:camera?.position.toArray()')
t=t.replace('walls:{cutout:indoorClarityActive,opacity:', 'surfaces:{floorOpacity:_getRadarMapMaterials()[0]?.opacity,wallTransparency:gatewaySurfacePrefs.walltrans},\n            walls:{cutout:indoorClarityActive,opacity:')
t=t.replace('else followState.eye.lerp(desiredEye, window.AppState?.gameData?.replay ? 1 : FIRST_PERSON_CAMERA.POSITION_LERP);', '''else {
            const now = performance.now(), dt = Math.min(100, Math.max(0, now - (followState.eyeUpdatedAt ?? now - 16.67)));
            const tau = Number(window.AppState?.display?.fpvtau);
            const alpha = window.AppState?.gameData?.replay || tau === 0 ? 1 :
                Number.isFinite(tau) && tau > 0 ? 1 - Math.exp(-dt / tau) : FIRST_PERSON_CAMERA.POSITION_LERP;
            followState.eye.lerp(desiredEye, alpha);
        }
        followState.eyeUpdatedAt = performance.now();''')
renderer.write_text(t,encoding='utf-8')
adapter=web/'m3d/korr-adapter.js';t=adapter.read_text(encoding='utf-8').replace('eyeHeight:pref.fpvheight,','eyeHeight:pref.fpvheight,fpvtau:pref.fpvtau,');adapter.write_text(t,encoding='utf-8')

files=[ROOT/'GatewayCapture.csproj',ROOT/'GatewayConsoleApplication.cs',ROOT/'V5-README.md',page,
       ROOT/'verification/test_published.py',ROOT/'verification/webgl511.html']
files+=list((web/'m3d').glob('*.js'))+[web/'ui/shubao.css']
for file in files:
    t=file.read_text(encoding='utf-8');file.write_text(t.replace('5.1.3','5.1.4'),encoding='utf-8')
print('V5.1.4 UI source updated')
