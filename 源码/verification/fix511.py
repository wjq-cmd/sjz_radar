from pathlib import Path
root=Path(__file__).resolve().parents[1]
def edit(path,old,new):
 p=root/path;s=p.read_text(encoding='utf-8-sig');assert old in s,(path,old[:100]);p.write_text(s.replace(old,new),encoding='utf-8')
a='Parser/ErCore/web/m3d/korr-adapter.js'
edit(a,"import * as THREE from 'three';", "import * as THREE from 'three';\nimport {characterKind,aimOf,PosePresenter} from './gateway-pose.js?v=5.1.1-1';")
edit(a,"['player','mate','unknown'].includes(e.kind) && !e.is_bot", "characterKind(e)")
edit(a,"e.kind==='ai' || e.is_bot", "e.kind==='ai'")
edit(a,"z:snapshot.self[2],yaw:snapshot.self_yaw,\n      pitch:snapshot.self_pitch", "z:snapshot.self[2],...aimOf(snapshot)")
edit(a,"let latest=null,previous=null,received=0,interval=33,pref=options.pref || {},active=true;", "let latest=null,pref=options.pref || {},active=true;\n  const presenter=new PosePresenter();")
edit(a,"""      if(latest && s.replay?.position < latest.replay?.position){previous=null;}
      else previous=latest;
      interval=Math.min(100,Math.max(16,performance.now()-received));received=performance.now();latest=s;
      window.AppState.gameData=toKorr(s,pref);window.AppState.frameCount++;""", """      latest=s;presenter.push(s,performance.now());
      window.AppState.gameData=toKorr(presenter.sample(performance.now()),pref);window.AppState.frameCount++;""")
edit(a,"pref.cam3d==='chase'","['fpv','chase'].includes(pref.cam3d)")
edit(a,"mode==='chase'","['fpv','chase'].includes(mode)")
edit(a,"['fpv','yaw'].includes(mode)","mode==='fpv'")
edit(a,"e?.is_bot||e?.kind==='ai'?'ai':'player'","e?.kind==='ai'?'ai':'player'")
edit(a,"""      const sample=interpolateHumans(previous,latest,(performance.now()-received)/interval);""", "      const sample=presenter.sample(performance.now());")
edit(a,"interpolation:'human-only',source:latest?.cursor", "interpolation:'human-only',source:latest?.cursor,expectedCharacters:window.AppState.gameData?.players.length||0")
# Old public helper now uses the same angle/axis contract.
edit(a,"return {...current,entities,self:own.world,self_yaw:own.yaw};", "return {...current,entities,self:own.world,self_yaw:own.yaw,self_aim_yaw:own.yaw};")
r='Parser/ErCore/web/m3d/korr-renderer.js'
edit(r,"import * as THREE from 'three';", "import * as THREE from 'three';\nimport {aimDirection,splitSurfaceIndices} from './gateway-pose.js?v=5.1.1-1';")
edit(r,"let indoorClarityEnabled = true;", "let indoorClarityEnabled = false; // Gateway uses transparent walls; no camera cutout.")
edit(r,"return [...new Set([variants.plain, variants.cutaway].filter(Boolean))];", "return [...new Set([variants.plain, variants.cutaway].flat().filter(Boolean))];")
edit(r,"const profile = { ...base };", "const profile = { ...base, ssao:false, screenSpaceEdges:false }; // Solid-depth SSAO is inappropriate for transparent walls.")
edit(r,"    const eligible = indoorClarityEnabled && cameraMode", "    const eligible = !mapMesh?.userData.gatewayTransparentWalls && indoorClarityEnabled && cameraMode")
# Eliminate secondary frame-rate-dependent position filters and reused-slot glide.
import re
p=root/r;s=p.read_text(encoding='utf-8')
for axis in ('x','y','z'):
 s=re.sub(rf'e\.root\.position\.{axis} \+= \(t{axis} - e\.root\.position\.{axis}\) \* \(window\.AppState\?\.gameData\?\.replay \? 1 : 0\.35\);',f'e.root.position.{axis} = t{axis};',s)
s=s.replace("e.root.rotation.z += dy * (window.AppState?.gameData?.replay ? 1 : 0.4);","e.root.rotation.z = targetYaw;")
for name in ('b','a'):
 s=s.replace(f"            e.root.userData.entityName = String({name}.name || '');",f"            e.root.userData.entityKey = {name}.key;\n            e.root.userData.entityName = String({name}.name || '');")
