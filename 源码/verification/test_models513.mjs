import assert from 'node:assert/strict';
import * as THREE from 'three';
import {buildCharacterModel,syncCharacterModel} from '../Parser/ErCore/web/m3d/character-models.js';
for(const id of ['tactical','mannequin','beacon']){
 const m=buildCharacterModel(id),box=new THREE.Box3().setFromObject(m.root);
 assert.equal(m.footZ,box.min.z,id+' reports its actual lowest point');
 assert(box.min.z>=-.91&&box.max.z<=.91,id+' must retain source capsule center/feet');
 assert(m.root.children.length>=3);m.setColor(0xabcdef);m.dispose();
}
const heading=(style,occluded)=>{const mesh=new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial({color:0xffffff}));mesh.userData={headingStyle:style,headingOccluded:occluded};return mesh;};
const headings={arrow:{visible:heading('arrow',false),occluded:heading('arrow',true)},line:{visible:heading('line',false),occluded:heading('line',true)}};
const entity={root:new THREE.Group(),capsule:new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial({color:0x123456})),occCapsule:new THREE.Mesh(new THREE.BoxGeometry(),new THREE.MeshBasicMaterial({color:0xff4058})),headings};
entity.root.position.set(12,-34,56);entity.root.add(entity.capsule);
entity.root.add(entity.occCapsule);
for(const pair of Object.values(headings))entity.root.add(pair.visible,pair.occluded);
for(let i=0;i<20;i++)for(const style of ['tactical','beacon','mannequin','capsule'])for(const scale of [.5,1,2]){
 syncCharacterModel(entity,style,{scale,visibleColor:0x123456,occludedColor:0xabcdef,showHeading:true,directionStyle:'line',directionAnchor:'foot'});assert.deepEqual(entity.root.position.toArray(),[12,-34,56]);
 assert.equal(entity.root.children.length,style==='capsule'?6:8);assert.equal(entity.capsule.visible,style==='capsule');
 assert.equal(entity.occCapsule.visible,style==='capsule');assert.equal(entity.modelRenderedFootZ,-.9);
 assert.equal(headings.line.visible.visible,true);assert.equal(headings.line.occluded.visible,true);assert.equal(headings.arrow.visible.visible,false);assert.equal(headings.arrow.occluded.visible,false);
 assert.equal(entity.headingStyle,'line');assert.equal(entity.headingAnchor,'foot');assert(Math.abs(headings.line.visible.position.z+.82)<1e-9);
 assert.equal(headings.line.visible.material.color.getHex(),0x123456);assert.equal(headings.line.occluded.material.color.getHex(),0xabcdef);
 if(style!=='capsule'){
  assert.equal(entity.customModel.root.children.length,entity.customOccludedModel.root.children.length);
  assert.equal(entity.customOccludedModel.root.userData.occluded,true);
 }
}
console.log('PASS: visible/occluded model and heading parity, independent colors, exact feet, stable root XYZ and repeated switching');
