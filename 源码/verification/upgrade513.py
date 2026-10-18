from pathlib import Path
import json,shutil
ROOT=Path(__file__).resolve().parents[1]
def edit(name,old,new):
 p=ROOT/name;s=p.read_text(encoding='utf-8-sig');assert old in s,(name,old[:100]);p.write_text(s.replace(old,new),encoding='utf-8')
for name in ['GatewayCapture.csproj','GatewayConsoleApplication.cs','Realtime/ConsoleDashboard.cs','V5-README.md','Parser/ErCore/src/core/er_radar.py','Parser/ErCore/web/m3d/korr-adapter.js','Parser/ErCore/web/m3d/korr-renderer.js']:
 p=ROOT/name;s=p.read_text(encoding='utf-8-sig').replace('5.1.1','5.1.3');p.write_text(s,encoding='utf-8')
radar='Parser/ErCore/src/core/er_radar.py';adapter='Parser/ErCore/web/m3d/korr-adapter.js';renderer='Parser/ErCore/web/m3d/korr-renderer.js';parser='Parser/gateway_parser.py'
edit(radar,'</style>','</style>\n<link rel="stylesheet" href="/ui/shubao.css?v=5.1.3">')
edit(radar,'<span id="pan-name">干员台</span>','<span id="pan-name">玩家列表</span>')
edit(radar,'>敌存<','>存活<')
edit(radar,'const LOOT_IMG  = (id) => "https://playerhub.df.qq.com/playerhub/60004/object/" + id + ".png";','const LOOT_IMG  = (id) => "/resources/items/" + encodeURIComponent(id) + ".png";')
edit(radar,'v3d:0, pip:1, cam3d:', 'model3d:"tactical", v3d:0, pip:1, cam3d:')
edit(radar,'<input id="s-charsize"','<input id="s-charsize"')
edit(radar,'<div class="vs-seg" id="q3dseg"','<select id="character-model" aria-label="3D 人物模型"><option value="tactical">战术人形</option><option value="mannequin">简洁人形</option><option value="beacon">定位信标</option><option value="capsule">原版胶囊</option></select>\n      <div class="vs-seg" id="q3dseg"')
edit(radar,'bindSlider("s-charsize", "charsize", v => v + "%");','''bindSlider("s-charsize", "charsize", v => v + "%");
$('character-model').value=PREF.model3d;
$('character-model').addEventListener('change',e=>{setPref('model3d',e.target.value);if(R3)R3.setPrefs(PREF);toast('3D 人物模型已切换');});''')
edit(radar,'<span class="badge ghost" style="--b:var(--txt-3)">人机</span>','<span class="badge bot-uncertain" title="旧协议 bIsABot 标记；当前版本真人/人机身份尚未核实，不影响人物显示">疑似人机</span>')
edit(radar,'（人机）','（疑似人机）')
edit(radar,"'真人坐标 '+Number(r.human||0)","'人物坐标 '+Number(r.characters||0)")
edit(radar,"'坐标为全部真人合计；自身姿态 '","'人物含待核实标记；自身姿态 '")
edit(radar,"'真人 '+(c.human||0)+' · AI '+(c.bot||0)+' · 物资 '+(c.loot||0)","'人物 '+(c.characters||0)+'（含自身 '+(c.charactersIncludingSelf||0)+'） · 疑似人机 '+(c.suspectedBots||0)+' · AI单位 '+(c.ai||0)+' · 物资 '+(c.loot||0)")
edit(radar,"' · 其中真人 '+(render?.humanPlayers||0)","' · 未标记人机 '+(render?.humanPlayers||0)")
edit(adapter,'charScale:(pref.charsize||100)/100,','charScale:(pref.charsize||100)/100,model3d:pref.model3d,')
edit(renderer,"import * as THREE from 'three';","import * as THREE from 'three';\nimport {syncCharacterModel} from './character-models.js?v=5.1.3';")
edit(renderer,'            // v698g5: 头顶信息卡 (名字 + 血条 + 武器 + 护甲)','            syncCharacterModel(e,disp.model3d);\n            // v698g5: 头顶信息卡 (名字 + 血条 + 武器 + 护甲)')
edit(renderer,'    selfEntity.root.scale.setScalar(Number(disp.charScale)||1);','    syncCharacterModel(selfEntity,disp.model3d);\n    selfEntity.root.scale.setScalar(Number(disp.charScale)||1);')
edit(parser,'if entity.get("kind") not in ("player", "mate") or entity.get("is_bot"):', 'if entity.get("kind") not in ("player", "mate", "unknown"):')
edit(parser,'                            self.rate_events.append((now, "human", count))','                            self.rate_events.append((now, "characters", count))\n                            if not entity.get("is_bot"):\n                                self.rate_events.append((now, "human", count))')
edit(parser,'            missing = sum(e.get("kind") == "loot"','''            counts["characters"] = sum(e.get("kind") in ("player", "mate", "unknown") for e in entities)
            counts["charactersIncludingSelf"] = counts["characters"] + int(self.last_state.get("self") is not None)
            counts["suspectedBots"] = sum(e.get("kind") in ("player", "mate", "unknown") and bool(e.get("is_bot")) for e in entities)
            missing = sum(e.get("kind") == "loot"''')
edit(parser,'        if path.startswith("/m3d/")','''        if path.startswith("/resources/"):
            er_radar.m3d_serve(self, path, prefix="/resources/", root=ASSETS)
            return
        if path.startswith("/ui/"):
            er_radar.m3d_serve(self, path, prefix="/ui/", root=ROOT / "web/ui")
            return
        if path.startswith("/m3d/")''')
edit(parser,'    Handler.map_info = er_radar.Handler.map_info = er_radar.load_map_info()','''    map_info = er_radar.load_map_info()
    for item in map_info.get("maps", []):
        item["tileUrl"] = "/resources/maps2d/" + item["key"] + "/{z}_{x}_{y}.jpg"
    Handler.map_info = er_radar.Handler.map_info = map_info''')
dashboard='Realtime/ConsoleDashboard.cs'
edit(dashboard,'真人 {Num(counts,"human")} | AI {Num(counts,"bot")}','人物 {Num(counts,"characters")}（含自身 {Num(counts,"charactersIncludingSelf")}）| 疑似人机 {Num(counts,"suspectedBots")} | AI单位 {Num(counts,"ai")}')
edit(dashboard,'真人有效坐标样本 {Decimal(rates,"human"):F1}','人物有效坐标样本 {Decimal(rates,"characters"):F1}')
edit(dashboard,'离线回放 · 真人优先','离线回放 · 人物状态')
print('V5.1.3 UI, local resource routes, model selection and count labels installed')