s=s.replace("            e.root.visible = true;\n            e.root.position.set(ueToThreeX(it.x)","            e.root.visible = true;\n            e.root.userData.entityKey = it.key;\n            e.root.position.set(ueToThreeX(it.x)")
p.write_text(s,encoding='utf-8')
edit(r,"if (distM < 500) return 1;\n    return 0;", "return 1; // Preserve character identification at any distance, matching the 2D roster.")
# UE pitch is positive up; scene Z is also up. Mouse orbit cannot override FPV packets.
edit(r,"    const yaw = ueYawToThreeRotZ(hit.yaw);\n    const direction = new THREE.Vector3(Math.cos(yaw), Math.sin(yaw), 0);", "    const direction = new THREE.Vector3(...aimDirection(Number(hit.yaw)||0,Number(hit.pitch)||0));")
start="""        if(window.AppState?.display?.followYaw && Number.isFinite(hit.yaw)) {
            const yaw=ueYawToThreeRotZ(hit.yaw), pitch=-(Number(hit.pitch)||0)*Math.PI/180;
            direction.set(Math.cos(yaw)*Math.cos(pitch),Math.sin(yaw)*Math.cos(pitch),Math.sin(pitch));
        }"""
edit(r,start,"""        if(Number.isFinite(hit.yaw)) direction.set(...aimDirection(hit.yaw,Number(hit.pitch)||0));""")
edit(r,"    controls.enablePan = cameraMode === CAMERA_MODES.FIRST_PERSON ? false : freeOrbitEnablePan;", "    controls.enableRotate = cameraMode !== CAMERA_MODES.FIRST_PERSON;\n    controls.enablePan = cameraMode === CAMERA_MODES.FIRST_PERSON ? false : freeOrbitEnablePan;")
# Keep all triangles; floor faces write depth, wall faces alpha blend without carving holes.
start="""    const cutawayMaterial = _createRadarMapMaterial(heightRange.low, heightRange.high, true);
    mapMesh = new THREE.Mesh(geo, plainMaterial);
    mapMesh.userData.radarMaterials = {
        plain: plainMaterial,
        cutaway: cutawayMaterial,
    };"""
replacement="""    const wallMaterial = _createRadarMapMaterial(heightRange.low, heightRange.high, false);
    wallMaterial.transparent = true; wallMaterial.opacity = 0.22; wallMaterial.depthWrite = false;
    wallMaterial.forceSinglePass = true;
    const partition = splitSurfaceIndices(geo.attributes.position.array,geo.index.array);
    geo.setIndex(new THREE.BufferAttribute(partition.indices,1));
    geo.clearGroups();geo.addGroup(0,partition.horizontal,0);geo.addGroup(partition.horizontal,partition.vertical,1);
    const materials = [plainMaterial,wallMaterial];
    mapMesh = new THREE.Mesh(geo,materials);
    mapMesh.userData.radarMaterials = {plain:materials};
    mapMesh.userData.gatewayTransparentWalls = true;
    mapMesh.userData.surfaceCounts = {floor:partition.horizontal/3,wall:partition.vertical/3};"""
edit(r,start,replacement)
# Numeric inspection is opt-in, so ordinary rendering avoids serializing every entity.
edit(r,"export const gateway = {", """export const gateway = {
    inspect(){
        const entities=[];
        for(const [kind,pool] of Object.entries(POOL))for(const e of pool)if(e.root.visible)
            entities.push({kind,key:e.root.userData.entityKey,position:e.root.position.toArray(),yaw:e.root.rotation.z});
        const direction=new THREE.Vector3();camera?.getWorldDirection(direction);
        return {entities,self:selfEntity?.root.position.toArray(),camera:{mode:cameraMode,position:camera?.position.toArray(),direction:direction.toArray()},
            walls:{cutout:indoorClarityActive,opacity:_getRadarMapMaterials()[1]?.opacity,depthWrite:_getRadarMapMaterials()[1]?.depthWrite,counts:mapMesh?.userData.surfaceCounts}};
    },""")
edit(r,"playerKeys: POOL.players.filter(e => e.root.visible).map(e => e.root.userData.entityKey),", "playerKeys: POOL.players.filter(e => e.root.visible).map(e => e.root.userData.entityKey),\n        humanPlayers: POOL.players.filter(e=>e.root.visible && !window.AppState?.gameData?.players.find(p=>p.key===e.root.userData.entityKey)?.is_bot).length,")
for file in ['GatewayCapture.csproj','GatewayConsoleApplication.cs','Realtime/ConsoleDashboard.cs','V5-README.md']:
 p=root/file;s=p.read_text(encoding='utf-8-sig').replace('5.1.0','5.1.1');p.write_text(s,encoding='utf-8')
for file in [a,'Parser/ErCore/src/core/er_radar.py']:
 p=root/file;s=p.read_text(encoding='utf-8').replace('5.1.0-1','5.1.1-1');p.write_text(s,encoding='utf-8')
# UI diagnostics include all character models, not just human subset.
edit('Parser/ErCore/src/core/er_radar.py',"'3D真人实体 '+(render?.players||0)","'3D人物 '+(render?.players||0)+' / 应显示 '+(render?.expectedCharacters||0)+' · 其中真人 '+(render?.humanPlayers||0)")
